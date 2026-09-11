"""Implementations of the stable tool names."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from babeldoc_core import DocumentJob
from babeldoc_core import JobConfig
from babeldoc_core import JobState
from babeldoc_core import parse_markdown
from babeldoc_core.manifest import atomic_write_json

from .registry import register


_KNOWN_JOBS: dict[str, str] = {}


def _job(args: dict[str, Any]) -> DocumentJob:
    if not args.get("workdir") and args.get("job_id"):
        known = _KNOWN_JOBS.get(str(args["job_id"]))
        if known:
            args = {**args, "workdir": known}
    if not args.get("workdir"):
        raise ValueError("workdir is required when loading a job")
    try:
        return DocumentJob.load(args["workdir"])
    except Exception as exc:
        from babeldoc_core import JobError

        if isinstance(exc, JobError) and exc.code == "manifest_missing":
            raise JobError("workdir_missing", f"{args['workdir']}/agent 不存在：请先跑 parse_document") from exc
        raise


def _schema(required=("workdir",), properties=None):
    props = {"workdir": {"type": "string"}, "job_id": {"type": "string"}}
    props.update(properties or {})
    return {"type": "object", "properties": props, "required": list(required)}


def _legacy_agent(args: dict[str, Any]) -> Path | None:
    workdir = Path(args.get("workdir", ""))
    agent = workdir / "agent"
    if agent.is_dir() and not (agent / "manifest.json").exists():
        return agent
    return None


@register("job_create", group="job", description="Create a resumable translation job.", input_schema=_schema(("pdf", "workdir"), {"pdf": {"type": "string"}, "config": {"type": "object"}}))
def job_create(args):
    job = DocumentJob.create(args["pdf"], args["workdir"], JobConfig.from_dict(args.get("config")))
    _KNOWN_JOBS[job.job_id] = str(job.workdir)
    return {"job_id": job.job_id, "data": job.status(), "artifacts": [str(job.agent_dir / "manifest.json")]}


@register("job_status", group="job", description="Read persisted job status.", input_schema=_schema())
def job_status(args):
    job = _job(args)
    return {"job_id": job.job_id, "data": job.status()}


@register("job_resume", group="job", description="Reload and continue a persisted job.", input_schema=_schema())
def job_resume(args):
    job = _job(args).resume()
    return {"job_id": job.job_id, "data": job.status()}


@register("parse_document", group="parse", description="Parse a PDF or text fixture into IR.", input_schema=_schema(("pdf", "workdir"), {"pdf": {"type": "string"}}))
def parse_document(args):
    job = DocumentJob.create(args["pdf"], args["workdir"], JobConfig.from_dict(args.get("config"))) if not (Path(args["workdir"]) / "agent" / "manifest.json").exists() else DocumentJob.load(args["workdir"])
    _KNOWN_JOBS[job.job_id] = str(job.workdir)
    job.parse()
    return {"job_id": job.job_id, "data": {"state": job.state.value, "manifest": job.status()}, "artifacts": [str(job.agent_dir / "document.json"), str(job.agent_dir / "document.md")]}


@register("inspect_selection", group="parse", description="Read the auditable translation selection report.", input_schema=_schema())
def inspect_selection(args):
    job = _job(args)
    path = job.agent_dir / "selection_report.json"
    if not path.exists():
        raise FileNotFoundError(path)
    return {"job_id": job.job_id, "data": json.loads(path.read_text(encoding="utf-8")), "artifacts": [str(path)]}


@register("dump_markdown", group="parse", description="Return the canonical source Markdown artifact.", input_schema=_schema())
def dump_markdown(args):
    job = _job(args)
    path = job.agent_dir / "document.md"
    if not path.exists():
        raise FileNotFoundError(path)
    return {"job_id": job.job_id, "data": {"markdown": path.read_text(encoding="utf-8")}, "artifacts": [str(path)]}


@register("dump_ir", group="parse", description="Return the serialized document IR.", input_schema=_schema())
def dump_ir(args):
    job = _job(args)
    path = job.agent_dir / "document.json"
    if not path.exists():
        raise FileNotFoundError(path)
    return {"job_id": job.job_id, "data": json.loads(path.read_text(encoding="utf-8")), "artifacts": [str(path)]}


@register("inspect_document", group="parse", description="Inspect blocks and skip decisions.", input_schema=_schema())
def inspect_document(args):
    job = _job(args)
    ir = job._load_ir()
    return {"job_id": job.job_id, "data": {"blocks": [block.to_dict() for block in ir.blocks], "metadata": ir.metadata}}


@register("inspect_layout", group="layout", description="Inspect persisted layout geometry if available.", input_schema=_schema())
def inspect_layout(args):
    job = _job(args)
    path = job.agent_dir / "layout_geometry.json"
    return {"job_id": job.job_id, "data": json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"pages": 0, "paragraphs": []}}


@register("dump_text_layer", group="parse", description="Dump the extracted document text layer.", input_schema=_schema())
def dump_text_layer(args):
    job = _job(args)
    path = job.agent_dir / "document.md"
    return {"job_id": job.job_id, "data": {"text": path.read_text(encoding="utf-8") if path.exists() else ""}, "artifacts": [str(path)]}


@register("translate_document", group="translate", description="Translate using an injected provider or safe source fallback.", input_schema=_schema(properties={"provider": {"description": "Python provider object; omitted uses source fallback"}, "translated_md": {"type": "string"}}))
def translate_document(args):
    job = _job(args)
    if args.get("translated_md"):
        source = Path(args["translated_md"])
        if not source.is_file():
            raise FileNotFoundError(source)
        if job.state.value == "created":
            job.parse()
        if job.state.value == "parsed":
            (job.agent_dir / "translated.md").write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
            job._transition(JobState.TRANSLATED)
            job._artifacts(["translated.md"])
    else:
        job.translate(args.get("provider"))
    return {"job_id": job.job_id, "data": {"state": job.state.value}, "warnings": job.manifest.get("warnings", []), "artifacts": [str(job.agent_dir / "translated.md")]}


@register("validate_translation", group="translate", description="Run deterministic translation protocol gates.", input_schema=_schema())
def validate_translation(args):
    job = _job(args)
    report = job.validate_translation()
    return {"job_id": job.job_id, "data": report, "artifacts": [str(job.agent_dir / "protocol_report.json")]}


@register("repair_translation", group="translate", description="Validate and report repair requirements.", input_schema=_schema(properties={"ids": {"type": "array"}}))
def repair_translation(args):
    return retranslate_ids(args)


@register("retranslate_ids", group="translate", description="Replace selected translation IDs in a canonical Markdown file.", input_schema=_schema(properties={"ids": {"type": "array"}, "translated": {"type": "object"}}))
def retranslate_ids(args):
    job = _job(args)
    path = job.agent_dir / "translated.md"
    if not path.exists():
        raise FileNotFoundError(path)
    values = args.get("translated") or {}
    parsed = parse_markdown(path.read_text(encoding="utf-8"))
    for block_id in args.get("ids") or []:
        if block_id in values and block_id in parsed:
            label = parsed[block_id][1] or "text"
            parsed[block_id] = (str(values[block_id]), label)
    lines = ["<!-- babeldoc:translation-protocol v1 -->", ""]
    for block_id, (body, label) in parsed.items():
        lines.extend([f"<!-- id={block_id} label={label or 'text'} -->", body, ""])
    path.write_text("\n".join(lines), encoding="utf-8")
    return {"job_id": job.job_id, "data": {"ids": args.get("ids") or [], "translated_md": str(path)}, "artifacts": [str(path)]}


@register("apply_translation", group="translate", description="Apply a validated translation to job IR.", input_schema=_schema())
def apply_translation(args):
    job = _job(args)
    report = job.validate_translation()
    return {"job_id": job.job_id, "data": report, "artifacts": [str(job.agent_dir / "translated.jsonl")]}


@register("review_protocol", group="review", description="Review protocol gate and return findings.", input_schema=_schema())
def review_protocol(args):
    job = _job(args)
    report = job.validate_translation() if job.state.value == "translated" else json.loads((job.agent_dir / "protocol_report.json").read_text(encoding="utf-8")) if (job.agent_dir / "protocol_report.json").exists() else {"ok": True, "violations": []}
    return {"job_id": job.job_id, "data": report}


@register("review_fidelity", group="review", description="Run semantic reviewer provider when supplied.", input_schema=_schema())
def review_fidelity(args):
    job = _job(args)
    job.review(args.get("provider"))
    return {"job_id": job.job_id, "data": json.loads((job.agent_dir / "review_verdict.json").read_text(encoding="utf-8")), "artifacts": [str(job.agent_dir / "review_verdict.json")]}


@register("review_layout", group="review", description="Review layout geometry and lint findings.", input_schema=_schema())
def review_layout(args):
    return layout_lint(args)


@register("backtranslate_check", group="review", description="Placeholder-safe local backtranslation check.", input_schema=_schema())
def backtranslate_check(args):
    job = _job(args)
    return {"job_id": job.job_id, "data": {"checked": False, "reason": "provider not configured"}, "warnings": ["provider_not_configured"]}


@register("reconstruct_pdf", group="layout", description="Reconstruct a PDF through an optional layout provider.", input_schema=_schema())
def reconstruct_pdf(args):
    job = _job(args)
    job.reconstruct(args.get("provider"))
    return {"job_id": job.job_id, "data": {"output": str(job.workdir / "translated.pdf")}, "artifacts": [str(job.workdir / "translated.pdf")]}


@register("render_pages", group="layout", description="Render representative pages and persist render metadata.", input_schema=_schema(properties={"pages": {"type": ["string", "array"]}}))
def render_pages(args):
    job = _job(args)
    job.render(args.get("pages", "representative"))
    return {"job_id": job.job_id, "data": {"state": job.state.value}, "artifacts": [str(job.agent_dir / "rendered" / "manifest.json")]}


@register("layout_lint", group="layout", description="Read layout lint findings.", input_schema=_schema())
def layout_lint(args):
    legacy = _legacy_agent(args)
    if legacy is not None:
        path = legacy / "layout_lint.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"findings": [], "summary": {"total": 0}}
        data.setdefault("summary", {}).setdefault("total", len(data.get("findings", [])))
        return {"data": data}
    job = _job(args)
    path = job.agent_dir / "layout_lint.json"
    return {"job_id": job.job_id, "data": json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"findings": [], "summary": {"total": 0}}}


@register("layout_locate", group="layout", description="Locate paragraph IDs near a page/box.", input_schema=_schema(properties={"page": {"type": "integer"}, "box": {"type": "array"}}))
def layout_locate(args):
    legacy = _legacy_agent(args)
    if legacy is not None:
        geometry_path = legacy / "layout_geometry.json"
        geometry = json.loads(geometry_path.read_text(encoding="utf-8")) if geometry_path.exists() else {}
        candidates = [item for item in geometry.get("paragraphs", []) if not args.get("page") or item.get("page") == args["page"]]
        return {"data": {"candidates": candidates}}
    job = _job(args)
    geometry = json.loads((job.agent_dir / "layout_geometry.json").read_text(encoding="utf-8")) if (job.agent_dir / "layout_geometry.json").exists() else {}
    candidates = [item for item in geometry.get("paragraphs", []) if not args.get("page") or item.get("page") == args["page"]]
    return {"job_id": job.job_id, "data": {"candidates": candidates}}


@register("layout_patch", group="layout", description="Persist constrained layout overrides.", input_schema=_schema(properties={"patch": {"type": "object"}, "reason": {"type": "string"}, "finding_id": {"type": "string"}, "round": {"type": "integer"}, "render_verified": {"type": "boolean"}, "clear": {"type": "boolean"}}))
def layout_patch(args):
    legacy = _legacy_agent(args)
    if legacy is not None:
        path = legacy / "layout_overrides.json"
        current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"version": 1, "paragraphs": {}, "pages": {}, "history": []}
        if args.get("clear"):
            current["paragraphs"] = {}
            current["pages"] = {}
        else:
            patch = args.get("patch") or {}
            allowed = {"scale_cap", "font_scale", "line_skip", "box_scale", "box", "force_break_after_text", "force_break_after_offset"}
            for section in ("paragraphs", "pages"):
                for ident, fields in (patch.get(section) or {}).items():
                    if section == "paragraphs":
                        bad = set(fields or {}) - allowed
                        if bad:
                            from babeldoc_core import JobError
                            raise JobError("invalid_patch", f"未知字段: {sorted(bad)}")
                    current.setdefault(section, {}).setdefault(str(ident), {}).update(fields or {})
        current.setdefault("history", []).append({"reason": args.get("reason", ""), "finding_id": args.get("finding_id"), "round": args.get("round"), "render_verified": bool(args.get("render_verified", False)), "patch": args.get("patch") or {}})
        atomic_write_json(path, current)
        diff = {"added": ["paragraphs." + key for key in (args.get("patch") or {}).get("paragraphs", {})], "changed": {}, "removed": []}
        return {"data": {"diff": diff, "overrides": current, "path": str(path)}, "artifacts": [str(path)]}
    job = _job(args)
    path = job.agent_dir / "layout_overrides.json"
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"version": 1, "paragraphs": {}, "pages": {}, "history": []}
    patch = args.get("patch") or {}
    for key in ("paragraphs", "pages"):
        existing.setdefault(key, {}).update(patch.get(key) or {})
    existing.setdefault("history", []).append({"reason": args.get("reason", ""), "finding_id": args.get("finding_id"), "round": args.get("round"), "render_verified": bool(args.get("render_verified", False)), "patch": patch})
    atomic_write_json(path, existing)
    return {"job_id": job.job_id, "data": existing, "artifacts": [str(path)]}


@register("layout_rollback", group="layout", description="Remove current layout overrides and return the backup state.", input_schema=_schema())
def layout_rollback(args):
    job = _job(args)
    path = job.agent_dir / "layout_overrides.json"
    atomic_write_json(path, {"version": 1, "paragraphs": {}, "pages": {}, "history": [{"reason": "rollback", "patch": {"__clear__": True}}]})
    return {"job_id": job.job_id, "data": {"rolled_back": True}, "artifacts": [str(path)]}


def _snapshot_dir(job, name):
    return job.agent_dir / "snapshots" / name


@register("job_snapshot", group="version", description="Snapshot recoverable job artifacts.", input_schema=_schema(properties={"name": {"type": "string"}}))
def job_snapshot(args):
    return snapshot(args)


@register("snapshot", group="version", description="Snapshot recoverable artifacts.", input_schema=_schema(properties={"name": {"type": "string"}}))
def snapshot(args):
    legacy = _legacy_agent(args)
    if legacy is not None:
        name = args.get("name") or "latest"
        target = legacy / "snapshots" / name
        target.mkdir(parents=True, exist_ok=True)
        copied = []
        for filename in ("translated.jsonl", "translated.md", "layout_overrides.json", "apply_report.json", "review_verdict.json"):
            source = legacy / filename
            if source.exists():
                shutil.copy2(source, target / filename)
                copied.append(filename)
        atomic_write_json(target / "meta.json", {"name": name, "files": copied})
        return {"data": {"name": name, "files": copied}, "artifacts": [str(target)]}
    job = _job(args)
    name = args.get("name") or "latest"
    target = _snapshot_dir(job, name)
    target.mkdir(parents=True, exist_ok=True)
    copied = []
    for source in (job.agent_dir / "translated.md", job.agent_dir / "translated.jsonl", job.agent_dir / "layout_overrides.json", job.agent_dir / "review_verdict.json"):
        if source.exists():
            shutil.copy2(source, target / source.name)
            copied.append(source.name)
    atomic_write_json(target / "meta.json", {"name": name, "files": copied})
    return {"job_id": job.job_id, "data": {"name": name, "files": copied}, "artifacts": [str(target)]}


@register("job_restore", group="version", description="Restore a named job snapshot.", input_schema=_schema(properties={"name": {"type": "string"}, "apply": {"type": "boolean"}}))
def job_restore(args):
    return restore(args)


@register("restore", group="version", description="Restore a named snapshot.", input_schema=_schema(properties={"name": {"type": "string"}, "apply": {"type": "boolean"}}))
def restore(args):
    legacy = _legacy_agent(args)
    if legacy is not None:
        source = legacy / "snapshots" / args["name"]
        if not source.is_dir():
            raise FileNotFoundError(source)
        restored = []
        for item in source.iterdir():
            if item.name == "meta.json":
                continue
            shutil.copy2(item, legacy / item.name)
            restored.append(item.name)
        return {"data": {"restored": restored}}
    job = _job(args)
    source = _snapshot_dir(job, args["name"])
    if not source.is_dir():
        raise FileNotFoundError(source)
    restored = []
    for item in source.iterdir():
        if item.name == "meta.json":
            continue
        shutil.copy2(item, job.agent_dir / item.name)
        restored.append(item.name)
    return {"job_id": job.job_id, "data": {"restored": restored}}


@register("list_snapshots", group="version", description="List named snapshots.", input_schema=_schema())
def list_snapshots(args):
    legacy = _legacy_agent(args)
    if legacy is not None:
        directory = legacy / "snapshots"
        names = []
        if directory.exists():
            names = [{"name": item.name} for item in sorted(directory.iterdir()) if item.is_dir()]
        return {"data": {"snapshots": names}}
    job = _job(args)
    directory = job.agent_dir / "snapshots"
    return {"job_id": job.job_id, "data": {"snapshots": sorted(item.name for item in directory.iterdir()) if directory.exists() else []}}


@register("export_report", group="version", description="Export a concise FINAL_REPORT.md.", input_schema=_schema())
def export_report(args):
    job = _job(args)
    path = job.agent_dir / "FINAL_REPORT.md"
    content = f"# BabelDOC report\n\n- Job: `{job.job_id}`\n- State: `{job.state.value}`\n"
    path.write_text(content, encoding="utf-8")
    # Keep the historical root-level report path as a convenience alias.
    legacy_path = job.workdir / "FINAL_REPORT.md"
    legacy_path.write_text(content, encoding="utf-8")
    job._artifacts(["FINAL_REPORT.md"])
    return {"job_id": job.job_id, "data": {"report": str(path)}, "artifacts": [str(path), str(legacy_path)]}


# Legacy aliases retained for callers of the pre-productized skill.
@register("review_document", group="review", description="Legacy alias for review_fidelity.", input_schema=_schema())
def review_document(args):
    return review_fidelity(args)


@register("layout_set", group="layout", description="Legacy alias for layout_patch.", input_schema=_schema(properties={"patch": {"type": "object"}, "reason": {"type": "string"}, "finding_id": {"type": "string"}, "round": {"type": "integer"}, "render_verified": {"type": "boolean"}, "clear": {"type": "boolean"}}))
def layout_set(args):
    return layout_patch(args)


@register("report", group="version", description="Legacy alias for export_report.", input_schema=_schema())
def report(args):
    return export_report(args)
