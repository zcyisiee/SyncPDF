"""Import the current useful state of legacy workdirs into SQLite."""
from __future__ import annotations

from pathlib import Path

from babeldoc_tools.serve.asset_store import AssetStore
from babeldoc_tools.serve.store import DocumentStore
from babeldoc_tools.serve.workdir import WorkdirReader


def migrate_root(root: Path | str) -> dict[str, object]:
    """Migrate source PDFs and readable paragraph views without copying old logs."""
    base = Path(root).expanduser().resolve()
    store = DocumentStore.for_root(base)
    database = store.database
    assets = AssetStore(base, database)
    migrated: list[str] = []
    skipped: list[dict[str, str]] = []
    for did in store.list_dids():
        workdir = store.resolve(did)
        source = workdir / "source.pdf"
        if not source.is_file():
            skipped.append({"did": did, "reason": "source_missing"})
            continue
        try:
            digest = assets.put(source, kind="source")
            database.register_document(
                did, digest, source.stat().st_size,
                str((Path("assets") / digest[:2] / f"{digest}.pdf").as_posix()),
            )
            try:
                from babeldoc_tools.serve.views import paragraphs

                rows = [item.model_dump() for item in paragraphs(WorkdirReader(workdir), None)]
                database.upsert_blocks(did, rows)
            except Exception:
                # A source-only upload is still a valid migrated document.
                pass
            migrated.append(did)
        except OSError as exc:
            skipped.append({"did": did, "reason": type(exc).__name__})
    return {"migrated": migrated, "skipped": skipped, "database": str(database.path)}
