"""Idempotent import of current document state, excluding historical logs."""

from __future__ import annotations

import json
import pickle
import tempfile
import zipfile
from pathlib import Path

from babeldoc_tools.serve.asset_store import AssetStore
from babeldoc_tools.serve.draft import read_draft
from babeldoc_tools.serve.store import DocumentStore
from babeldoc_tools.serve.workdir import WorkdirReader

# Only current parser inputs; never walk a workdir recursively into debug/cache.
SNAPSHOT_FILES = (
    "state.pkl",
    "anchors.json",
    "document.md",
    "translated.md",
    "translated.jsonl",
    "layout_geometry.json",
    "paragraphs.jsonl",
    "styles.json",
    "formulas.jsonl",
    "layout_overrides.json",
    "formula_latex.json",
    "manifest.json",
    "source/provider/provider_ir.json",
    "source/mineru/provider_ir.json",
)


def migrate_root(
    root: Path | str, *, document_ids: list[str] | None = None
) -> dict[str, object]:
    base = Path(root).expanduser().resolve()
    store = DocumentStore.for_root(base)
    database = store.database
    assets = AssetStore(base, database)
    migrated, skipped, warnings = [], [], []
    temporary_root = base / "tmp"
    temporary_root.mkdir(exist_ok=True)
    try:
        for did in document_ids if document_ids is not None else store.list_dids():
            workdir = store.resolve(did)
            source = workdir / "source.pdf"
            if not source.is_file():
                skipped.append({"did": did, "reason": "source_missing"})
                continue
            try:
                import pymupdf

                with pymupdf.open(source) as pdf:
                    if pdf.page_count < 1:
                        raise ValueError("empty source PDF")
                digest = assets.put(source, kind="source")
                existing = database.document_by_hash(digest)
                if existing is not None and existing["id"] != did:
                    skipped.append(
                        {
                            "did": did,
                            "reason": "duplicate_pdf",
                            "document_id": existing["id"],
                        }
                    )
                    continue
                database.register_document(
                    did,
                    digest,
                    source.stat().st_size,
                    assets.resolve(digest).relative_to(base).as_posix(),
                )
                # 论文标题/一作：`--migrate` 也是老文档的回填入口（与读路径的懒回填
                # 同一实现，`set_paper_meta` 只填空，重复跑不会把已有值抹成 NULL）。
                from babeldoc_tools.serve.paper_meta import resolve_paper_meta

                resolve_paper_meta(database, WorkdirReader(workdir), did)
                draft = read_draft(workdir)
                if database.draft(did) is None:
                    database.save_draft(
                        did, draft.model_dump(), expected_revision=draft.revision
                    )
                from babeldoc_tools.common import ToolError
                from babeldoc_tools.serve.views import paragraphs

                try:
                    rows = [
                        item.model_dump()
                        for item in paragraphs(WorkdirReader(workdir), None)
                    ]
                except ToolError as exc:
                    warnings.append({"did": did, "reason": exc.code})
                    rows = []
                database.upsert_blocks(did, rows)
                with database._lock, database.connection:
                    for row in rows:
                        if row.get("target") is not None:
                            database.connection.execute(
                                "INSERT OR IGNORE INTO translation_blocks VALUES (?,?,?,?,?)",
                                (
                                    did,
                                    row["id"],
                                    "migration",
                                    draft.revision,
                                    row["target"],
                                ),
                            )
                state_path = workdir / "agent/state.pkl"
                if state_path.is_file():
                    # Trusted local parser artifact. Snapshot contains rebased paths,
                    # leaving the original workdir untouched for CLI compatibility.
                    with state_path.open("rb") as handle:
                        state = pickle.load(handle)  # noqa: S301
                    from babeldoc.tools.agent.prepared_pdf import resolve_source_pdf

                    prepared = resolve_source_pdf(state, workdir)
                    if prepared is None:
                        warnings.append(
                            {
                                "did": did,
                                "reason": "prepared_pdf_missing; reparse required",
                            }
                        )
                    else:
                        prepared_asset = assets.put(prepared, kind="prepared")
                        state["pdf_path"] = "source.pdf"
                        state["temp_pdf_path"] = "prepared.pdf"
                        with tempfile.TemporaryDirectory(
                            dir=temporary_root, prefix="migration-"
                        ) as folder:
                            snapshot = Path(folder) / "snapshot.zip"
                            with zipfile.ZipFile(
                                snapshot, "w", compression=zipfile.ZIP_DEFLATED
                            ) as archive:
                                archive.writestr("agent/state.pkl", pickle.dumps(state))
                                for name in SNAPSHOT_FILES:
                                    file = workdir / "agent" / name
                                    if (
                                        name != "state.pkl"
                                        and file.is_file()
                                        and not file.is_symlink()
                                    ):
                                        archive.write(file, f"agent/{name}")
                            snapshot_asset = assets.put(
                                snapshot, kind="parse_snapshot", extension="zip"
                            )
                        with database._lock, database.connection:
                            database.connection.execute(
                                "INSERT OR REPLACE INTO parse_results(document_id,parser_version,status,snapshot_asset,prepared_pdf_asset) VALUES (?,'legacy-current','ready',?,?)",
                                (did, snapshot_asset, prepared_asset),
                            )
                # Only import a current PDF that the PDF parser can actually open.
                outputs = sorted((workdir / "output").glob("*.mono.pdf"))
                if outputs:
                    with pymupdf.open(outputs[0]) as pdf:
                        if pdf.page_count < 1:
                            raise ValueError("empty export")
                    exported = assets.put(outputs[0], kind="export")
                    with database._lock, database.connection:
                        exists = database.connection.execute(
                            "SELECT 1 FROM exports WHERE document_id=?", (did,)
                        ).fetchone()
                        if not exists:
                            # A legacy PDF has no proof it includes unsaved draft changes.
                            status = "previous" if draft.paragraphs else "ok"
                            database.connection.execute(
                                "INSERT INTO exports(document_id,asset_sha256,revision,status) VALUES (?,?,?,?)",
                                (did, exported, draft.revision, status),
                            )
                migrated.append(did)
            except Exception as exc:
                skipped.append(
                    {"did": did, "reason": type(exc).__name__, "message": str(exc)}
                )
        result = {
            "migrated": migrated,
            "skipped": skipped,
            "warnings": warnings,
            "database": str(database.path),
        }
        (temporary_root / "migration-report.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return result
    finally:
        database.close()
