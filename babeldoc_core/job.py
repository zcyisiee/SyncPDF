"""Resumable DocumentJob state machine."""

from __future__ import annotations

import hashlib
import json
import pickle
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .config import JobConfig
from .manifest import atomic_write_json
from .manifest import read_json
from .manifest import sha256_file
from .manifest import update_artifact_hashes
from .models import JobError
from .models import JobState
from .protocol import DocumentBlock
from .protocol import DocumentIR
from .protocol import extract_formula_atoms
from .protocol import extract_style_atoms
from .protocol import parse_markdown
from .protocol import serialize_markdown
from .protocol import validate_translation as check_translation


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class DocumentJob:
    """Persisted document workflow with bounded and inspectable transitions."""

    def __init__(self, pdf: Path, workdir: Path, config: JobConfig, manifest: dict[str, Any]):
        self.pdf = pdf
        self.workdir = workdir
        self.agent_dir = workdir / "agent"
        self.config = config
        self._manifest = manifest

    @classmethod
    def create(
        cls,
        pdf: str | Path,
        workdir: str | Path,
        config: JobConfig | dict[str, Any] | None = None,
    ) -> "DocumentJob":
        pdf_path = Path(pdf).expanduser().resolve()
        work_path = Path(workdir).expanduser().resolve()
        if not pdf_path.is_file():
            raise JobError("pdf_missing", f"input document does not exist: {pdf_path}")
        job_config = config if isinstance(config, JobConfig) else JobConfig.from_dict(config)
        agent = work_path / "agent"
        existing_manifest = read_json(agent / "manifest.json")
        if isinstance(existing_manifest, dict):
            existing_pdf = (existing_manifest.get("input") or {}).get("pdf")
            if existing_pdf == str(pdf_path):
                return cls.load(work_path)
            raise JobError("job_exists", f"a different job already uses {work_path}")
        agent.mkdir(parents=True, exist_ok=True)
        manifest = {
            "schema_version": 1,
            "job_id": f"job-{hashlib.sha256(str(work_path).encode()).hexdigest()[:12]}",
            "created_at": _now(),
            "updated_at": _now(),
            "input": {"pdf": str(pdf_path), "sha256": sha256_file(pdf_path)},
            "config": job_config.to_dict(),
            "state": JobState.CREATED.value,
            "stages": {JobState.CREATED.value: {"at": _now()}},
            "artifacts": {},
            "warnings": [],
            "repair_rounds": {"translation": 0, "layout": 0},
            "resumable_stage": JobState.CREATED.value,
        }
        atomic_write_json(agent / "manifest.json", manifest)
        return cls(pdf_path, work_path, job_config, manifest)

    @classmethod
    def load(cls, workdir: str | Path) -> "DocumentJob":
        work_path = Path(workdir).expanduser().resolve()
        manifest_path = work_path / "agent" / "manifest.json"
        manifest = read_json(manifest_path)
        if not isinstance(manifest, dict):
            raise JobError("manifest_missing", f"manifest not found: {manifest_path}")
        input_pdf = Path((manifest.get("input") or {}).get("pdf", ""))
        if not input_pdf.is_file():
            raise JobError("pdf_missing", f"input document does not exist: {input_pdf}")
        return cls(input_pdf, work_path, JobConfig.from_dict(manifest.get("config")), manifest)

    @property
    def state(self) -> JobState:
        try:
            return JobState(self._manifest.get("state", JobState.CREATED.value))
        except ValueError as exc:
            raise JobError("manifest_invalid", "unknown job state") from exc

    @property
    def job_id(self) -> str:
        return str(self._manifest.get("job_id"))

    @property
    def manifest(self) -> dict[str, Any]:
        return json.loads(json.dumps(self._manifest))

    def status(self) -> dict[str, Any]:
        return self.manifest

    @property
    def ir(self) -> DocumentIR:
        """Return the persisted structured document IR."""

        return self._load_ir()

    @property
    def artifacts(self) -> dict[str, str]:
        return dict(self._manifest.get("artifacts") or {})

    @property
    def manifest_path(self) -> Path:
        return self.agent_dir / "manifest.json"

    def _save(self) -> None:
        self._manifest["updated_at"] = _now()
        atomic_write_json(self.agent_dir / "manifest.json", self._manifest)

    def _transition(self, state: JobState, *, warnings: list[str] | None = None) -> None:
        current = self.state
        allowed = {
            JobState.CREATED: {JobState.PARSED, JobState.BLOCKED_TRANSLATION},
            JobState.PARSED: {JobState.TRANSLATED, JobState.BLOCKED_TRANSLATION},
            JobState.TRANSLATED: {JobState.PROTOCOL_VALIDATED, JobState.BLOCKED_PROTOCOL},
            JobState.PROTOCOL_VALIDATED: {JobState.REVIEWED, JobState.NEEDS_HUMAN_REVIEW},
            JobState.REVIEWED: {JobState.RECONSTRUCTED, JobState.BLOCKED_LAYOUT},
            JobState.RECONSTRUCTED: {JobState.RENDERED, JobState.BLOCKED_LAYOUT},
            JobState.RENDERED: {JobState.ACCEPTED, JobState.NEEDS_HUMAN_REVIEW},
            JobState.BLOCKED_TRANSLATION: {JobState.PARSED},
            JobState.BLOCKED_PROTOCOL: {JobState.TRANSLATED},
            JobState.NEEDS_HUMAN_REVIEW: {JobState.PROTOCOL_VALIDATED, JobState.RECONSTRUCTED},
            JobState.BLOCKED_LAYOUT: {JobState.REVIEWED, JobState.RECONSTRUCTED},
        }
        if state not in allowed.get(current, set()):
            raise JobError("stage_invalid", f"cannot transition {current.value} -> {state.value}")
        self._manifest["state"] = state.value
        self._manifest["resumable_stage"] = state.value
        self._manifest.setdefault("stages", {})[state.value] = {"at": _now()}
        if warnings:
            self._manifest.setdefault("warnings", []).extend(warnings)
        self._save()

    def _artifacts(self, names: list[str]) -> None:
        self._manifest["artifacts"].update(update_artifact_hashes(self.agent_dir, names))
        self._save()

    def parse(self) -> "DocumentJob":
        if self.state != JobState.CREATED:
            return self
        source = self._extract_text()
        blocks: list[DocumentBlock] = []
        for index, paragraph in enumerate([part.strip() for part in source.split("\n\n") if part.strip()], 1):
            label = "text"
            block = DocumentBlock(
                block_id=f"P01-{index:03d}",
                page=1,
                layout_label=label,
                source_text=paragraph,
                translate=self.config.skip_policy.should_translate(label),
                reason=self.config.skip_policy.reason(label),
                formulas=extract_formula_atoms(paragraph),
                styles=extract_style_atoms(paragraph),
            )
            blocks.append(block)
        if not blocks:
            blocks = [DocumentBlock("P01-001", source_text="", translate=True)]
        ir = DocumentIR(blocks=blocks, metadata={"pdf": str(self.pdf), "source": "text-fallback"})
        (self.agent_dir / "document.md").write_text(serialize_markdown(ir), encoding="utf-8")
        (self.agent_dir / "document.json").write_text(json.dumps(ir.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        selection_rows = [
            {
                "id": block.block_id,
                "page": block.page,
                "layout_label": block.layout_label,
                "source_region": block.layout_label,
                "source": block.source_text if block.translate else None,
                "translate": block.translate,
                "reason": block.reason,
                "overridable": bool(block.reason),
            }
            for block in blocks
        ]
        (self.agent_dir / "anchors.json").write_text(
            json.dumps({"rows": selection_rows}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        atomic_write_json(
            self.agent_dir / "selection_report.json",
            {
                "version": 1,
                "summary": {
                    "total": len(blocks),
                    "translate": sum(block.translate for block in blocks),
                    "skipped": sum(not block.translate for block in blocks),
                },
                "rows": selection_rows,
            },
        )
        (self.agent_dir / "sheet.jsonl").write_text(
            "".join(json.dumps(block.to_dict(), ensure_ascii=False) + "\n" for block in blocks),
            encoding="utf-8",
        )
        atomic_write_json(
            self.agent_dir / "layout_geometry.json",
            {
                "version": 1,
                "pages": max((block.page for block in blocks), default=1),
                "page_info": [{"page": 1, "layout_regions": []}],
                "paragraphs": [
                    {
                        "id": block.block_id,
                        "page": block.page,
                        "layout_label": block.layout_label,
                        "source_text": block.source_text,
                        "src_box": None,
                        "layout_box": None,
                        "overridable": True,
                    }
                    for block in blocks
                ],
                "overrides": {"paragraphs": {}, "pages": {}},
            },
        )
        with (self.agent_dir / "state.pkl").open("wb") as handle:
            pickle.dump(ir.to_dict(), handle, protocol=pickle.HIGHEST_PROTOCOL)
        self._transition(JobState.PARSED)
        self._artifacts(["document.md", "document.json", "anchors.json", "selection_report.json", "sheet.jsonl", "layout_geometry.json", "state.pkl"])
        return self

    def _extract_text(self) -> str:
        try:
            raw = self.pdf.read_bytes()
        except OSError as exc:
            raise JobError("pdf_read_failed", str(exc)) from exc
        if self.pdf.suffix.lower() in {".txt", ".md", ".markdown"}:
            return raw.decode("utf-8", errors="replace")
        # Optional PyMuPDF support is intentionally opportunistic.
        try:
            try:
                import pymupdf as fitz  # type: ignore
            except ImportError:
                import fitz  # type: ignore

            document = fitz.open(str(self.pdf))
            return "\n\n".join(page.get_text("text") for page in document).strip()
        except Exception:  # noqa: BLE001 - binary fallback remains deterministic
            decoded = raw.decode("utf-8", errors="ignore")
            return decoded or self.pdf.stem

    def _load_ir(self) -> DocumentIR:
        path = self.agent_dir / "document.json"
        if not path.is_file():
            raise JobError("artifact_missing", "document.json is missing; run parse first")
        return DocumentIR.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def translate(self, provider: Any = None) -> "DocumentJob":
        if self.state == JobState.CREATED:
            self.parse()
        elif self.state == JobState.BLOCKED_TRANSLATION:
            self._manifest.setdefault("repair_rounds", {}).setdefault("translation", 0)
            self._manifest["repair_rounds"]["translation"] += 1
            if self._manifest["repair_rounds"]["translation"] > self.config.max_translation_repairs:
                raise JobError("translation_incomplete", "translation repair limit exceeded")
            self._transition(JobState.PARSED)
        if self.state != JobState.PARSED:
            return self
        ir = self._load_ir()
        warnings: list[str] = []
        output: dict[str, str] = {}
        for block in ir.blocks:
            if not block.translate:
                output[block.block_id] = block.source_text
                continue
            if provider is None:
                output[block.block_id] = block.source_text
                warnings.append(f"fallback_source:{block.block_id}")
                continue
            try:
                if hasattr(provider, "translate"):
                    value = provider.translate(block.source_text, block=block, config=self.config)
                elif callable(provider):
                    value = provider(block.source_text, block=block, config=self.config)
                else:
                    raise TypeError("provider must be callable or expose translate()")
            except Exception as exc:  # noqa: BLE001
                self._manifest["state"] = JobState.BLOCKED_TRANSLATION.value
                self._manifest.setdefault("warnings", []).append(f"provider_error:{exc}")
                self._save()
                raise JobError("provider_error", str(exc)) from exc
            if isinstance(value, dict):
                output[block.block_id] = str(value.get("text", value.get(block.block_id, "")))
            else:
                output[block.block_id] = str(value)
        lines = ["<!-- babeldoc:translation-protocol v1 -->", ""]
        for block in ir.blocks:
            lines.extend([f"<!-- id={block.block_id} label={block.layout_label} -->", output[block.block_id], ""])
        (self.agent_dir / "translated.md").write_text("\n".join(lines), encoding="utf-8")
        (self.agent_dir / "translated.jsonl").write_text(
            "".join(
                json.dumps(
                    {"id": block.block_id, "source": block.source_text, "target": output[block.block_id]},
                    ensure_ascii=False,
                )
                + "\n"
                for block in ir.blocks
            ),
            encoding="utf-8",
        )
        self._transition(JobState.TRANSLATED, warnings=warnings)
        self._artifacts(["translated.md", "translated.jsonl"])
        return self

    def validate_translation(self) -> dict[str, Any]:
        if self.state == JobState.PARSED:
            self.translate()
        elif self.state == JobState.BLOCKED_PROTOCOL:
            self._manifest.setdefault("repair_rounds", {}).setdefault("translation", 0)
            self._manifest["repair_rounds"]["translation"] += 1
            if self._manifest["repair_rounds"]["translation"] > self.config.max_translation_repairs:
                raise JobError("translation_incomplete", "translation repair limit exceeded")
            self._transition(JobState.TRANSLATED)
        if self.state != JobState.TRANSLATED:
            if self.state == JobState.PROTOCOL_VALIDATED:
                return {"ok": True, "violations": []}
            raise JobError("stage_invalid", f"validate_translation requires translated state, got {self.state.value}")
        ir = self._load_ir()
        path = self.agent_dir / "translated.md"
        if not path.is_file():
            raise JobError("artifact_missing", "translated.md is missing")
        violations = check_translation(ir, path.read_text(encoding="utf-8"))
        report = {"ok": not violations, "violations": violations, "checked_at": _now()}
        atomic_write_json(self.agent_dir / "protocol_report.json", report)
        if violations:
            self._manifest["state"] = JobState.BLOCKED_PROTOCOL.value
            self._manifest["resumable_stage"] = JobState.BLOCKED_PROTOCOL.value
            self._save()
            raise JobError("protocol_violation", "; ".join(violations), violations=violations)
        rows = []
        parsed = parse_markdown(path.read_text(encoding="utf-8"))
        for block in ir.blocks:
            rows.append({"id": block.block_id, "source": block.source_text, "target": parsed[block.block_id][0], "layout_label": block.layout_label})
        (self.agent_dir / "translated.jsonl").write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n", encoding="utf-8")
        self._transition(JobState.PROTOCOL_VALIDATED)
        self._artifacts(["protocol_report.json", "translated.jsonl"])
        return report

    def review(self, provider: Any = None) -> "DocumentJob":
        if self.state in {JobState.CREATED, JobState.PARSED}:
            self.translate()
        if self.state == JobState.TRANSLATED:
            self.validate_translation()
        if self.state != JobState.PROTOCOL_VALIDATED:
            return self
        verdict = {"verdict": "pass", "findings": []}
        if provider is not None:
            try:
                if hasattr(provider, "review"):
                    verdict = provider.review(self._load_ir(), job=self)
                elif callable(provider):
                    verdict = provider(self._load_ir(), job=self)
            except Exception as exc:  # noqa: BLE001
                self._manifest["state"] = JobState.NEEDS_HUMAN_REVIEW.value
                self._manifest.setdefault("warnings", []).append(f"review_provider_error:{exc}")
                self._save()
                raise JobError("provider_error", str(exc)) from exc
        atomic_write_json(self.agent_dir / "review_verdict.json", verdict)
        if verdict.get("verdict") in {"block", "needs_human_review"}:
            self._transition(JobState.NEEDS_HUMAN_REVIEW)
        else:
            self._transition(JobState.REVIEWED)
        self._artifacts(["review_verdict.json"])
        return self

    def reconstruct(self, provider: Any = None) -> "DocumentJob":
        if self.state == JobState.BLOCKED_LAYOUT:
            rounds = self._manifest.setdefault("repair_rounds", {}).setdefault("layout", 0) + 1
            self._manifest["repair_rounds"]["layout"] = rounds
            if rounds > self.config.max_layout_repairs:
                raise JobError("layout_blocked", "layout repair limit exceeded")
            self._transition(JobState.REVIEWED)
        if self.state in {JobState.CREATED, JobState.PARSED, JobState.TRANSLATED}:
            self.review()
        if self.state == JobState.PROTOCOL_VALIDATED:
            self.review()
        if self.state != JobState.REVIEWED:
            return self
        output = self.workdir / "translated.pdf"
        try:
            if provider is not None and hasattr(provider, "reconstruct"):
                result = provider.reconstruct(self._load_ir(), job=self, output=output)
                if result is not None and Path(result) != output:
                    shutil.copy2(result, output)
            elif callable(provider):
                result = provider(self._load_ir(), job=self, output=output)
                if result is not None and Path(result) != output:
                    shutil.copy2(result, output)
            else:
                shutil.copy2(self.pdf, output)
        except Exception as exc:  # noqa: BLE001
            self._manifest["state"] = JobState.BLOCKED_LAYOUT.value
            self._manifest["resumable_stage"] = JobState.BLOCKED_LAYOUT.value
            self._manifest.setdefault("warnings", []).append(f"layout_provider_error:{exc}")
            self._save()
            raise JobError("layout_blocked", str(exc)) from exc
        self._manifest.setdefault("outputs", {})["translated.pdf"] = sha256_file(output)
        atomic_write_json(self.agent_dir / "reconstruct_report.json", {"output": str(output), "mode": "provider" if provider else "source_copy"})
        self._transition(JobState.RECONSTRUCTED)
        self._artifacts(["reconstruct_report.json"])
        return self

    def render(self, pages: str | list[int] = "representative") -> "DocumentJob":
        if self.state in {
            JobState.CREATED,
            JobState.PARSED,
            JobState.TRANSLATED,
            JobState.PROTOCOL_VALIDATED,
            JobState.REVIEWED,
        }:
            self.reconstruct()
        if self.state != JobState.RECONSTRUCTED:
            return self
        render_dir = self.agent_dir / "rendered"
        render_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(render_dir / "manifest.json", {"pages": pages, "source": str(self.workdir / "translated.pdf")})
        self._transition(JobState.RENDERED)
        self._artifacts(["rendered/manifest.json"])
        self._transition(JobState.ACCEPTED)
        return self

    def resume(self) -> "DocumentJob":
        """Reload the persisted job in place and return it."""

        refreshed = self.load(self.workdir)
        self._manifest = refreshed._manifest
        self.config = refreshed.config
        self.pdf = refreshed.pdf
        return self
