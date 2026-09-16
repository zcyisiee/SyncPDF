"""诊断采集核心：版本化运行归档 + 线程安全事件流 + 原子快照。

设计要点（对齐计划的数据契约）：

- **显式可选**：管线只在拿到 recorder 时采集；recorder 为 ``None``（或
  :class:`NullRecorder`）时零额外 IO。深层模块用 :func:`get_current` /
  :func:`set_current` 取进程内当前 recorder（模块级全局，线程池 worker 也能取到）。
- **线程安全**：事件序号用锁递增；快照与证据文件先写临时文件再 ``os.replace``
  原子发布，随后才写 manifest。
- **失败安全**：``record_event`` / ``write_snapshot`` / ``archive_file`` 内部异常
  一律捕获并记入 ``errors``（经 :attr:`DebugRecorder.capture_status` 暴露），
  **绝不中断管线**；返回 ``None`` 表示该条证据缺失。
- **零业务依赖**：只依赖标准库。

归档布局::

    <workdir>/debug/runs/<run_id>/
      manifest.json   # 版本化运行清单（原子写）
      events.jsonl    # 追加式事件流（每行一个 JSON 对象，含 seq）
      snapshots/      # 按阶段与页面拆分的快照 JSON
      artifacts/      # 复制的稳定证据文件（PDF 副本、TeX、日志等）
"""

from __future__ import annotations

import contextlib
import datetime
import hashlib
import json
import os
import re
import shlex
import shutil
import threading
import time
import uuid
from pathlib import Path
from pathlib import PurePosixPath

SCHEMA_VERSION = 1

MANIFEST_FILE = "manifest.json"
EVENTS_FILE = "events.jsonl"
SNAPSHOTS_DIR = "snapshots"
ARTIFACTS_DIR = "artifacts"

STATUS_RUNNING = "running"
STATUS_FINISHED = "finished"
STATUS_INTERRUPTED = "interrupted"
STATUS_ERROR = "error"


# --------------------------------------------------------------------------- #
# 时间与 run_id
# --------------------------------------------------------------------------- #
def _now(*, milliseconds: bool = False) -> str:
    """UTC ISO8601 时间戳；``milliseconds`` 控制精度（事件允许毫秒）。"""
    spec = "milliseconds" if milliseconds else "seconds"
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec=spec)


_run_id_lock = threading.Lock()
_run_id_counter = 0


def new_run_id() -> str:
    """生成 run_id：``<UTC时间戳>Z-<6位十六进制后缀>``（如 ``20260916T083000Z-00012a``）。

    同一进程内后缀单调递增（可排序、可复现），并混入低 8 位 pid 以免不同
    进程在同一秒生成同名目录。
    """
    global _run_id_counter
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    with _run_id_lock:
        _run_id_counter += 1
        index = _run_id_counter
    suffix = f"{index & 0xFFFF:04x}{os.getpid() & 0xFF:02x}"
    return f"{stamp}-{suffix}"


