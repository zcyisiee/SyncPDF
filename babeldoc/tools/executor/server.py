from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs
from urllib.parse import urlparse

from babeldoc.tools.executor.runner import FakeExecutionRunner
from babeldoc.tools.executor.runner import MultiprocessExecutionRunner
from babeldoc.tools.executor.state import ExecutionBusyError
from babeldoc.tools.executor.state import ExecutionNotFoundError
from babeldoc.tools.executor.state import ExecutionStore
from babeldoc.tools.executor.state import ReplayGapError
from babeldoc.tools.executor.workroot import get_workroot
from babeldoc.tools.executor.workroot import relative_to_workroot
from babeldoc.tools.executor.workroot import resolve_file
from babeldoc.tools.executor.workroot import resolve_inside_workroot

logger = logging.getLogger(__name__)
WATERMARK_TIMEOUT_SECONDS = 600


class ExecutorServer(ThreadingHTTPServer):
    def __init__(self, server_address, store: ExecutionStore):
        super().__init__(server_address, ExecutorHandler)
        self.store = store


class ExecutorHandler(BaseHTTPRequestHandler):
    server: ExecutorServer

    def do_GET(self):
        if self.path == "/healthz":
            try:
                workroot = get_workroot()
                proof_file = workroot / ".executor-healthz-write-proof"
                proof_file.write_text("ok", encoding="utf-8")
                proof_file.unlink()
            except ValueError as exc:
                self._write_json(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"status": "error", "code": "workroot_unavailable"},
                )
                logger.warning("executor healthz failed: %s", exc)
                return
            self._write_json(HTTPStatus.OK, {"status": "ok"})
            return

        parsed = urlparse(self.path)
        parts = parsed.path.strip("/").split("/")
        if (
            len(parts) == 4
            and parts[:2] == ["v1", "executions"]
            and parts[3] == "events"
        ):
            query = parse_qs(parsed.query)
            try:
                after_seq = int(query["after_sequence"][0])
            except ValueError:
                self._write_error(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_request",
                    "after_sequence must be an integer",
                )
                return
            except (KeyError, IndexError):
                self._write_error(
                    HTTPStatus.BAD_REQUEST,
                    "invalid_request",
                    "after_sequence is required",
                )
                return
            self._stream_events(parts[2], after_seq)
            return

        self._write_error(HTTPStatus.NOT_FOUND, "not_found", "not found")

    def do_POST(self):
        parsed = urlparse(self.path)
        parts = parsed.path.strip("/").split("/")
        if parsed.path == "/v1/executions":
            self._create_execution()
            return

        if parsed.path == "/v1/abort":
            self._abort_current()
            return

        if parsed.path in {"/v1/pdf/watermark1", "/v1/pdf/watermark2"}:
            self._run_watermark(parsed.path.rsplit("/", 1)[-1])
            return

        self._write_error(HTTPStatus.NOT_FOUND, "not_found", "not found")

    def log_message(self, fmt, *args):
        if self.path == "/healthz":
            logger.debug(fmt, *args)
            return
        logger.debug(fmt, *args)

    def _create_execution(self):
        request: dict | None = None
        try:
            request = self._read_json_body()
            response = self.server.store.create(request)
        except json.JSONDecodeError:
            self._write_error(HTTPStatus.BAD_REQUEST, "invalid_request", "invalid json")
            return
        except ValueError as exc:
            self._write_error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            return
        except ExecutionBusyError as exc:
            logger.warning(
                "executor rejected create because it is busy: requested_task_id=%s active_task_id=%s active_execution_id=%s",
                request.get("task_id") if isinstance(request, dict) else None,
                exc.snapshot.get("task_id"),
                exc.snapshot.get("execution_id"),
            )
            self._write_json(
                HTTPStatus.CONFLICT,
                {
                    "code": "busy",
                    "message": "executor is busy",
                    "snapshot": exc.snapshot,
                },
            )
            return

        logger.info(
            "executor accepted task: task_id=%s execution_id=%s initial_sequence=%s",
            request.get("task_id"),
            response.get("execution_id"),
            response.get("initial_sequence"),
        )
        self._write_json(HTTPStatus.CREATED, response)

    def _abort_current(self):
        logger.warning("executor abort requested")
        self.server.store.abort_current()
        self._write_json(HTTPStatus.ACCEPTED, {"status": "aborting"})

    def _run_watermark(self, operation: str) -> None:
        try:
            request = self._read_json_body()
            operation_id, input_file, output_file, asset_files = (
                self._validate_watermark_request(operation, request)
            )
            abort_event = self.server.store.begin_heavy_operation(operation_id)
        except json.JSONDecodeError:
            self._write_error(HTTPStatus.BAD_REQUEST, "invalid_request", "invalid json")
            return
        except FileNotFoundError as exc:
            self._write_error(
                HTTPStatus.BAD_REQUEST,
                "input_missing" if "input_file" in str(exc) else "asset_missing",
                "input or asset file is missing",
            )
            return
        except ValueError as exc:
            self._write_error(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
            return
        except ExecutionBusyError as exc:
            self._write_json(
                HTTPStatus.CONFLICT,
                {
                    "code": "busy",
                    "message": "executor is busy",
                    "snapshot": exc.snapshot,
                },
            )
            return

        logger.info(
            "executor watermark operation started: operation=%s operation_id=%s input=%s output=%s assets=%s",
            operation,
            operation_id,
            relative_to_workroot(get_workroot(), input_file),
            relative_to_workroot(get_workroot(), output_file),
            len(asset_files),
        )
        try:
            self._run_watermark_subprocess(
                operation,
                input_file,
                output_file,
                asset_files,
                abort_event,
            )
            self._write_json(
                HTTPStatus.OK,
                {
                    "operation_id": operation_id,
                    "output_file": relative_to_workroot(get_workroot(), output_file),
                },
            )
            logger.info(
                "executor watermark operation finished: operation=%s operation_id=%s output=%s",
                operation,
                operation_id,
                relative_to_workroot(get_workroot(), output_file),
            )
        except TimeoutError:
            logger.exception(
                "executor watermark operation timed out: operation=%s operation_id=%s",
                operation,
                operation_id,
            )
            self._write_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "transform_timeout",
                "watermark transform timed out",
            )
        except RuntimeError:
            logger.exception(
                "executor watermark operation failed: operation=%s operation_id=%s",
                operation,
                operation_id,
            )
            self._write_error(
                HTTPStatus.INTERNAL_SERVER_ERROR,
                "transform_failed",
                "watermark transform failed",
            )
        finally:
            self.server.store.finish_heavy_operation(operation_id)

    @staticmethod
    def _validate_watermark_request(
        operation: str,
        request: dict,
    ) -> tuple[str, Path, Path, list[Path]]:
        operation_id = request.get("operation_id")
        if not isinstance(operation_id, str) or not operation_id:
            raise ValueError("operation_id is required")
        options = request.get("options")
        if options is not None and options != {}:
            raise ValueError("options must be an empty object")

        workroot = get_workroot()
        input_value = request.get("input_file")
        output_value = request.get("output_file")
        if not isinstance(input_value, str) or not input_value:
            raise ValueError("input_file is required")
        if not isinstance(output_value, str) or not output_value:
            raise ValueError("output_file is required")

        try:
            input_file = resolve_file(workroot, input_value)
        except FileNotFoundError as exc:
            raise FileNotFoundError("input_file") from exc
        asset_files = ExecutorHandler._resolve_watermark_assets(
            workroot,
            operation,
            request,
        )
        output_file = resolve_inside_workroot(workroot, output_value)
        output_file.parent.mkdir(parents=True, exist_ok=True)
        if input_file == output_file:
            raise ValueError("output_file must not overwrite input_file")
        return operation_id, input_file, output_file, asset_files

    @staticmethod
    def _resolve_watermark_assets(
        workroot: Path,
        operation: str,
        request: dict,
    ) -> list[Path]:
        if operation == "watermark1":
            asset_value = request.get("asset_file")
            if not isinstance(asset_value, str) or not asset_value:
                raise ValueError("asset_file is required")
            try:
                return [resolve_file(workroot, asset_value)]
            except FileNotFoundError as exc:
                raise FileNotFoundError("asset_file") from exc

        asset_value_1 = request.get("asset_file_1")
        asset_value_2 = request.get("asset_file_2")
        if not isinstance(asset_value_1, str) or not asset_value_1:
            raise ValueError("asset_file_1 is required")
        if not isinstance(asset_value_2, str) or not asset_value_2:
            raise ValueError("asset_file_2 is required")
        try:
            asset_file_1 = resolve_file(workroot, asset_value_1)
            asset_file_2 = resolve_file(workroot, asset_value_2)
        except FileNotFoundError as exc:
            raise FileNotFoundError("asset_file") from exc
        return [asset_file_1, asset_file_2]

    @staticmethod
    def _run_watermark_subprocess(
        operation: str,
        input_file: Path,
        output_file: Path,
        asset_files: list[Path],
        abort_event,
    ) -> None:
        command = [
            sys.executable,
            "-m",
            "babeldoc.tools.executor.watermark_transform",
            operation,
            "--input",
            str(input_file),
            "--output",
            str(output_file),
        ]
        for asset_file in asset_files:
            command.extend(["--asset", str(asset_file)])
        process = subprocess.Popen(  # noqa: S603
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        deadline = time.monotonic() + WATERMARK_TIMEOUT_SECONDS
        try:
            while True:
                return_code = process.poll()
                if return_code is not None:
                    if return_code != 0:
                        raise RuntimeError("watermark transform failed")
                    if not output_file.is_file():
                        raise RuntimeError("watermark transform did not create output")
                    return
                if abort_event.is_set():
                    raise TimeoutError("watermark transform aborted")
                if time.monotonic() >= deadline:
                    raise TimeoutError("watermark transform timed out")
                time.sleep(0.05)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)

    def _stream_events(self, execution_id: str, after_seq: int):
        try:
            self.server.store.replay(execution_id, after_seq)
        except ReplayGapError:
            logger.error(
                "executor event stream replay gap: execution_id=%s after_sequence=%s",
                execution_id,
                after_seq,
            )
            self._write_error(
                HTTPStatus.GONE,
                "replay_gap",
                "requested sequence is no longer available",
            )
            return
        except ExecutionNotFoundError:
            logger.warning(
                "executor event stream requested unknown execution: execution_id=%s after_sequence=%s",
                execution_id,
                after_seq,
            )
            self._write_error(
                HTTPStatus.NOT_FOUND,
                "execution_not_found",
                "execution not found",
            )
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "application/x-ndjson")
        self.end_headers()
        logger.info(
            "executor event stream attached: execution_id=%s after_sequence=%s",
            execution_id,
            after_seq,
        )
        try:
            for event in self.server.store.stream(execution_id, after_seq):
                self.wfile.write(event.to_json_line())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            logger.warning(
                "executor event stream disconnected: execution_id=%s after_sequence=%s",
                execution_id,
                after_seq,
            )
            return
        logger.info(
            "executor event stream ended: execution_id=%s after_sequence=%s",
            execution_id,
            after_seq,
        )

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", "0"))
        raw = self.rfile.read(length)
        if not raw:
            return {}
        return json.loads(raw)

    def _write_json(self, status: HTTPStatus, payload: dict):
        data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _write_error(self, status: HTTPStatus, code: str, message: str):
        self._write_json(status, {"code": code, "message": message})


