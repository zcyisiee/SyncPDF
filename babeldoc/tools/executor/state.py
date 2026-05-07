from __future__ import annotations

import logging
import secrets
import threading
import uuid
from collections import deque
from collections.abc import Iterable
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from babeldoc.tools.executor.protocol import MAX_EVENT_LOG_SIZE
from babeldoc.tools.executor.protocol import MAX_INITIAL_SEQUENCE
from babeldoc.tools.executor.protocol import MAX_SEQUENCE
from babeldoc.tools.executor.protocol import EventEnvelope
from babeldoc.tools.executor.protocol import WorkerEvent
from babeldoc.tools.executor.runner import ExecutionRunner
from babeldoc.tools.executor.runner import UnavailableRunner

logger = logging.getLogger(__name__)


class ReplayGapError(Exception):
    pass


class ExecutionBusyError(Exception):
    def __init__(self, snapshot: dict[str, Any]):
        super().__init__("executor is busy")
        self.snapshot = snapshot


class ExecutionNotFoundError(Exception):
    pass


TERMINAL_EVENT_TYPES = {"result", "error"}


@dataclass
class ExecutionRecord:
    execution_id: str
    task_id: str
    request: dict[str, Any]
    initial_seq: int
    last_seq: int
    status: str = "active"
    events: deque[EventEnvelope] = field(default_factory=deque)
    abort_event: threading.Event = field(default_factory=threading.Event)
    first_available_seq: int | None = None


