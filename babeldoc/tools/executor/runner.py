from __future__ import annotations

import logging
import multiprocessing
import shutil
import threading
import time
from collections.abc import Callable
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from babeldoc.tools.executor.protocol import WorkerEvent

ProcessTarget = Callable[[dict[str, Any], Any, Any], None]
logger = logging.getLogger(__name__)


def _configure_child_logging() -> None:
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    handler = logging.StreamHandler()
    handler.setFormatter(formatter)

    logging.basicConfig(level=logging.WARNING, force=True)

    executor_logger = logging.getLogger("babeldoc.tools.executor")
    executor_logger.handlers.clear()
    executor_logger.addHandler(handler)
    executor_logger.setLevel(logging.INFO)
    executor_logger.propagate = False


def _run_process_target(
    target: ProcessTarget,
    request: dict[str, Any],
    progress_send,
    cancel_recv,
) -> None:
    _configure_child_logging()
    task_id = _task_id(request)
    started_at = time.monotonic()
    logger.info("executor subprocess target starting: task_id=%s", task_id)
    try:
        target(request, progress_send, cancel_recv)
    finally:
        elapsed = time.monotonic() - started_at
        logger.info(
            "executor subprocess target exited: task_id=%s elapsed=%.3fs",
            task_id,
            elapsed,
        )


def _forkserver_warmup_target() -> None:
    return


class ExecutionRunner:
    def run(
        self,
        request: dict[str, Any],
        emit: Callable[[WorkerEvent], None],
        abort_event: threading.Event,
    ) -> None:
        raise NotImplementedError


class UnavailableRunner(ExecutionRunner):
    def run(
        self,
        request: dict[str, Any],
        emit: Callable[[WorkerEvent], None],
        abort_event: threading.Event,
    ) -> None:
        emit(
            WorkerEvent(
                "error",
                {
                    "message": "executor runner is not configured",
                    "message_for_user": None,
                    "code": "runner_not_configured",
                },
            )
        )


class FakeExecutionRunner(ExecutionRunner):
    def run(
        self,
        request: dict[str, Any],
        emit: Callable[[WorkerEvent], None],
        abort_event: threading.Event,
    ) -> None:
        if request.get("mode") == "block":
            abort_event.wait(timeout=30)
            return
        if request.get("mode") == "burst":
            emit(WorkerEvent("progress", {"index": 1}))
            emit(WorkerEvent("progress", {"index": 2}))
            emit(
                WorkerEvent(
                    "result",
                    {
                        "files": {"dual_pdf": "dual.pdf"},
                        "metrics": {},
                        "usage": None,
                    },
                )
            )
            return

        input_path, output_dir = self._resolve_io(request)
        output_dir.mkdir(parents=True, exist_ok=True)
        emit(
            WorkerEvent(
                "progress",
                {
                    "type": "progress_update",
                    "stage": "fake_executor",
                    "overall_progress": 10,
                },
            )
        )
        if abort_event.wait(timeout=0.05):
            return

        files = {
            "dual": output_dir / "translated_dual.pdf",
            "mono": output_dir / "translated_mono.pdf",
            "no_watermark_dual": output_dir / "translated_dual_no_watermark.pdf",
            "no_watermark_mono": output_dir / "translated_mono_no_watermark.pdf",
        }
        for path in files.values():
            shutil.copyfile(input_path, path)

        auto_glossary = output_dir / "auto_extracted_glossary.csv"
        auto_glossary.write_text("source,target\n", encoding="utf-8")

        emit(
            WorkerEvent(
                "progress",
                {
                    "type": "progress_update",
                    "stage": "fake_executor",
                    "overall_progress": 90,
                },
            )
        )
        if abort_event.wait(timeout=0.05):
            return

        emit(
            WorkerEvent(
                "result",
                {
                    "files": {
                        "dual_pdf": str(files["dual"]),
                        "mono_pdf": str(files["mono"]),
                        "dual_no_watermark_pdf": str(files["no_watermark_dual"]),
                        "mono_no_watermark_pdf": str(files["no_watermark_mono"]),
                        "auto_extracted_glossary_csv": str(auto_glossary),
                    },
                    "metrics": {
                        "pdf_total_char_count": input_path.stat().st_size,
                        "pdf_total_char_token_count": 0,
                        "peak_memory_usage": 0,
                        "time_consume_seconds": 0.1,
                    },
                    "usage": {},
                },
            )
        )

    @staticmethod
    def _resolve_io(request: dict[str, Any]) -> tuple[Path, Path]:
        paths = request.get("paths")
        if not isinstance(paths, dict):
            raise ValueError("paths must be an object")

        input_value = paths.get("input_file")
        output_value = paths.get("output_dir")
        if not isinstance(input_value, str) or not input_value:
            raise ValueError("paths.input_file is required")
        if not isinstance(output_value, str) or not output_value:
            raise ValueError("paths.output_dir is required")

        input_path = Path(input_value)
        output_dir = Path(output_value)
        if not input_path.is_absolute():
            input_path = Path.cwd() / input_path
        if not output_dir.is_absolute():
            output_dir = Path.cwd() / output_dir
        if not input_path.is_file():
            raise FileNotFoundError(str(input_path))
        return input_path, output_dir