def serve(
    host: str,
    port: int,
    store: ExecutionStore | None = None,
    runner_name: str = "babeldoc",
):
    runner = _create_runner(runner_name)
    if store is None and isinstance(runner, MultiprocessExecutionRunner):
        runner.warmup()
    server = ExecutorServer(
        (host, port),
        store or ExecutionStore(runner),
    )
    logger.info("starting executor on %s:%s", host, port)
    server.serve_forever()


def _create_runner(runner_name: str):
    if runner_name == "babeldoc":
        from babeldoc.tools.executor.babeldoc_adapter import run_babeldoc_request

        runner = MultiprocessExecutionRunner(
            run_babeldoc_request,
            start_method="forkserver",
            preload_modules=(
                "babeldoc.tools.executor.runner",
                "babeldoc.tools.executor.babeldoc_adapter",
                "babeldoc.format.pdf.high_level",
                "babeldoc.docvision.rpc_doclayout8",
            ),
        )
        return runner
    if runner_name == "fake":
        return FakeExecutionRunner()
    raise ValueError(f"unknown executor runner: {runner_name}")


def main():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Run HTTP executor.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=7860)
    parser.add_argument("--runner", choices=["babeldoc", "fake"], default="babeldoc")
    args = parser.parse_args()
    serve(args.host, args.port, runner_name=args.runner)
