"""Parallel local previews decoupled from provider stdout consumption.

Completed blocks are dispatched immediately to a worker pool. Blocks on the
same PDF page hash to the same worker, so page patch state stays serial
(no lost updates between concurrent compiles of one page); different pages
compile in parallel up to the configured worker count.

A page composes (``preview_ready``) as soon as every **in-scope** block on it
has settled. Scope = the paragraph ids in ``agent/document.md`` (the translate
selection, written by parse) — **not** the ``blocks`` table: that index is only
backfilled after a job succeeds (:meth:`JobRunner._sync_metadata`), so it is
empty during a fresh document's first run, and it is the union of *all* native
paragraphs (headers/footers included), which can never equal the translation
scope. Settled means: translated for this job+revision AND compiled ``ok`` —
or recorded ``preview_failed``, so one unsafe/uncompilable block shows the
baseline original instead of starving the whole page forever.
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

#: ``document.md`` 的段落标记注释（``<!-- id=P01-003 label=text -->``）。
_SCOPE_ID_RE = re.compile(r"<!--\s*id=(P\d+-\d+)\b")

#: 编译落定状态：ok（贴片可用）或 preview_failed（该块回退基线原文）。
_SETTLED_STATUSES = ("ok", "preview_failed")


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


def scope_by_page(workdir) -> dict[int, set[str]]:
    """翻译范围按页分组：``agent/document.md`` 的段落 id → ``{page: {pid}}``。

    ``document.md`` 由 parse 在 translate 之前写好，其中的 id 集合就是本次
    翻译会提交的全部块（prompt 与它一一对应）。文件缺失/不可读 → 空 dict
    （退化为永不成页，``close`` 仍会在翻译结束时兜底合成）。
    """
    try:
        text = (Path(workdir) / "agent" / "document.md").read_text(encoding="utf-8")
    except OSError:
        return {}
    scope: dict[int, set[str]] = {}
    for pid in set(_SCOPE_ID_RE.findall(text)):
        page = _page_of(pid)
        if page is not None:
            scope.setdefault(page, set()).add(pid)
    return scope


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
        #: 翻译范围（``agent/document.md`` 的 id），成页判定只看它（见模块 docstring）。
        self._scope_by_page = scope_by_page(workdir)

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
        home = _page_of(pid)
        if home is not None:
            self.page_records.setdefault(home, record)
        landing = None
        try:
            result = self.compiler.compile_block_patch(record)
            landing = result.get("page")
            if home is not None:
                self.page_revisions[home] = (
                    self.page_revisions.get(home, 0.0) + result.get("duration_s", 0.0)
                )
        except Exception as exc:
            # Preview failure is visible and retryable; it does not corrupt provider
            # output or turn a partially built PDF into a successful export. The
            # compile_blocks row marks the block *settled* so the rest of the page
            # can still compose (this block falls back to the baseline original).
            with database._lock, database.connection:
                database.connection.execute(
                    "INSERT OR REPLACE INTO compile_blocks"
                    "(document_id,block_id,input_hash,patch_asset,status,target_size,error)"
                    " VALUES (?,?,NULL,NULL,'preview_failed',NULL,?)",
                    (self.did, pid, str(exc)[:300]),
                )
            database.append_event(
                self.job_id,
                self.did,
                "preview_failed",
                {"paragraph_id": pid, "message": str(exc)},
                block_id=pid,
            )
        for page in ({home, landing} - {None}):
            if page in self.published or not self._page_complete(page):
                continue
            self.compiler.compose_page_asset(
                self.page_records.get(page) or record,
                page,
                complete=True,
                duration_s=self.page_revisions.get(page, 0.0),
            )
            self.published.add(page)

    def _page_complete(self, page):
        """该页所有**在翻译范围内**的块都已提交译文且编译落定（ok 或 preview_failed）。"""
        ids = self._scope_by_page.get(page)
        if not ids:
            return False
        marks = ",".join("?" for _ in ids)
        database = self.store.database
        with database._lock:
            # marks 只是按 id 数量生成的 ? 占位符，值全部走参数绑定（S608 误报）。
            translated, settled = database.connection.execute(
                f"SELECT "  # noqa: S608
                f"(SELECT COUNT(*) FROM translation_blocks WHERE job_id=? AND revision=?"
                f"  AND document_id=? AND block_id IN ({marks})),"
                f"(SELECT COUNT(*) FROM compile_blocks WHERE document_id=?"
                f"  AND block_id IN ({marks}) AND status IN (?,?))",
                (
                    self.job_id,
                    self.revision,
                    self.did,
                    *ids,
                    self.did,
                    *ids,
                    *_SETTLED_STATUSES,
                ),
            ).fetchone()
        return translated == len(ids) and settled == len(ids)

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
