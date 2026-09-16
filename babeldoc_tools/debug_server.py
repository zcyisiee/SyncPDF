"""bdt debug 查看器的本地 HTTP 服务（标准库 ThreadingHTTPServer，只读归档）。

安全边界：

- 只监听 ``127.0.0.1``；每个请求校验随机访问 token（``?token=`` 或
  ``X-Debug-Token`` 头）、``Host``（回环地址）与 ``Origin``（若存在必须同源）。
- 只读：不提供任何写操作；``/api/v1/artifacts/`` 与 ``/api/v1/runs/.../snapshot``
  只允许 run 目录下 ``artifacts/`` / ``snapshots/`` 白名单内的文件（resolve 后
  必须仍在 run 目录内，防路径穿越）。
- 响应统一加 ``X-Content-Type-Options: nosniff`` 与 CSP 头；PDF 以
  ``Content-Disposition: inline`` 提供内联预览。

生命周期：服务进程由 ``bdt debug`` / ``--debug`` 流水线经隐藏参数
``--debug-serve-internal`` 拉起（detached 子进程）。watchdog 线程每 60s 检查：
无活跃 pipeline（``debug/write.lock`` 可获取）且 ``last_request_at`` 静默超过
30 分钟 → 自动退出。
"""

from __future__ import annotations

import datetime
import fcntl
import json
import os
import re
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path

from babeldoc.debug_recorder import read_events

VIEWER_DIR = Path(__file__).resolve().parent / "debug_viewer"

#: run_id 的合法形态（``new_run_id`` 产物）；校验防路径注入。
RUN_ID_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{6}$")

#: 无活跃 pipeline 且无浏览器心跳的自动退出阈值（秒）。
IDLE_TIMEOUT_S = 30 * 60
WATCHDOG_PERIOD_S = 60

#: artifact 路径白名单的第一段（相对 run_dir）。
ALLOWED_ARTIFACT_ROOTS = ("artifacts", "snapshots")

_JSON_TYPES = {".json": "application/json; charset=utf-8"}
_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".log": "text/plain; charset=utf-8",
    ".tex": "text/plain; charset=utf-8",
    ".jsonl": "text/plain; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
}


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _debug_dir(workdir: Path) -> Path:
    return workdir / "debug"


def _runs_dir(workdir: Path) -> Path:
    return _debug_dir(workdir) / "runs"


def _write_lock_path(workdir: Path) -> Path:
    return _debug_dir(workdir) / "write.lock"


