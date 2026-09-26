"""``bdt cloud`` 测试夹具：脚本假引擎（事件形状照真实 syncpdf-cli）、小 PDF、登录客户端。"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("fastapi")
pymupdf = pytest.importorskip("pymupdf")

from babeldoc_tools.cloud.app import create_app  # noqa: E402
from babeldoc_tools.cloud.auth import COOKIE  # noqa: E402
from babeldoc_tools.cloud.auth import create_invite  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

#: 假引擎：``translate`` 按 FAKE_MODE 发事件并把原文复制为译文；``dual`` 复制原文为对照版。
#: FAKE_GATE：发完第一页后等这个文件出现再继续（让测试在「运行中」稳定地取消）；
#: FAKE_PID：把引擎进程号写进这个文件。
FAKE_ENGINE = r'''
import json, os, pathlib, shutil, sys, time
args = sys.argv[1:]
arg = lambda name: args[args.index(name) + 1]
log = os.environ.get("FAKE_LOG")
if log:
    with open(log, "a") as f:
        f.write(json.dumps(args) + "\n")
if args[0] == "dual":
    shutil.copyfile(arg("--source"), arg("--output"))
    sys.exit(0)
if os.environ.get("FAKE_PID"):
    pathlib.Path(os.environ["FAKE_PID"]).write_text(str(os.getpid()))
mode = os.environ.get("FAKE_MODE", "success")
pages = int(os.environ.get("FAKE_PAGES", "2"))
out = pathlib.Path(arg("--output"))
def emit(**event):
    print(json.dumps(event), flush=True)
emit(type="run_started", pages=0)
for stage in ("preflight", "source_analysis", "layout_analysis", "paragraph_analysis"):
    emit(type="stage_started", stage=stage)
    emit(type="stage_finished", stage=stage, elapsed_ms=1)
box = {"x0": 72, "y0": 700, "x1": 300, "y1": 720}
for page in range(1, pages + 1):
    emit(type="paragraph", paragraph_id=f"P{page:02d}-001", page=page, status="pending",
         boxes=[box], coord_system="pdf_user", source_bbox=box)
if mode == "partial":
    emit(type="paragraph", paragraph_id="P01-002", page=1, status="not_replaced",
         boxes=[box], coord_system="pdf_user", source_bbox={"x0": 72, "y0": 600, "x1": 300, "y1": 650})
    emit(type="issue", severity="warning", code="bind_page_unreliable", paragraph_id="P01-002", page=1, message="")
    emit(type="issue", severity="warning", code="coverage_gap", paragraph_id=None, page=pages, message="")
emit(type="stage_started", stage="translating")
if mode == "fail":
    emit(type="error", fatal=True, code="translator_failed", message="boom")
    emit(type="run_finished", ok=False, elapsed_ms=1)
    print("fatal: boom", file=sys.stderr)
    sys.exit(1)
for page in range(1, pages + 1):
    emit(type="paragraph", paragraph_id=f"P{page:02d}-001", page=page, status="typeset",
         boxes=[box], coord_system="pdf_user", source_bbox=box)
    shutil.copyfile(arg("--input"), out)
    emit(type="page_ready", page=page, preview_path=None, revision=page)
    gate = os.environ.get("FAKE_GATE")
    while gate and not os.path.exists(gate):
        time.sleep(0.02)
for stage in ("typesetting", "validating", "publishing"):
    emit(type="stage_started", stage=stage)
emit(type="document_finished", output=str(out), stats={})
emit(type="run_finished", ok=mode == "success", elapsed_ms=5)
sys.exit(0 if mode == "success" else 1)
'''


def make_pdf(path: Path, pages: int = 2, text: str | None = "Hello cloud") -> Path:
    doc = pymupdf.open()
    for number in range(pages):
        page = doc.new_page(width=612, height=792)
        if text:  # 空串 / None：空白页，没有文字层
            page.insert_text((72, 90), f"{text} page {number + 1}")
    doc.save(path)
    doc.close()
    return path


def wait_for(predicate, timeout: float = 10.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.02)
    raise AssertionError("condition not met in time")


def parse_sse(lines) -> list[dict]:
    """``event:``/``id:``/``data:`` 块 → ``[{"id", "event", "data"}]``（心跳注释跳过）。"""
    events, current = [], {}
    for line in lines:
        if not line:
            if current:
                events.append(current)
                if current.get("event") == "end":
                    break
            current = {}
        elif line.startswith("id: "):
            current["id"] = int(line[4:])
        elif line.startswith("event: "):
            current["event"] = line[7:]
        elif line.startswith("data: "):
            current["data"] = json.loads(line[6:])
    return events


class Cloud:
    """一个数据根 + app；``client(name)`` 返回已登录的独立客户端。"""

    def __init__(self, root: Path, engine: Path, tmp: Path) -> None:
        self.root = root
        self.engine = engine
        self.tmp = tmp
        self.app = create_app(root, engine=str(engine), translator="fake:echo", run_worker=False)
        self.service = self.app.state.service
        self.runner = self.app.state.runner
        self.paths = self.app.state.paths
        # 只有这一个客户端跑 lifespan（关闭时才关数据库）；其余客户端只是独立的 cookie jar。
        self._lifespan = TestClient(self.app)
        self._lifespan.__enter__()
        self._server = None
        self._port = 0

    def invite(self, name: str = "tester", quota: int = 5) -> str:
        return create_invite(self.service.db, name, quota)["code"]

    def client(self, name: str = "tester", quota: int = 5) -> TestClient:
        client = TestClient(self.app)
        response = client.post("/api/login", json={"code": self.invite(name, quota)})
        assert response.status_code == 200, response.text
        return client

    def anonymous(self) -> TestClient:
        return TestClient(self.app)

    def upload(self, client: TestClient, pdf: Path, thinking: str = "low", name: str | None = None):
        with pdf.open("rb") as handle:
            return client.post(
                "/api/jobs",
                files={"file": (name or pdf.name, handle, "application/pdf")},
                data={"model": "gemini-3.8-flash", "thinking": thinking},
            )

    def pdf(self, name: str, pages: int = 2, text: str | None = None) -> Path:
        return make_pdf(self.tmp / name, pages, text if text is not None else f"Paper {name}")

    def live(self, client: TestClient):
        """真实 uvicorn + httpx：TestClient 会缓冲整个响应体，读不了进行中的 SSE。"""
        import socket
        import threading

        import httpx
        import uvicorn

        if self._server is None:
            sock = socket.socket()
            sock.bind(("127.0.0.1", 0))
            # lifespan 已由 TestClient 跑过；再跑一次会在退出时关掉共享的数据库
            self._server = uvicorn.Server(uvicorn.Config(self.app, lifespan="off", log_level="warning"))
            threading.Thread(target=self._server.run, kwargs={"sockets": [sock]}, daemon=True).start()
            wait_for(lambda: self._server.started)
            self._port = sock.getsockname()[1]
        # TestClient 的 cookie 挂在 testserver 域上，这里直接带会话头
        cookie = f"{COOKIE}={client.cookies.get(COOKIE)}"
        return httpx.Client(base_url=f"http://127.0.0.1:{self._port}", headers={"Cookie": cookie}, timeout=10)

    def close(self) -> None:
        if self._server is not None:
            self._server.should_exit = True
            self._server = None
        if self._lifespan is not None:
            self._lifespan.__exit__(None, None, None)
            self._lifespan = None


@pytest.fixture
def fake_engine(tmp_path: Path) -> Path:
    script = tmp_path / "syncpdf-cli"
    script.write_text("#!" + sys.executable + "\n" + FAKE_ENGINE, encoding="utf-8")
    script.chmod(0o755)
    return script


@pytest.fixture
def cloud(tmp_path: Path, fake_engine: Path, monkeypatch):
    monkeypatch.setenv("FAKE_LOG", str(tmp_path / "engine-calls.jsonl"))
    instance = Cloud(tmp_path / "root", fake_engine, tmp_path)
    yield instance
    instance.close()
