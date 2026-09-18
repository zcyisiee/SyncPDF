"""Conservative cleanup of expired disposable files; assets are never traversed."""

from __future__ import annotations

import time
from pathlib import Path

from babeldoc_tools.common import ToolError
from babeldoc_tools.serve.database import MetadataDB


def cleanup(root: Path, *, max_age_seconds: float = 86400) -> dict:
    root = root.resolve()
    if (root / "app.db").is_file():
        database = MetadataDB(root)
        try:
            if any(
                row["status"] in ("queued", "running")
                for row in database.job_snapshots()
            ):
                raise ToolError("document_busy", "存在活动任务，暂不清理缓存")
        finally:
            database.close()
    removed, freed = 0, 0
    cutoff = time.time() - max_age_seconds
    for name in ("tmp", "cache"):
        directory = root / name
        if not directory.is_dir() or directory.is_symlink():
            continue
        # Never follow symlinks, and restrict every deletion to its disposable root.
        for path in directory.rglob("*"):
            if (
                path.is_symlink()
                or not path.is_file()
                or not path.resolve().is_relative_to(directory)
            ):
                continue
            stat = path.stat()
            if stat.st_mtime >= cutoff:
                continue
            path.unlink()
            removed += 1
            freed += stat.st_size
    return {
        "removed_files": removed,
        "freed_bytes": freed,
        "minimum_age_seconds": max_age_seconds,
    }
