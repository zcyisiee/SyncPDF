"""Parallel local previews decoupled from provider stdout consumption.

Completed blocks are dispatched immediately to a worker pool. Blocks on the
same PDF page hash to the same worker, so page patch state stays serial
(no lost updates between concurrent compiles of one page); different pages
compile in parallel up to the configured worker count.
"""

from __future__ import annotations

import os
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from babeldoc_tools.serve.block_compile import BlockCompiler
from babeldoc_tools.serve.jobs import JobRecord
from babeldoc_tools.serve.store import DocumentStore

#: 并行预览编译 worker 数上限：xelatex 是 CPU 密集进程，8 已是单机上限。
MAX_PREVIEW_WORKERS = 8

#: ``P<page>-<seq>``（``markdown_view._deterministic_ids``）：页号是路由键。
_PID_PAGE_RE = re.compile(r"^P(\d+)-")


def preview_workers_from_environ(environ=None) -> int:
    """解析 ``BDT_SERVE_PREVIEW_WORKERS``：缺省 8，非法值夹到 [1, 8]。"""
    raw = (environ or os.environ).get("BDT_SERVE_PREVIEW_WORKERS")
    if raw is None or not str(raw).strip():
        return MAX_PREVIEW_WORKERS
    try:
        value = int(str(raw).strip())
    except ValueError:
        return MAX_PREVIEW_WORKERS
    return max(1, min(MAX_PREVIEW_WORKERS, value))


def _page_of(pid: str) -> int | None:
    match = _PID_PAGE_RE.match(pid)
    return int(match.group(1)) if match else None


def _lock_index(pid: str, workers: int) -> int:
    """页号 → worker 槽位：同页恒同槽（串行），不同页尽量分散。"""
    page = _page_of(pid)
    key = page if page is not None else hash(pid)
    return key % workers


class ServeStreamPreview:
    def __init__(self, workdir, recorder):
        self.workdir = workdir
        self.recorder = recorder
        self.did = os.environ["BDT_SERVE_DOCUMENT"]
        self.job_id = os.environ["BDT_SERVE_JOB"]
        self.revision = int(os.environ["BDT_SERVE_REVISION"])
        self.store = DocumentStore.for_root(
            Path(os.environ["BDT_SERVE_DATABASE"]).parent
        )
        self.compiler = BlockCompiler(self.store, None)
        self.workers = preview_workers_from_environ()
        self.pool = ThreadPoolExecutor(
            max_workers=self.workers, thread_name_prefix="block-preview"
        )
        # Per-worker page locks: blocks on the same PDF page serialize their
        # read-modify-write of the page patch state; different pages never contend.
        self.page_locks = [threading.Lock() for _ in range(self.workers)]
        self.pending = []
        self.page_revisions = {}
        self.page_records = {}
        self.published = set()

    def _lock_for(self, pid: str) -> threading.Lock:
        return self.page_locks[_lock_index(pid, self.workers)]

    def submit(self, pid, _body, _label):
        self.pending.append(self.pool.submit(self._compile, pid))

    def _compile(self, pid):
        with self._lock_for(pid):
            self._compile_locked(pid)

    def _compile_locked(self, pid):
        database = self.store.database
        raw = next(
            (row for row in database.job_snapshots() if row["job_id"] == self.job_id),
            None,
        )
        if (
            raw is None
            or raw.get("status") != "running"
            or raw.get("cancel_requested_at")
        ):
            return
        record = JobRecord.model_validate(raw)
        record.paragraph_id = pid
        record.revision = self.revision
        try:
            result = self.compiler.compile_block_patch(record)
            page = result["page"]
            self.page_records[page] = record
            self.page_revisions[page] = self.page_revisions.get(page, 0.0) + result["duration_s"]
            if self._page_complete(page) and page not in self.published:
                self.compiler.compose_page_asset(record, page, complete=True, duration_s=self.page_revisions[page])
                self.published.add(page)
        except Exception as exc:
            # Preview failure is visible and retryable; it does not corrupt provider
            # output or turn a partially built PDF into a successful export.
            database.append_event(
                self.job_id,
                self.did,
                "preview_failed",
                {"paragraph_id": pid, "message": str(exc)},
                block_id=pid,
            )

    def _page_complete(self, page):
        database = self.store.database
        with database._lock:
            row = database.connection.execute(
                "SELECT COUNT(b.id), COUNT(tb.block_id), COUNT(cb.block_id) "
                "FROM blocks b "
                "LEFT JOIN translation_blocks tb ON tb.document_id=b.document_id "
                "AND tb.block_id=b.id AND tb.job_id=? AND tb.revision=? "
                "LEFT JOIN compile_blocks cb ON cb.document_id=b.document_id "
                "AND cb.block_id=b.id AND cb.status='ok' "
                "WHERE b.document_id=? AND b.page=?",
                (self.job_id, self.revision, self.did, page),
            ).fetchone()
        return bool(row and row[0] > 0 and row[0] == row[1] == row[2])

    def close(self, *, failed=False):
        self.pool.shutdown(wait=True, cancel_futures=failed)
        for future in self.pending:
            if not future.cancelled():
                future.result()
        try:
            if not failed:
                for page, record in self.page_records.items():
                    if page not in self.published:
                        self.compiler.compose_page_asset(record, page, complete=False, duration_s=self.page_revisions[page])
                if self.page_records:
                    self.compiler.compose_full_preview(next(iter(self.page_records.values())))
        except Exception as exc:
            self.store.database.append_event(self.job_id, self.did, "preview_failed", {"message": str(exc)})
        finally:
            self.store.database.close()