class MultiprocessExecutionRunner(ExecutionRunner):
    def __init__(
        self,
        target: ProcessTarget,
        *,
        start_method: str = "spawn",
        preload_modules: Iterable[str] = (),
        poll_seconds: float = 0.05,
        join_timeout_seconds: float = 1.0,
    ):
        self._target = target
        self._start_method = start_method
        self._preload_modules = tuple(preload_modules)
        if start_method == "forkserver" and self._preload_modules:
            multiprocessing.set_forkserver_preload(list(self._preload_modules))
        self._context = multiprocessing.get_context(start_method)
        self._poll_seconds = poll_seconds
        self._join_timeout_seconds = join_timeout_seconds

    def warmup(self) -> None:
        if self._start_method != "forkserver":
            return

        started_at = time.monotonic()
        process = self._context.Process(target=_forkserver_warmup_target)
        process.start()
        process.join()
        elapsed = time.monotonic() - started_at
        if process.exitcode != 0:
            raise RuntimeError(
                f"executor forkserver warmup failed: exit_code={process.exitcode}"
            )
        logger.info(
            "executor forkserver warmup completed: elapsed=%.3fs preload_modules=%s",
            elapsed,
            ",".join(self._preload_modules) or "none",
        )

    def run(
        self,
        request: dict[str, Any],
        emit: Callable[[WorkerEvent], None],
        abort_event: threading.Event,
    ) -> None:
        task_id = _task_id(request)
        progress_recv, progress_send = self._context.Pipe(duplex=False)
        cancel_recv, cancel_send = self._context.Pipe(duplex=False)
        process = self._context.Process(
            target=_run_process_target,
            args=(self._target, request, progress_send, cancel_recv),
        )
        started_at = time.monotonic()
        process.start()
        logger.info(
            "executor subprocess started: task_id=%s pid=%s start_elapsed=%.3fs",
            task_id,
            process.pid,
            time.monotonic() - started_at,
        )
        progress_send.close()
        cancel_recv.close()

        terminal_seen = False
        try:
            while True:
                if abort_event.is_set():
                    logger.warning(
                        "executor subprocess cancellation requested: task_id=%s pid=%s",
                        task_id,
                        process.pid,
                    )
                    self._send_cancel(cancel_send)
                    return

                if progress_recv.poll(self._poll_seconds):
                    try:
                        item = progress_recv.recv()
                    except EOFError:
                        process.join(timeout=0)
                        if not terminal_seen:
                            logger.error(
                                "executor subprocess pipe closed before terminal event: task_id=%s pid=%s exit_code=%s",
                                task_id,
                                process.pid,
                                process.exitcode,
                            )
                            self._emit_missing_terminal_error(process.exitcode, emit)
                        return
                    if item is None:
                        if not terminal_seen:
                            process.join(timeout=0.2)
                            logger.error(
                                "executor subprocess ended before terminal event: task_id=%s pid=%s exit_code=%s",
                                task_id,
                                process.pid,
                                process.exitcode,
                            )
                            self._emit_missing_terminal_error(process.exitcode, emit)
                        return

                    event = self._coerce_event(item)
                    emit(event)
                    if event.type in {"result", "error"}:
                        terminal_seen = True
                        if event.type == "result":
                            logger.info(
                                "executor subprocess emitted terminal result: task_id=%s pid=%s",
                                task_id,
                                process.pid,
                            )
                        else:
                            logger.warning(
                                "executor subprocess emitted terminal error: task_id=%s pid=%s code=%s message=%s",
                                task_id,
                                process.pid,
                                event.payload.get("code"),
                                event.payload.get("message"),
                            )
                        return
                    continue

                if not process.is_alive():
                    process.join(timeout=0)
                    terminal_seen = self._drain_progress(
                        progress_recv, emit, terminal_seen
                    )
                    if not terminal_seen:
                        logger.error(
                            "executor subprocess exited before terminal event: task_id=%s pid=%s exit_code=%s",
                            task_id,
                            process.pid,
                            process.exitcode,
                        )
                        self._emit_missing_terminal_error(process.exitcode, emit)
                    return
        finally:
            self._send_cancel(cancel_send)
            cancel_send.close()
            progress_recv.close()
            self._stop_process(process)

    @staticmethod
    def _coerce_event(item: Any) -> WorkerEvent:
        if isinstance(item, WorkerEvent):
            return item
        if isinstance(item, dict):
            event_type = item.get("type")
            payload = item.get("payload", item.get("data"))
            if isinstance(event_type, str) and isinstance(payload, dict):
                return WorkerEvent(event_type, payload)
        raise ValueError("subprocess emitted an invalid executor event")

    def _drain_progress(
        self,
        progress_recv: Any,
        emit: Callable[[WorkerEvent], None],
        terminal_seen: bool,
    ) -> bool:
        while not terminal_seen and progress_recv.poll():
            try:
                item = progress_recv.recv()
            except EOFError:
                return terminal_seen
            if item is None:
                return terminal_seen
            event = self._coerce_event(item)
            emit(event)
            terminal_seen = event.type in {"result", "error"}
        return terminal_seen

    @staticmethod
    def _send_cancel(cancel_send: Any) -> None:
        try:
            cancel_send.send(True)
        except (BrokenPipeError, EOFError, OSError):
            return

    @staticmethod
    def _emit_missing_terminal_error(
        exitcode: int | None,
        emit: Callable[[WorkerEvent], None],
    ) -> None:
        exit_suffix = "unknown" if exitcode is None else str(exitcode)
        emit(
            WorkerEvent(
                "error",
                {
                    "message": (
                        "executor subprocess ended without a terminal "
                        f"event: exit_code={exit_suffix}"
                    ),
                    "message_for_user": None,
                    "code": "missing_terminal_event",
                },
            )
        )

    def _stop_process(self, process: multiprocessing.Process) -> None:
        if not process.is_alive():
            process.join(timeout=0)
            return

        logger.warning(
            "executor subprocess still alive during cleanup; terminating pid=%s",
            process.pid,
        )
        process.terminate()
        process.join(timeout=self._join_timeout_seconds)
        if process.is_alive():
            logger.error(
                "executor subprocess did not terminate; killing pid=%s",
                process.pid,
            )
            process.kill()
            process.join()


def _task_id(request: dict[str, Any]) -> str:
    value = request.get("task_id")
    return value if isinstance(value, str) and value else "unknown"