class ExecutionStore:
    def __init__(
        self,
        runner: ExecutionRunner | None = None,
        max_event_log_size: int = MAX_EVENT_LOG_SIZE,
    ):
        if max_event_log_size <= 0:
            raise ValueError("max_event_log_size must be positive")
        self._runner = runner or UnavailableRunner()
        self._max_event_log_size = max_event_log_size
        self._lock = threading.RLock()
        self._condition = threading.Condition(self._lock)
        self._current: ExecutionRecord | None = None

    def create(self, request: dict[str, Any]) -> dict[str, Any]:
        task_id = request.get("task_id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("task_id is required")

        with self._lock:
            if self._current is not None and self._current.status == "active":
                logger.warning(
                    "executor create rejected because active execution exists: requested_task_id=%s active_task_id=%s active_execution_id=%s",
                    task_id,
                    self._current.task_id,
                    self._current.execution_id,
                )
                raise ExecutionBusyError(self._snapshot_locked(self._current))

            initial_seq = secrets.randbelow(MAX_INITIAL_SEQUENCE) + 1
            record = ExecutionRecord(
                execution_id=str(uuid.uuid4()),
                task_id=task_id,
                request=request,
                initial_seq=initial_seq,
                last_seq=initial_seq,
            )
            self._current = record

        logger.info(
            "executor execution created: task_id=%s execution_id=%s initial_sequence=%s",
            record.task_id,
            record.execution_id,
            record.initial_seq,
        )
        thread = threading.Thread(
            target=self._run,
            args=(record,),
            name=f"executor-{record.execution_id}",
            daemon=True,
        )
        thread.start()

        return {
            "execution_id": record.execution_id,
            "status": "started",
            "initial_sequence": record.initial_seq,
        }

    def abort_current(self) -> None:
        with self._condition:
            record = self._current
            if record is None:
                self._condition.notify_all()
                return
            logger.warning(
                "executor active execution abort requested: task_id=%s execution_id=%s status=%s",
                record.task_id,
                record.execution_id,
                record.status,
            )
            record.abort_event.set()
            if record.status == "active":
                record.status = "aborted"
            self._condition.notify_all()

    def begin_heavy_operation(self, operation_id: str) -> threading.Event:
        if not operation_id:
            raise ValueError("operation_id is required")
        with self._condition:
            if self._current is not None and self._current.status == "active":
                raise ExecutionBusyError(self._snapshot_locked(self._current))
            initial_seq = secrets.randbelow(MAX_INITIAL_SEQUENCE) + 1
            record = ExecutionRecord(
                execution_id=operation_id,
                task_id=operation_id,
                request={},
                initial_seq=initial_seq,
                last_seq=initial_seq,
            )
            self._current = record
            logger.info(
                "executor heavy operation started: operation_id=%s initial_sequence=%s",
                operation_id,
                initial_seq,
            )
            self._condition.notify_all()
            return record.abort_event

    def finish_heavy_operation(self, operation_id: str) -> None:
        with self._condition:
            if self._current is None or self._current.execution_id != operation_id:
                return
            if self._current.status == "active":
                self._current.status = "completed"
            logger.info(
                "executor heavy operation finished: operation_id=%s status=%s",
                operation_id,
                self._current.status,
            )
            self._condition.notify_all()

    def replay(self, execution_id: str, after_seq: int) -> list[EventEnvelope]:
        with self._lock:
            record = self._require_current_locked(execution_id)
            self._raise_if_gap_locked(record, after_seq)
            return [event for event in record.events if event.sequence > after_seq]

    def stream(
        self,
        execution_id: str,
        after_seq: int,
        wait_seconds: float = 0.25,
    ) -> Iterable[EventEnvelope]:
        cursor = after_seq
        while True:
            with self._condition:
                record = self._require_current_locked(execution_id)
                self._raise_if_gap_locked(record, cursor)
                pending = [event for event in record.events if event.sequence > cursor]
                status = record.status
                if not pending and status == "active":
                    self._condition.wait(timeout=wait_seconds)
                    continue

            for event in pending:
                cursor = event.sequence
                yield event

            if pending and pending[-1].type in TERMINAL_EVENT_TYPES:
                return
            if not pending and status != "active":
                return

    def snapshot(self, execution_id: str) -> dict[str, Any]:
        with self._lock:
            return self._snapshot_locked(self._require_current_locked(execution_id))

    def _run(self, record: ExecutionRecord) -> None:
        def emit(event: WorkerEvent) -> None:
            self._append_event(record.execution_id, event)

        try:
            self._runner.run(record.request, emit, record.abort_event)
            with self._condition:
                if record.status == "active":
                    logger.error(
                        "executor runner returned without terminal event: task_id=%s execution_id=%s",
                        record.task_id,
                        record.execution_id,
                    )
                    self._append_event_locked(
                        record,
                        WorkerEvent(
                            "error",
                            {
                                "code": "missing_terminal_event",
                                "message": (
                                    "executor runner returned without a terminal event"
                                ),
                                "message_for_user": None,
                                "details": {},
                            },
                        ),
                    )
                self._condition.notify_all()
        except Exception as exc:
            logger.exception(
                "executor runner raised internal error: task_id=%s execution_id=%s",
                record.task_id,
                record.execution_id,
            )
            self._append_event(
                record.execution_id,
                WorkerEvent(
                    "error",
                    {
                        "code": "internal_error",
                        "message": str(exc),
                        "message_for_user": None,
                        "details": {"exception_type": exc.__class__.__name__},
                    },
                ),
            )

    def _append_event(self, execution_id: str, event: WorkerEvent) -> None:
        with self._condition:
            record = self._require_current_locked(execution_id)
            self._append_event_locked(record, event)
            self._condition.notify_all()

    def _append_event_locked(
        self,
        record: ExecutionRecord,
        event: WorkerEvent,
    ) -> None:
        if record.status in {"terminal", "aborted"}:
            return
        if record.last_seq >= MAX_SEQUENCE:
            record.abort_event.set()
            raise OverflowError("event sequence exhausted")
        record.last_seq += 1
        if record.last_seq <= 0:
            record.abort_event.set()
            raise OverflowError("event sequence invariant violated")
        envelope = EventEnvelope(
            type=event.type,
            execution_id=record.execution_id,
            sequence=record.last_seq,
            payload=event.payload,
        )
        record.events.append(envelope)
        if record.first_available_seq is None:
            record.first_available_seq = envelope.sequence
        while len(record.events) > self._max_event_log_size:
            record.events.popleft()
            record.first_available_seq = record.events[0].sequence
        if event.type in TERMINAL_EVENT_TYPES and record.status == "active":
            record.status = "terminal"
            if event.type == "result":
                logger.info(
                    "executor terminal result emitted: task_id=%s execution_id=%s sequence=%s",
                    record.task_id,
                    record.execution_id,
                    envelope.sequence,
                )
            else:
                logger.warning(
                    "executor terminal error emitted: task_id=%s execution_id=%s sequence=%s code=%s message=%s",
                    record.task_id,
                    record.execution_id,
                    envelope.sequence,
                    event.payload.get("code"),
                    event.payload.get("message"),
                )

    def _require_current_locked(self, execution_id: str) -> ExecutionRecord:
        if self._current is None or self._current.execution_id != execution_id:
            raise ExecutionNotFoundError(execution_id)
        return self._current

    def _snapshot_locked(self, record: ExecutionRecord) -> dict[str, Any]:
        return {
            "execution_id": record.execution_id,
            "task_id": record.task_id,
            "status": record.status,
            "last_sequence": record.last_seq,
        }

    def _raise_if_gap_locked(self, record: ExecutionRecord, after_seq: int) -> None:
        if record.first_available_seq is None:
            if after_seq < record.initial_seq:
                raise ReplayGapError("requested sequence is no longer available")
            return
        if after_seq < record.first_available_seq - 1:
            raise ReplayGapError("requested sequence is no longer available")
