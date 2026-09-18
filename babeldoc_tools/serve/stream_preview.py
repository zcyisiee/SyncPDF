"""Serialized local previews decoupled from provider stdout consumption."""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from babeldoc_tools.serve.block_compile import BlockCompiler
from babeldoc_tools.serve.jobs import JobRecord
from babeldoc_tools.serve.store import DocumentStore


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
        self.pool = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="block-preview"
        )
        self.pending = []

    def submit(self, pid, _body, _label):
        self.pending.append(self.pool.submit(self._compile, pid))

    def _compile(self, pid):
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
            self.compiler.compile(record)
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

    def close(self, *, failed=False):
        self.pool.shutdown(wait=True, cancel_futures=failed)
        for future in self.pending:
            if not future.cancelled():
                future.result()
        self.store.database.close()
