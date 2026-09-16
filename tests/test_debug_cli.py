"""``bdt --debug`` / ``bdt debug`` 的 CLI 生命周期测试（离线确定性）。

覆盖：查看器 spawn/复用/--stop、write.lock 互斥、--debug 信封 debug 块、
--debug-recompile 互斥校验、查看器 API token 校验。查看器进程是真实
``python -m babeldoc_tools --debug-serve-internal`` 子进程；测试结束一律
``--stop`` 清理（fixture teardown 兜底）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from babeldoc import debug_recorder as _dr
from babeldoc_tools import __main__ as cli
from babeldoc_tools import debug_runtime
from babeldoc_tools import debug_server

REPO_ROOT = Path(__file__).resolve().parents[1]


def _cli(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 - argv 由测试构造
        [sys.executable, "-m", "babeldoc_tools", *argv],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
        timeout=60,
    )


def _main(*argv: str) -> dict:
    """进程内跑 ``bdt``；返回解析后的 stdout JSON 信封。"""
    import contextlib
    import io

    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        code = cli.main(list(argv))
    payload = json.loads(out.getvalue().strip().splitlines()[-1])
    payload["_exit"] = code
    return payload


@pytest.fixture
def workdir(tmp_path: Path):
    wd = tmp_path / "wd"
    wd.mkdir()
    yield wd
    # 兜底清理：保证测试不遗留查看器进程。
    debug_runtime.DebugSession(wd).stop_viewer()


def _viewer_json(workdir: Path) -> dict:
    return json.loads((workdir / "debug" / "viewer.json").read_text("utf-8"))


# --------------------------------------------------------------------------- #
# bdt debug：启动 / 复用 / 停止
# --------------------------------------------------------------------------- #
def test_debug_start_reuse_stop(tmp_path):
    workdir = tmp_path / "empty-wd"  # 目录不存在也能启动（serve 自建 runs/）
    try:
        first = _main("debug", "--workdir", str(workdir), "--no-open")
        assert first["ok"] is True
        assert first["_exit"] == 0
        assert first["data"]["url"].startswith("http://127.0.0.1:")
        assert first["data"]["reused"] is False
        state = _viewer_json(workdir)
        assert state["port"] == first["data"]["port"]

        second = _main("debug", "--workdir", str(workdir), "--no-open")
        assert second["data"]["reused"] is True
        assert second["data"]["port"] == first["data"]["port"]

        stopped = _main("debug", "--workdir", str(workdir), "--stop")
        assert stopped["data"]["stopped"] is True
        assert not (workdir / "debug" / "viewer.json").exists()

        third = _main("debug", "--workdir", str(workdir), "--no-open")
        assert third["data"]["reused"] is False

        again = _main("debug", "--workdir", str(workdir), "--stop")
        assert again["data"]["stopped"] is True
        # 再停一次：not_running
        final = _main("debug", "--workdir", str(workdir), "--stop")
        assert final["data"]["stopped"] is False
        assert final["data"]["reason"] == "not_running"
    finally:
        debug_runtime.DebugSession(workdir).stop_viewer()


def test_debug_stale_viewer_state_replaced(workdir):
    """伪造 viewer.json（pid 不存在）→ start 重写为新 pid/port。"""
    stale = {"pid": 999999, "port": 9, "token": "x", "started_at": "x"}
    (workdir / "debug").mkdir()
    (workdir / "debug" / "viewer.json").write_text(json.dumps(stale))
    out = _main("debug", "--workdir", str(workdir), "--no-open")
    assert out["ok"] is True
    state = _viewer_json(workdir)
    assert state["pid"] != 999999
    assert state["port"] == out["data"]["port"]


# --------------------------------------------------------------------------- #
# --debug 信封与归档
# --------------------------------------------------------------------------- #
def test_parse_debug_envelope_and_archive(tmp_path, workdir, monkeypatch):
    """``parse --debug``：信封带 data.debug、manifest finished、events 非空。"""
    from babeldoc.tools.agent import markdown_view

    def fake_extract(pdf_path, workdir, *args, **kwargs):  # noqa: ARG001
        agent = Path(workdir) / "agent"
        agent.mkdir(parents=True, exist_ok=True)
        (agent / "document.md").write_text("# doc\n", encoding="utf-8")
        (agent / "anchors.json").write_text("{}", encoding="utf-8")
        (agent / "sheet.jsonl").write_text("", encoding="utf-8")
        return {
            "document_md": str(agent / "document.md"),
            "paragraphs": 1,
            "chars": 5,
            "label_counts": {"text": 1},
            "skipped_label_counts": {},
        }

    monkeypatch.setattr(markdown_view, "extract_markdown", fake_extract)
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 stub\n")

    payload = _main(
        "parse",
        str(pdf),
        "--workdir",
        str(workdir),
        "--debug",
        "--debug-no-open",
    )
    assert payload["ok"] is True
    block = payload["data"]["debug"]
    assert block["run_id"]
    assert block["url"].startswith("http://127.0.0.1:")
    assert Path(block["manifest"]).is_file()

    manifest = json.loads(Path(block["manifest"]).read_text("utf-8"))
    assert manifest["status"] == "finished"
    assert manifest["stages"]["parse"]["status"] == "ok"
    events = _dr.read_events(Path(block["manifest"]).parent)
    kinds = [event["kind"] for event in events]
    assert "stage_started" in kinds and "stage_finished" in kinds
    # 产物归档（document.md 复制进 artifacts/parse/）
    run_dir = Path(block["manifest"]).parent
    assert (run_dir / "artifacts" / "parse" / "document.md").is_file()


def test_debug_write_lock_excludes_second_writer(workdir):
    """手动 flock write.lock 后，--debug 命令报 debug_busy，不跑 pipeline。"""
    import fcntl

    debug_dir = workdir / "debug"
    debug_dir.mkdir()
    lock_path = debug_dir / "write.lock"
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        session = debug_runtime.DebugSession(workdir)
        with pytest.raises(Exception) as excinfo:
            session.create_run(config={"stages": ["parse"]})
        assert getattr(excinfo.value, "code", None) == "debug_busy"
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def test_debug_run_failure_keeps_debug_block(tmp_path, workdir, monkeypatch):
    """pipeline 失败时 debug 块仍附在信封里，manifest 标 error。"""
    from babeldoc.tools.agent import markdown_view

    def boom(pdf_path, workdir, *args, **kwargs):  # noqa: ARG001
        raise RuntimeError("模拟解析失败")

    monkeypatch.setattr(markdown_view, "extract_markdown", boom)
    pdf = tmp_path / "paper.pdf"
    pdf.write_bytes(b"%PDF-1.4 stub\n")
    payload = _main(
        "parse", str(pdf), "--workdir", str(workdir), "--debug", "--debug-no-open"
    )
    assert payload["ok"] is False
    block = payload["data"]["debug"]
    manifest = json.loads(Path(block["manifest"]).read_text("utf-8"))
    assert manifest["status"] == "error"
    assert manifest["stages"]["parse"]["status"] == "error"


# --------------------------------------------------------------------------- #
# --debug-recompile 校验（argparse 用法错误 → exit 2）
# --------------------------------------------------------------------------- #
def test_debug_recompile_requires_debug():
    out = _cli("build", "--workdir", "wd", "--debug-recompile")
    assert out.returncode == 2
    assert "--debug" in out.stderr


def test_debug_recompile_conflicts_no_latex_bbox():
    out = _cli(
        "build", "--workdir", "wd", "--debug", "--debug-recompile", "--no-latex-bbox"
    )
    assert out.returncode == 2
    assert "--no-latex-bbox" in out.stderr


# --------------------------------------------------------------------------- #
# 查看器 HTTP 服务：token / Host / 路由（进程内起服务，不走 spawn）
# --------------------------------------------------------------------------- #
@pytest.fixture
def served(tmp_path):
    workdir = tmp_path / "wd"
    (workdir / "debug" / "runs").mkdir(parents=True)
    server = ThreadingHTTPServer(("127.0.0.1", 0), debug_server._Handler)
    server.daemon_threads = True
    server.debug_ctx = debug_server._Context(workdir, "tok123")
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield workdir, server.server_address[1]
    server.shutdown()
    server.server_close()


def _get(port: int, path: str, token: str | None = "tok123"):  # noqa: S107
    url = f"http://127.0.0.1:{port}{path}"
    if token and "token=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}token={token}"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:  # noqa: S310
            body = resp.read()
            try:
                return resp.status, json.loads(body)
            except ValueError:
                return resp.status, body
    except urllib.error.HTTPError as exc:
        return exc.code, {}


def test_server_token_required(served):
    _, port = served
    status, _ = _get(port, "/api/v1/runs", token=None)
    assert status == 403
    status, body = _get(port, "/api/v1/runs?token=wrong")
    assert status == 403
    status, body = _get(port, "/api/v1/runs")
    assert status == 200
    assert body["ok"] is True
    assert body["runs"] == []


def test_server_runs_and_events_and_artifact_whitelist(served):
    workdir, port = served
    run_dir = workdir / "debug" / "runs" / "20260916T000000Z-abc123"
    (run_dir / "artifacts" / "parse").mkdir(parents=True)
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": run_dir.name,
                "created_at": "2026-09-16T00:00:00+00:00",
                "status": "finished",
                "stages": {"parse": {"status": "ok"}},
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "events.jsonl").write_text(
        '{"seq": 1, "kind": "a"}\n{"seq": 2, "kind": "b"}\n{"seq": 3, "kin',
        encoding="utf-8",
    )
    (run_dir / "artifacts" / "parse" / "document.md").write_text(
        "# doc\n", encoding="utf-8"
    )

    status, body = _get(port, "/api/v1/runs")
    assert status == 200
    assert body["runs"][0]["run_id"] == run_dir.name
    assert body["runs"][0]["status"] == "finished"

    status, body = _get(port, f"/api/v1/runs/{run_dir.name}/manifest")
    assert status == 200
    assert body["manifest"]["stages"]["parse"]["status"] == "ok"

    # 增量事件 + 半行容错
    status, body = _get(port, f"/api/v1/runs/{run_dir.name}/events?after_seq=1")
    assert status == 200
    assert [e["seq"] for e in body["events"]] == [2]
    assert body["last_seq"] == 2

    # 坏 run_id / 不存在 run → 404
    status, _ = _get(port, "/api/v1/runs/..%2F..%2Fetc/manifest")
    assert status in (403, 404)
    status, _ = _get(port, "/api/v1/runs/20260916T000000Z-ffffff/manifest")
    assert status == 404

    # artifact 白名单：允许 artifacts/ 内文件
    status, body = _get(
        port, f"/api/v1/artifacts/{run_dir.name}/artifacts/parse/document.md"
    )
    assert status == 200
    # 路径穿越拒绝
    status, _ = _get(
        port, f"/api/v1/artifacts/{run_dir.name}/artifacts/../../manifest.json"
    )
    assert status in (403, 404)
    # 白名单外目录（如 manifest.json 本体不在 artifacts/snapshots 下）拒绝
    status, _ = _get(port, f"/api/v1/artifacts/{run_dir.name}/manifest.json")
    assert status in (403, 404)


def test_heartbeat_ok(served):
    _, port = served
    status, body = _get(port, "/api/v1/heartbeat")
    assert status == 200
    assert body["ok"] is True


def test_help_lists_debug_subcommand():
    out = _cli("--help")
    assert out.returncode == 0
    for command in (
        "parse",
        "translate",
        "apply",
        "build",
        "check",
        "layout-set",
        "report",
        "run",
        "debug",
    ):
        assert command in out.stdout
    assert "debug-serve-internal" not in out.stdout
