"""``bdt debug`` 的运行时管理：查看器进程生命周期 + 写锁 + run 归档开关。

进程模型（一个 workdir 一个查看器）：

- 查看器是 detached 子进程：``python -m babeldoc_tools --debug-serve-internal
  --workdir <W> --port <P> --token <T>``（隐藏参数，非公开子命令）。pipeline
  进程结束后查看器继续服务；``bdt debug --stop`` 或 30 分钟静默自动退出。
- 状态文件 ``<workdir>/debug/viewer.json``：``{pid, port, token, started_at,
  heartbeat_at}``；启动时 pid 活着且端口可达 → 复用。
- 写互斥：``<workdir>/debug/write.lock``（``fcntl.flock`` 独占，进程退出自动
  释放）。同一 workdir 同时只允许一个 ``--debug`` 写入方；查看器只读，不拿锁。
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

from babeldoc import debug_recorder as _dr

from babeldoc_tools import common

VIEWER_READY_TIMEOUT_S = 10.0
VIEWER_STOP_TIMEOUT_S = 5.0


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _debug_dir(workdir: Path) -> Path:
    return Path(workdir) / "debug"


def _viewer_state_path(workdir: Path) -> Path:
    return _debug_dir(workdir) / "viewer.json"


def _write_lock_path(workdir: Path) -> Path:
    return _debug_dir(workdir) / "write.lock"


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _pid_alive(pid) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    # 先回收我们自己的子进程：已退出的 detached 子进程在 wait 之前是僵尸，
    # kill(pid, 0) 对僵尸仍返回成功，会误判为"活着"。
    try:
        reaped, _status = os.waitpid(pid, os.WNOHANG)
        if reaped == pid:
            return False
    except (ChildProcessError, OSError):
        pass
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _port_reachable(port) -> bool:
    try:
        port = int(port)
    except (TypeError, ValueError):
        return False
    if port <= 0:
        return False
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.5):
            return True
    except OSError:
        return False


def _find_free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _viewer_url(port: int, token: str, run_id: str | None = None) -> str:
    url = f"http://127.0.0.1:{port}/?token={token}"
    if run_id:
        url += f"#run={run_id}"
    return url


def _package_parent() -> Path:
    """``babeldoc_tools`` 的父目录（保证 detached 子进程能 import 包）。"""
    return Path(__file__).resolve().parent.parent


class DebugSession:
    """一个 workdir 的 debug 会话：写锁 + 本次 run 的 recorder + 查看器进程。"""

    def __init__(self, workdir):
        self.workdir = Path(workdir)
        self._lock_fd: int | None = None

    # ------------------------------------------------------------ 写锁
    def acquire_write_lock(self) -> None:
        """获取 ``debug/write.lock`` 独占锁；拿不到报 ``debug_busy``。"""
        debug_dir = _debug_dir(self.workdir)
        try:
            debug_dir.mkdir(parents=True, exist_ok=True)
            fd = os.open(
                _write_lock_path(self.workdir), os.O_RDWR | os.O_CREAT, 0o644
            )
        except OSError as exc:
            raise common.ToolError(
                "debug_start_failed", f"debug 归档目录不可写: {exc}"
            ) from exc
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            raise common.ToolError(
                "debug_busy",
                f"workdir {self.workdir} 已有另一个 debug 写入方在运行"
                "（同一 workdir 禁止并发 debug 写入；不同 workdir 可并行）",
            ) from exc
        self._lock_fd = fd

    def release_write_lock(self) -> None:
        fd, self._lock_fd = self._lock_fd, None
        if fd is None:
            return
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        try:
            os.close(fd)
        except OSError:
            pass

    # ------------------------------------------------------------ run
    def create_run(
        self,
        *,
        config: dict | None = None,
        input_pdf=None,
    ):
        """拿写锁并创建本次 run 的 ``DebugRecorder``（失败 → ``ToolError``）。"""
        self.acquire_write_lock()
        try:
            run_id = _dr.new_run_id()
            run_dir = _debug_dir(self.workdir) / "runs" / run_id
            run_dir.mkdir(parents=True, exist_ok=False)
            recorder = _dr.DebugRecorder(run_dir)
        except common.ToolError:
            self.release_write_lock()
            raise
        except Exception as exc:  # noqa: BLE001 - 归档不可写必须中止 pipeline
            self.release_write_lock()
            raise common.ToolError(
                "debug_start_failed", f"debug 归档创建失败: {exc}"
            ) from exc
        if config:
            recorder.set_config(
                stages=config.get("stages"), options=config.get("options")
            )
        if input_pdf:
            recorder.add_input("pdf", input_pdf)
        _dr.set_current(recorder)
        return recorder

    def finish_run(self, recorder, status: str = _dr.STATUS_FINISHED) -> None:
        """收 run：写 manifest 终态、复位上下文、释放写锁。"""
        try:
            if recorder is not None:
                recorder.finish(status)
                recorder.close()
        finally:
            _dr.set_current(None)
            self.release_write_lock()

    # ------------------------------------------------------------ viewer
    def viewer_state(self) -> dict | None:
        return _read_json(_viewer_state_path(self.workdir))

    def _viewer_alive(self, state: dict | None) -> bool:
        if not state:
            return False
        return _pid_alive(state.get("pid")) and _port_reachable(state.get("port"))

    def start_viewer(
        self,
        *,
        port: int = 0,
        no_open: bool = False,
        run_id: str | None = None,
    ) -> dict:
        """复用或启动查看器进程；返回 ``{url, port, pid, reused, run_id}``。

        服务无法在超时内就绪 → ``ToolError(debug_start_failed)``（调用方语义：
        debug 启动失败 = 整个命令失败，pipeline 不开始）。
        """
        state = self.viewer_state()
        if self._viewer_alive(state):
            url = _viewer_url(int(state["port"]), state["token"], run_id)
            if not no_open:
                _open_browser(url)
            return {
                "url": url,
                "port": int(state["port"]),
                "pid": int(state["pid"]),
                "reused": True,
                "run_id": run_id,
            }

        # 清理失效状态文件，再 spawn detached 子进程。
        if state is not None:
            with contextlib.suppress(OSError):
                _viewer_state_path(self.workdir).unlink()

        token = secrets.token_urlsafe(16)
        requested = int(port or 0)
        bind_port = requested if requested > 0 else _find_free_port()
        env = dict(os.environ)
        package_parent = str(_package_parent())
        env["PYTHONPATH"] = (
            package_parent + os.pathsep + env["PYTHONPATH"]
            if env.get("PYTHONPATH")
            else package_parent
        )
        argv = [
            sys.executable,
            "-m",
            "babeldoc_tools",
            "--debug-serve-internal",
            "--workdir",
            str(self.workdir),
            "--port",
            str(bind_port),
            "--token",
            token,
        ]
        try:
            proc = subprocess.Popen(  # noqa: S603 - 固定 argv 拉起自身包
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                env=env,
            )
        except OSError as exc:
            raise common.ToolError(
                "debug_start_failed", f"查看器进程启动失败: {exc}"
            ) from exc

        heartbeat = f"http://127.0.0.1:{bind_port}/api/v1/heartbeat"
        deadline = time.time() + VIEWER_READY_TIMEOUT_S
        while time.time() < deadline:
            if proc.poll() is not None:
                raise common.ToolError(
                    "debug_start_failed",
                    f"查看器进程启动后立即退出（exit {proc.returncode}）；"
                    f"端口 {bind_port} 可能被占用",
                )
            try:
                request = urllib.request.Request(
                    heartbeat, headers={"X-Debug-Token": token}
                )
                # 心跳地址固定 127.0.0.1（本进程刚 spawn 的查看器），非外部输入。
                with urllib.request.urlopen(  # noqa: S310
                    request, timeout=1.0
                ) as response:
                    if response.status == 200:
                        break
            except OSError:
                time.sleep(0.1)
        else:
            proc.terminate()
            raise common.ToolError(
                "debug_start_failed",
                f"查看器 {VIEWER_READY_TIMEOUT_S}s 内未就绪（端口 {bind_port}）",
            )

        url = _viewer_url(bind_port, token, run_id)
        sys.stderr.write(f"debug viewer: {url}\n")
        if not no_open:
            _open_browser(url)
        return {
            "url": url,
            "port": bind_port,
            "pid": proc.pid,
            "reused": False,
            "run_id": run_id,
        }

    def stop_viewer(self) -> dict:
        """停掉查看器进程（SIGTERM，等 ``VIEWER_STOP_TIMEOUT_S`` 后 SIGKILL）。"""
        state = self.viewer_state()
        if not state or not _pid_alive(state.get("pid")):
            with contextlib.suppress(OSError):
                _viewer_state_path(self.workdir).unlink()
            return {"stopped": False, "reason": "not_running"}
        pid = int(state["pid"])
        try:
            os.kill(pid, 15)  # SIGTERM
        except OSError as exc:
            return {"stopped": False, "reason": f"signal_failed: {exc}"}
        deadline = time.time() + VIEWER_STOP_TIMEOUT_S
        while time.time() < deadline:
            if not _pid_alive(pid):
                break
            time.sleep(0.1)
        if _pid_alive(pid):
            with contextlib.suppress(OSError):
                os.kill(pid, 9)  # SIGKILL
            time.sleep(0.2)
        with contextlib.suppress(OSError):
            _viewer_state_path(self.workdir).unlink()
        return {"stopped": not _pid_alive(pid), "pid": pid}

    # ------------------------------------------------------------ bindings
    def record_bindings(
        self, *, source_pdf: str | None = None, mono: str | None = None
    ) -> Path | None:
        """落盘 ``debug/bindings.json``（旧 workdir 回放的显式 PDF 绑定）。"""
        if not source_pdf and not mono:
            return None
        debug_dir = _debug_dir(self.workdir)
        try:
            debug_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise common.ToolError(
                "debug_start_failed", f"debug 目录不可写: {exc}"
            ) from exc
        bindings = _read_json(debug_dir / "bindings.json") or {}
        if source_pdf:
            bindings["source_pdf"] = str(source_pdf)
        if mono:
            bindings["mono"] = str(mono)
        bindings["updated_at"] = _now()
        path = debug_dir / "bindings.json"
        path.write_text(
            json.dumps(bindings, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return path


def _open_browser(url: str) -> None:
    """自动打开浏览器；失败只警告（stderr），不影响命令返回。"""
    try:
        if not webbrowser.open(url):
            sys.stderr.write(f"debug: 无法自动打开浏览器，请手动访问 {url}\n")
    except Exception as exc:  # noqa: BLE001 - 浏览器缺失不阻断
        sys.stderr.write(f"debug: 打开浏览器失败（{exc}），请手动访问 {url}\n")


def latest_run_id(workdir) -> str | None:
    """最新一个 run 的 id（created_at 倒序的第一个）；无则 ``None``。"""
    from babeldoc_tools import debug_server

    runs = debug_server.list_runs(Path(workdir))
    return runs[0]["run_id"] if runs else None


def debug_block(recorder, viewer: dict | None) -> dict:
    """信封 ``data.debug`` 块（成功与失败都附）。"""
    block = {
        "run_id": getattr(recorder, "run_id", None),
        "url": (viewer or {}).get("url"),
        "manifest": (
            str(recorder.manifest_path)
            if getattr(recorder, "manifest_path", None)
            else None
        ),
        "capture_status": (
            recorder.capture_status if recorder is not None else {"ok": False}
        ),
    }
    if viewer:
        block["viewer"] = {
            "port": viewer.get("port"),
            "pid": viewer.get("pid"),
            "reused": viewer.get("reused"),
        }
    return block


def attach_debug(payload: dict, recorder, viewer: dict | None) -> dict:
    """把 debug 块附到信封 ``data``（不存在则新建）。"""
    if not isinstance(payload, dict):
        return payload
    data = payload.get("data")
    if not isinstance(data, dict):
        data = {}
        payload["data"] = data
    data["debug"] = debug_block(recorder, viewer)
    return payload


@contextlib.contextmanager
def debug_stage(recorder, stage: str, data: dict | None = None):
    """阶段生命周期：``start_stage`` + ``stage_started`` →（异常时
    ``stage_error`` + ``finish_stage(error)``）→ ``finish_stage(ok)``。

    调用方在 ``with`` 块末尾自行补 ``stage_finished`` 事件与 archive（需要
    结果摘要时）。recorder 为 ``None`` 时完全 no-op。
    """
    if recorder is None:
        yield
        return
    recorder.start_stage(stage)
    recorder.record_event(stage, "stage_started", data or {})
    try:
        yield
    except Exception as exc:  # noqa: BLE001 - 记录后原样向上抛
        recorder.record_event(stage, "stage_error", {"error": str(exc)[:500]})
        recorder.finish_stage(stage, "error")
        raise
    recorder.finish_stage(stage, "ok")