def _sha256_file(path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _join(*parts: str, suffix: str = "") -> str:
    """拼相对路径（统一 ``/`` 分隔）；``suffix`` 在缺后缀时补齐。"""
    segments = [str(part) for part in parts]
    if any(
        PurePosixPath(part).is_absolute()
        or ".." in PurePosixPath(part).parts
        or "\\" in part
        for part in segments
    ):
        raise ValueError("归档路径必须位于 run 目录内")
    relative = "/".join(segment for segment in segments if segment)
    if suffix and not relative.endswith(suffix):
        relative += suffix
    return relative


def _tmp_path_for(target: Path) -> Path:
    return target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")


# --------------------------------------------------------------------------- #
# 进程内上下文
# --------------------------------------------------------------------------- #
_current_lock = threading.Lock()
_current: DebugRecorder | None = None


def set_current(recorder: DebugRecorder | None) -> None:
    """设置进程内当前 recorder（``None`` = 关闭采集）。"""
    global _current
    with _current_lock:
        _current = recorder


def get_current() -> DebugRecorder | None:
    """返回进程内当前 recorder；未开启时返回 ``None``。

    模块级全局（非 thread-local）：批编译线程池里的 worker 也能取到同一个
    recorder。调用方约定 ``recorder = get_current()`` 后判 ``if recorder:``。
    """
    with _current_lock:
        return _current


def read_events(run_dir, after_seq: int = 0) -> list[dict]:
    """读取 ``events.jsonl``；忽略未写完/损坏的行，只返回 ``seq > after_seq``。

    读取端（查看器）依赖这里的容错：写入方正在追加的行可能只有半行 JSON。
    """
    path = Path(run_dir) / EVENTS_FILE
    if not path.is_file():
        return []
    floor = int(after_seq or 0)
    events: list[dict] = []
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            text = line.strip()
            if not text:
                continue
            try:
                event = json.loads(text)
            except (ValueError, TypeError):
                # 未完成的尾行 / 损坏行：跳过，不让查看器整篇失败。
                continue
            if not isinstance(event, dict):
                continue
            seq = event.get("seq")
            if isinstance(seq, int) and seq <= floor:
                continue
            events.append(event)
    return events


# --------------------------------------------------------------------------- #
# recorder
# --------------------------------------------------------------------------- #
class DebugRecorder:
    """一次运行的归档写入方（一个 ``run_dir`` 一个实例）。"""

    def __init__(self, run_dir):
        self.run_dir = Path(run_dir)
        self.run_id = self.run_dir.name
        self._lock = threading.RLock()
        self._publication_lock = threading.RLock()
        self._seq = 0
        self._ids = 0
        self._secrets: set[str] = set()
        self._errors: list[dict] = []
        self._closed = False
        self._snapshots_dir = self.run_dir / SNAPSHOTS_DIR
        self._artifacts_dir = self.run_dir / ARTIFACTS_DIR
        self._manifest_path = self.run_dir / MANIFEST_FILE
        self._events_path = self.run_dir / EVENTS_FILE
        self._manifest: dict = {
            "schema_version": SCHEMA_VERSION,
            "run_id": self.run_id,
            "created_at": _now(),
            "finished_at": None,
            "status": STATUS_RUNNING,
            "input": {},
            "config": {"stages": [], "options": {}},
            "stages": {},
            "artifact_count": 0,
        }
        self._snapshots_dir.mkdir(parents=True, exist_ok=True)
        self._artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._events_handle = self._events_path.open("a", encoding="utf-8")
        self._flush_manifest()

    # ---------------------------------------------------------- 只读属性
    @property
    def manifest_path(self) -> Path:
        return self._manifest_path

    @property
    def events_path(self) -> Path:
        return self._events_path

    @property
    def manifest(self) -> dict:
        with self._lock:
            return json.loads(json.dumps(self._manifest))

    @property
    def capture_status(self) -> dict:
        """采集状态：``{"ok": true}`` 或 ``{"ok": false, "errors": [...]}``。"""
        with self._lock:
            errors = [dict(error) for error in self._errors]
        if not errors:
            return {"ok": True}
        return {"ok": False, "errors": errors}

    def new_id(self, prefix: str) -> str:
        with self._lock:
            self._ids += 1
            return f"{prefix}-{self._ids:06d}"

    def capture(self, operation: str, callback, *args, **kwargs):
        try:
            return callback(*args, **kwargs)
        except Exception as exc:
            self._record_error(operation, exc)
            return None

    def register_command(self, command: str) -> dict:
        try:
            argv = shlex.split(command)
        except ValueError:
            return {"executable": None, "arguments": "omitted", "valid": False}
        sensitive = re.compile(r"api.?key|token|secret|password|passwd|authorization", re.I)
        with self._lock:
            for index, arg in enumerate(argv):
                key, separator, value = arg.partition("=")
                if sensitive.search(key) and key.startswith("-"):
                    if not separator and index + 1 < len(argv):
                        value = argv[index + 1]
                    if value:
                        self._secrets.add(value)
                        if value.lower().startswith("bearer "):
                            self._secrets.add(value[7:])
        return {
            "executable": Path(argv[0]).name if argv else None,
            "argc": len(argv),
            "arguments": "omitted",
        }

    def redact(self, value):
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        if isinstance(value, str):
            with self._lock:
                secrets = sorted(self._secrets, key=len, reverse=True)
            for secret in secrets:
                value = value.replace(secret, "[REDACTED]")
            value = re.sub(r"(?i)(\bBearer\s+)[^\s\"',;]+", r"\1[REDACTED]", value)
            return re.sub(
                r"(?i)(\b(?:api[_-]?key|access[_-]?token|client[_-]?secret|password|passwd|token)\b[\"']?\s*[=:]\s*[\"']?)[^\s\"',;&]+",
                r"\1[REDACTED]",
                value,
            )
        if isinstance(value, dict):
            return {str(key): self.redact(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.redact(item) for item in value]
        return value

    @contextlib.contextmanager
    def process(self, stage: str, origin: str, command, *, prompt=None, timeout=None, **context):
        state = {"call_id": self.new_id("call")}
        metadata = self.capture(
            "process_started", self._start_process, stage, origin, command,
            state["call_id"], prompt, timeout, context,
        ) or {"call_id": state["call_id"], "origin": origin}
        started = time.perf_counter()
        try:
            yield state
        except BaseException as exc:
            state["exception"] = exc
            raise
        finally:
            seconds = time.perf_counter() - started
            self.capture("process_finished", self._finish_process, stage, metadata, state, seconds)

    def _start_process(self, stage, origin, command, call_id, prompt, timeout, context):
        metadata = {
            "call_id": call_id,
            "origin": origin,
            "command": self.register_command(command if isinstance(command, str) else shlex.join(command)),
            "timeout": timeout,
            **context,
        }
        if prompt is not None:
            metadata["prompt"] = self.archive_text(stage, f"calls/{call_id}/prompt.txt", prompt)
        self.record_event(stage, "call_started", metadata)
        return metadata

    def _finish_process(self, stage, metadata, state, seconds):
        exception = state.get("exception")
        cause = (exception.__cause__ or exception) if exception is not None else None
        result = state.get("result")
        output = result if result is not None else cause
        returncode = getattr(result, "returncode", None)
        status = "error" if exception is not None or returncode not in (None, 0) else "ok"
        if exception is not None and not isinstance(exception, Exception):
            status = "interrupted"
        data = {
            **metadata,
            "status": status,
            "returncode": returncode,
            "seconds": round(seconds, 6),
            "error_code": getattr(exception, "code", None),
            "error_type": type(cause).__name__ if cause is not None else None,
            "error": str(exception) if exception is not None else None,
        }
        for name in ("stdout", "stderr"):
            text = getattr(output, name, None)
            if name == "stdout" and text is None:
                text = getattr(output, "output", None)
            data[name] = self.archive_text(stage, f"calls/{state['call_id']}/{name}.txt", text)
        snapshot = self.write_snapshot(stage, f"calls/{state['call_id']}", data)
        self.record_event(stage, "call_finished", {**data, "snapshot": snapshot})

    # ---------------------------------------------------------- manifest
    def add_input(self, name: str, path, *, sha256: str | None = None) -> str | None:
        """记录运行输入（如 ``pdf``）；``sha256`` 缺省按文件内容计算。"""
        try:
            entry: dict = {"path": str(path)}
            if sha256 is None and path is not None and Path(path).is_file():
                sha256 = _sha256_file(path)
            if sha256:
                entry["sha256"] = sha256
            with self._lock:
                self._manifest.setdefault("input", {})[name] = entry
            self._flush_manifest()
            return entry.get("sha256")
        except Exception as exc:  # noqa: BLE001 - 采集中断不得影响管线
            self._record_error("add_input", exc, path=name)
            return None

    def set_config(self, *, stages=None, options: dict | None = None) -> None:
        """写入配置白名单（不采集 token / 完整环境）。"""
        try:
            with self._lock:
                config = self._manifest.setdefault("config", {})
                if stages is not None:
                    config["stages"] = [str(stage) for stage in stages]
                if options:
                    config.setdefault("options", {}).update(options)
            self._flush_manifest()
        except Exception as exc:  # noqa: BLE001
            self._record_error("set_config", exc)

    # ---------------------------------------------------------- stages
    def start_stage(self, stage: str) -> None:
        try:
            with self._lock:
                entry = self._stage_entry(stage)
                entry["status"] = STATUS_RUNNING
                entry["started_at"] = _now()
            self._flush_manifest()
        except Exception as exc:  # noqa: BLE001
            self._record_error("start_stage", exc, stage=stage)

    def finish_stage(self, stage: str, status: str = "ok") -> None:
        try:
            with self._lock:
                entry = self._stage_entry(stage)
                entry["status"] = status
                entry["finished_at"] = _now()
            self._flush_manifest()
        except Exception as exc:  # noqa: BLE001
            self._record_error("finish_stage", exc, stage=stage)

    # ---------------------------------------------------------- events
    def record_event(self, stage: str, kind: str, data: dict | None = None):
        """追加一条事件；线程安全分配 ``seq``。返回事件 dict，失败返回 ``None``。"""
        try:
            with self._lock:
                self._seq += 1
                event = {
                    "seq": self._seq,
                    "at": _now(milliseconds=True),
                    "stage": stage,
                    "kind": kind,
                    "data": self.redact(data if isinstance(data, dict) else {"value": data}),
                }
                self._events_handle.write(json.dumps(event, ensure_ascii=False) + "\n")
                self._events_handle.flush()
                entry = self._stage_entry(stage)
                entry["events"] = int(entry.get("events", 0)) + 1
            return event
        except Exception as exc:  # noqa: BLE001
            self._record_error("record_event", exc, stage=stage, kind=kind)
            return None

    # ---------------------------------------------------------- snapshots
    def write_snapshot(self, stage: str, name: str, payload) -> str | None:
        """原子写 ``snapshots/<stage>/<name>.json``；返回相对 run_dir 的路径。"""
        try:
            relative = _join(SNAPSHOTS_DIR, stage, name, suffix=".json")
            with self._publication_lock:
                self._atomic_write_json(self.run_dir / relative, self.redact(payload))
                self._publish_ref(relative, "snapshots", stage)
            return relative
        except Exception as exc:  # noqa: BLE001
            self._record_error("write_snapshot", exc, stage=stage, name=name)
            return None

    # ---------------------------------------------------------- artifacts
    def archive_file(self, stage: str, name: str, source) -> str | None:
        """把证据文件复制到 ``artifacts/<stage>/<name>``（原子替换）。

        源不存在时**不抛出**：记入 ``errors`` 并返回 ``None``（采集失败不得
        中断管线）。同名覆盖语义——历史保留靠 run_id 目录隔离。
        """
        try:
            relative = _join(ARTIFACTS_DIR, stage, name)
            source_path = Path(source)
            if not source_path.is_file():
                raise FileNotFoundError(f"证据文件不存在: {source_path}")
            if source_path.suffix.lower() in {".txt", ".md", ".log", ".tex", ".json", ".jsonl"}:
                return self.archive_text(stage, name, source_path.read_bytes())
            target = self.run_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = _tmp_path_for(target)
            try:
                shutil.copyfile(source_path, tmp)
                with self._publication_lock:
                    tmp.replace(target)
                    self._publish_ref(relative, "artifacts", stage)
            finally:
                self._remove_quietly(tmp)
            return relative
        except Exception as exc:  # noqa: BLE001
            self._record_error("archive_file", exc, stage=stage, name=name)
            return None

    def archive_text(self, stage: str, name: str, text) -> str | None:
        try:
            relative = _join(ARTIFACTS_DIR, stage, name)
            target = self.run_dir / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            source = text.decode("utf-8", errors="replace") if isinstance(text, bytes) else str(text or "")
            sanitized = self.redact(source)
            tmp = _tmp_path_for(target)
            try:
                tmp.write_text(sanitized, encoding="utf-8")
                with self._publication_lock:
                    tmp.replace(target)
                    self._publish_ref(relative, "artifacts", stage, redacted=source != sanitized)
            finally:
                self._remove_quietly(tmp)
            return relative
        except Exception as exc:
            self._record_error("archive_text", exc, stage=stage, name=name)
            return None

    def _publish_ref(self, relative: str, category: str, stage: str, **metadata) -> None:
        target = self.run_dir / relative
        entry = {
            "path": relative,
            "stage": stage,
            "sha256": _sha256_file(target),
            "bytes": target.stat().st_size,
            **metadata,
        }
        with self._lock:
            self._manifest.setdefault(category, {})[relative] = entry
            if category == "artifacts":
                self._manifest["artifact_count"] = len(self._manifest[category])
        self._flush_manifest()

    # ---------------------------------------------------------- lifecycle
    def finish(self, status: str = STATUS_FINISHED) -> None:
        """写 manifest 的 ``finished_at`` / ``status``。"""
        try:
            with self._lock:
                self._manifest["finished_at"] = _now()
                self._manifest["status"] = status
            self._flush_manifest()
        except Exception as exc:  # noqa: BLE001
            self._record_error("finish", exc)

    def close(self) -> None:
        """关闭事件文件句柄（进程退出前调用；幂等）。"""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            handle = self._events_handle
        try:
            handle.close()
        except OSError:
            pass

    # ---------------------------------------------------------- 内部
    def _stage_entry(self, stage: str) -> dict:
        entry = self._manifest.setdefault("stages", {}).setdefault(str(stage), {})
        entry.setdefault("events", 0)
        return entry

    def _record_error(self, operation: str, exc: BaseException, **context) -> None:
        error = {
            "operation": operation,
            "type": type(exc).__name__,
            "message": self.redact(str(exc))[:300],
        }
        error.update(
            {key: value for key, value in context.items() if value is not None}
        )
        with self._lock:
            self._errors.append(error)

    def _flush_manifest(self) -> None:
        """原子写 manifest；失败只记错误不抛出。"""
        try:
            with self._publication_lock:
                with self._lock:
                    payload = self.redact(json.loads(json.dumps(self._manifest)))
                    if self._errors:
                        payload["capture_status"] = self.capture_status
                self._atomic_write_json(self._manifest_path, payload)
        except Exception as exc:  # noqa: BLE001
            self._record_error("flush_manifest", exc)

    def _atomic_write_json(self, target: Path, payload) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        tmp = _tmp_path_for(target)
        try:
            tmp.write_text(text, encoding="utf-8")
            tmp.replace(target)
        finally:
            self._remove_quietly(tmp)

    @staticmethod
    def _remove_quietly(path: Path) -> None:
        try:
            if path.exists():
                path.unlink()
        except OSError:
            pass


class NullRecorder:
    """关闭状态的占位 recorder：所有方法 no-op（便于显式传递 recorder 的场景）。

    约定：``None`` 表示完全关闭（调用方判 ``if recorder:``）；需要传对象但确实
    不采集时用本类，避免到处写分支。
    """

    run_id = ""
    run_dir = None

    def __bool__(self):
        return False

    def new_id(self, prefix):
        return ""

    def capture(self, operation, callback, *args, **kwargs):
        return None

    def register_command(self, command):
        return {}

    def redact(self, value):
        return value

    def archive_text(self, stage, name, text):
        return None

    @property
    def capture_status(self) -> dict:
        return {"ok": True}

    @property
    def manifest(self) -> dict:
        return {}

    def add_input(self, name, path, **kwargs):
        return None

    def set_config(self, **kwargs):
        return None

    def start_stage(self, stage):
        return None

    def finish_stage(self, stage, status="ok"):
        return None

    def record_event(self, stage, kind, data=None):
        return None

    def write_snapshot(self, stage, name, payload):
        return None

    def archive_file(self, stage, name, source):
        return None

    def finish(self, status=STATUS_FINISHED):
        return None

    def close(self):
        return None