def write_lock_free(workdir: Path) -> bool:
    """写锁当前可获得（没有 pipeline 正在写归档）。"""
    lock_path = _write_lock_path(workdir)
    if not lock_path.exists():
        return True
    fd = None
    try:
        fd = os.open(lock_path, os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False
    finally:
        if fd is not None:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
                os.close(fd)
            except OSError:
                pass


def _load_manifest(run_dir: Path) -> dict | None:
    path = run_dir / "manifest.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def list_runs(workdir: Path) -> list[dict]:
    """运行索引：按 created_at 倒序，含每阶段状态摘要。"""
    runs = []
    base = _runs_dir(workdir)
    if not base.is_dir():
        return runs
    for child in base.iterdir():
        if not child.is_dir() or not RUN_ID_RE.match(child.name):
            continue
        manifest = _load_manifest(child)
        if manifest is None:
            # 归档不完整（manifest 没写完）：仍列出，标记 incomplete。
            runs.append(
                {
                    "run_id": child.name,
                    "status": "incomplete",
                    "created_at": None,
                    "stages": {},
                }
            )
            continue
        stages = manifest.get("stages") or {}
        runs.append(
            {
                "run_id": manifest.get("run_id") or child.name,
                "status": manifest.get("status"),
                "mode": manifest.get("mode"),
                "created_at": manifest.get("created_at"),
                "finished_at": manifest.get("finished_at"),
                "stages": {
                    name: (entry or {}).get("status")
                    for name, entry in stages.items()
                },
                "artifact_count": manifest.get("artifact_count", 0),
            }
        )
    runs.sort(key=lambda item: (item.get("created_at") or "", item["run_id"]), reverse=True)
    return runs


def _resolve_run_dir(workdir: Path, run_id: str) -> Path | None:
    if not RUN_ID_RE.match(run_id or ""):
        return None
    run_dir = (_runs_dir(workdir) / run_id).resolve()
    runs_root = _runs_dir(workdir).resolve()
    if runs_root not in run_dir.parents:
        return None
    return run_dir if run_dir.is_dir() else None


def _resolve_artifact(run_dir: Path, relative: str) -> Path | None:
    """把 ``<root>/<sub path>`` 解析到 run_dir 内；越界/非白名单返回 ``None``。"""
    parts = [part for part in relative.split("/") if part]
    if not parts or parts[0] not in ALLOWED_ARTIFACT_ROOTS:
        return None
    if any(part in (".", "..") for part in parts):
        return None
    target = (run_dir / Path(*parts)).resolve()
    root = run_dir.resolve()
    if root != target and root not in target.parents:
        return None
    return target if target.is_file() else None


class _Context:
    """挂在 server 实例上的运行上下文（workdir / token / 活跃度）。"""

    def __init__(self, workdir: Path, token: str):
        self.workdir = workdir
        self.token = token
        self.last_request_at = time.time()


class _Handler(BaseHTTPRequestHandler):
    """只读 JSON/静态资源处理；所有路由都要求 token。"""

    server_version = "bdt-debug/1"
    protocol_version = "HTTP/1.1"

    # ---------------------------------------------------------- 基础设施
    @property
    def ctx(self) -> _Context:
        return self.server.debug_ctx  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):  # noqa: A003 - http.server 签名
        sys.stderr.write("debug-viewer %s\n" % (fmt % args))

    def _touch(self) -> None:
        self.ctx.last_request_at = time.time()

    def _send(self, status: int, body: bytes, content_type: str, extra=None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; "
            "style-src 'self' 'unsafe-inline'; connect-src 'self'; "
            "frame-ancestors 'none'; base-uri 'none'",
        )
        for name, value in (extra or {}).items():
            self.send_header(name, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _send_json(self, payload, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _send_error_json(self, status: int, code: str, message: str) -> None:
        self._send_json({"ok": False, "error": {"code": code, "message": message}}, status)

    # ---------------------------------------------------------- 安全校验
    def _authorized(self, parsed) -> bool:
        host = (self.headers.get("Host") or "").split("@")[-1]
        hostname = host.split(":")[0]
        if hostname not in ("127.0.0.1", "localhost"):
            self._send_error_json(403, "bad_host", "Host 必须是回环地址")
            return False
        origin = self.headers.get("Origin")
        if origin:
            origin_host = urllib.parse.urlparse(origin).hostname or ""
            if origin_host not in ("127.0.0.1", "localhost"):
                self._send_error_json(403, "bad_origin", "Origin 必须同源")
                return False
        query = urllib.parse.parse_qs(parsed.query)
        token = (query.get("token") or [None])[0] or self.headers.get("X-Debug-Token")
        if token != self.ctx.token:
            self._send_error_json(403, "bad_token", "缺少或错误的访问 token")
            return False
        return True

    # ---------------------------------------------------------- 路由
    def do_GET(self) -> None:  # noqa: N802 - http.server 签名
        self._touch()
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if path == "/" or path == "/index.html":
            self._serve_static("index.html")
            return
        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return
        if path.startswith("/static/"):
            self._serve_static(path[len("/static/"):])
            return
        if not path.startswith("/api/"):
            self._send_error_json(404, "not_found", "未知路径")
            return
        if not self._authorized(parsed):
            return
        self._route_api(path, parsed)

    def _serve_static(self, relative: str) -> None:
        parts = [part for part in relative.split("/") if part]
        if not parts or any(part in (".", "..") for part in parts):
            self._send_error_json(404, "not_found", "静态资源不存在")
            return
        target = (VIEWER_DIR / Path(*parts)).resolve()
        if VIEWER_DIR not in target.parents or not target.is_file():
            self._send_error_json(404, "not_found", "静态资源不存在")
            return
        content_type = _CONTENT_TYPES.get(target.suffix.lower(), "application/octet-stream")
        self._send(200, target.read_bytes(), content_type)

    def _route_api(self, path: str, parsed) -> None:
        workdir = self.ctx.workdir
        if path == "/api/v1/heartbeat":
            self._send_json({"ok": True, "at": _now()})
            return
        if path == "/api/v1/runs":
            self._send_json({"ok": True, "runs": list_runs(workdir)})
            return

        match = re.match(r"^/api/v1/runs/([^/]+)/manifest$", path)
        if match:
            run_dir = _resolve_run_dir(workdir, match.group(1))
            manifest = _load_manifest(run_dir) if run_dir else None
            if manifest is None:
                self._send_error_json(404, "run_not_found", "run 不存在或 manifest 不完整")
                return
            self._send_json({"ok": True, "manifest": manifest})
            return

        match = re.match(r"^/api/v1/runs/([^/]+)/events$", path)
        if match:
            run_dir = _resolve_run_dir(workdir, match.group(1))
            if run_dir is None:
                self._send_error_json(404, "run_not_found", "run 不存在")
                return
            query = urllib.parse.parse_qs(parsed.query)
            try:
                after_seq = int((query.get("after_seq") or ["0"])[0])
            except ValueError:
                after_seq = 0
            events = read_events(run_dir, after_seq=after_seq)
            last_seq = max((event.get("seq") or 0 for event in events), default=after_seq)
            self._send_json({"ok": True, "events": events, "last_seq": last_seq})
            return

        match = re.match(r"^/api/v1/runs/([^/]+)/snapshot/(.+)$", path)
        if match:
            run_dir = _resolve_run_dir(workdir, match.group(1))
            if run_dir is None:
                self._send_error_json(404, "run_not_found", "run 不存在")
                return
            relative = urllib.parse.unquote(match.group(2))
            target = _resolve_artifact(run_dir, f"snapshots/{relative}")
            if target is None or target.suffix != ".json":
                self._send_error_json(404, "snapshot_not_found", "快照不存在")
                return
            self._send(
                200,
                target.read_bytes(),
                "application/json; charset=utf-8",
            )
            return

        match = re.match(r"^/api/v1/artifacts/([^/]+)/(.+)$", path)
        if match:
            run_dir = _resolve_run_dir(workdir, match.group(1))
            if run_dir is None:
                self._send_error_json(404, "run_not_found", "run 不存在")
                return
            relative = urllib.parse.unquote(match.group(2))
            target = _resolve_artifact(run_dir, relative)
            if target is None:
                self._send_error_json(404, "artifact_not_found", "证据文件不存在或不在白名单")
                return
            content_type = _CONTENT_TYPES.get(
                target.suffix.lower(), "application/octet-stream"
            )
            extra = {}
            if target.suffix.lower() == ".pdf":
                extra["Content-Disposition"] = f'inline; filename="{target.name}"'
            self._send(200, target.read_bytes(), content_type, extra=extra)
            return

        self._send_error_json(404, "not_found", "未知 API 路径")


def _write_viewer_state(workdir: Path, port: int, token: str) -> Path:
    state = {
        "pid": os.getpid(),
        "port": port,
        "token": token,
        "started_at": _now(),
        "heartbeat_at": _now(),
    }
    path = _debug_dir(workdir) / "viewer.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return path


def _clear_viewer_state(workdir: Path) -> None:
    path = _debug_dir(workdir) / "viewer.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = None
    # 只在确认是自己的状态文件时才删，避免误删继任查看器的状态。
    if state is None or state.get("pid") == os.getpid():
        try:
            path.unlink()
        except OSError:
            pass


def _watchdog(server: ThreadingHTTPServer, ctx: _Context) -> None:
    while True:
        time.sleep(WATCHDOG_PERIOD_S)
        idle = time.time() - ctx.last_request_at
        if idle > IDLE_TIMEOUT_S and write_lock_free(ctx.workdir):
            sys.stderr.write(
                f"debug-viewer: 无活跃 pipeline 且静默 {int(idle)}s，自动退出\n"
            )
            server.shutdown()
            return


def serve(workdir, port: int = 0, token: str = "") -> int:
    """启动查看器服务（阻塞，直到 watchdog 或外部信号让它 shutdown）。

    返回进程退出码。URL 与状态文件 ``<workdir>/debug/viewer.json`` 就绪后写。
    """
    workdir_path = Path(workdir).resolve()
    _runs_dir(workdir_path).mkdir(parents=True, exist_ok=True)
    ctx = _Context(workdir_path, token)
    try:
        server = ThreadingHTTPServer(("127.0.0.1", int(port or 0)), _Handler)
    except OSError as exc:
        sys.stderr.write(f"debug-viewer: 端口 {port} 绑定失败: {exc}\n")
        return 1
    server.daemon_threads = True
    server.debug_ctx = ctx  # type: ignore[attr-defined]
    actual_port = int(server.server_address[1])
    _write_viewer_state(workdir_path, actual_port, token)
    sys.stderr.write(
        f"debug-viewer: http://127.0.0.1:{actual_port}/?token={token} "
        f"(workdir={workdir_path})\n"
    )
    watcher = threading.Thread(
        target=_watchdog, args=(server, ctx), name="debug-viewer-watchdog", daemon=True
    )
    watcher.start()
    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        _clear_viewer_state(workdir_path)
    return 0
