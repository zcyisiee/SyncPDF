"""定期清理（启动时 + 每小时）：只删可重建缓存与过期调试产物，从不删原文与译文。"""

from __future__ import annotations

import time
from pathlib import Path

from babeldoc_tools.cloud.db import Database
from babeldoc_tools.cloud.db import Paths

__all__ = ["LIMITS", "cleanup", "evict"]

DAY = 24 * 3600
LIMITS = {
    "events_days": 7,
    "dual_hours": 24,
    "dual_bytes": 2 << 30,
    "preview_bytes": 5 << 30,
    "tmp_days": 1,
    "orphan_source_days": 30,
}


def evict(directory: Path, limit: int) -> int:
    """总量超过 ``limit`` 时按 mtime 从旧到新删文件；返回删掉的个数。"""
    files = [(p.stat().st_mtime, p.stat().st_size, p) for p in directory.rglob("*") if p.is_file()]
    total = sum(size for _, size, _ in files)
    removed = 0
    for _, size, path in sorted(files):
        if total <= limit:
            break
        path.unlink(missing_ok=True)
        total -= size
        removed += 1
    for sub in sorted((p for p in directory.rglob("*") if p.is_dir()), reverse=True):
        if not any(sub.iterdir()):
            sub.rmdir()
    return removed


def _older(paths, seconds: float, at: float) -> list[Path]:
    return [p for p in paths if p.exists() and at - p.stat().st_mtime > seconds]


def cleanup(db: Database, paths: Paths, limits: dict | None = None) -> dict:
    """残留 workdir 不在这里删：只有启动时（:meth:`Runner.recover`）确定没有运行中的引擎。"""
    limits = {**LIMITS, **(limits or {})}
    at = time.time()
    report = {}
    events = _older(paths.translations.glob("*/events.jsonl.gz"), limits["events_days"] * DAY, at)
    for path in events:
        path.unlink()
    report["events"] = len(events)
    duals = _older(paths.dual.glob("*.pdf"), limits["dual_hours"] * 3600, at)
    for path in duals:
        path.unlink()
    report["dual"] = len(duals) + evict(paths.dual, limits["dual_bytes"])
    report["preview"] = evict(paths.preview, limits["preview_bytes"])
    tmp = _older(paths.tmp.iterdir(), limits["tmp_days"] * DAY, at)
    for path in tmp:
        path.unlink(missing_ok=True)
    report["tmp"] = len(tmp)
    # 第一版只统计：没有任何未删除 job 引用且超过 30 天的原文，不自动删。
    report["orphan_sources"] = db.one(
        "SELECT COUNT(*) FROM sources WHERE created_at < ? AND sha256 NOT IN ("
        "SELECT t.source_sha FROM translations t JOIN jobs j ON j.translation_id = t.id "
        "WHERE j.deleted_at IS NULL)",
        (at - limits["orphan_source_days"] * DAY,),
    )[0]
    return report
