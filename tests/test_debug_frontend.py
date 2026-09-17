"""查看器服务端契约（无浏览器）：静态资源、渲染端点、增量事件、CSP/白名单。

渲染走真实 ``--debug-render-internal`` 子进程（PyMuPDF 不跨线程共享），
因此这里需要一个真实 PDF artifact；其余全部离线。
"""

from __future__ import annotations

import json
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

import pymupdf
import pytest
from babeldoc_tools import debug_server

RUN_ID = "20260916T000000Z-abc123"
TOKEN = "tok123"  # noqa: S105 - 测试固定 token


def _build_run(workdir: Path) -> Path:
    run_dir = workdir / "debug" / "runs" / RUN_ID
    (run_dir / "snapshots" / "parse").mkdir(parents=True)
    (run_dir / "artifacts" / "parse").mkdir(parents=True)
    (run_dir / "artifacts" / "build").mkdir(parents=True)
    with pymupdf.open() as doc:
        for n in range(2):
            page = doc.new_page(width=500, height=700)
            page.insert_text((40, 260), f"page {n + 1}", fontsize=14)
        doc.save(run_dir / "artifacts" / "parse" / "prepared.pdf")
        doc.save(run_dir / "artifacts" / "build" / "mono.pdf")
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": RUN_ID,
                "created_at": "2026-09-16T00:00:00+00:00",
                "finished_at": "2026-09-16T00:01:00+00:00",
                "status": "finished",
                "stages": {"parse": {"status": "ok"}},
                "artifacts": {
                    "artifacts/parse/prepared.pdf": {"path": "artifacts/parse/prepared.pdf"},
                    "artifacts/build/mono.pdf": {"path": "artifacts/build/mono.pdf"},
                },
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "events.jsonl").write_text(
        "".join(json.dumps({"seq": i, "kind": f"e{i}"}) + "\n" for i in range(1, 11)),
        encoding="utf-8",
    )
    (run_dir / "snapshots" / "parse" / "page-frames.json").write_text(
        json.dumps(
            {
                "frames": [
                    {
                        "page_index": i,
                        "width": 500,
                        "height": 700,
                        "cropbox": [0, 0, 500, 700],
                        "rotation": 0,
                    }
                    for i in range(2)
                ]
            }
        ),
        encoding="utf-8",
    )
    return run_dir


@pytest.fixture
def served(tmp_path):
    workdir = tmp_path / "wd"
    _build_run(workdir)
    server = ThreadingHTTPServer(("127.0.0.1", 0), debug_server._Handler)
    server.daemon_threads = True
    server.debug_ctx = debug_server._Context(workdir, TOKEN)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield workdir, server.server_address[1]
    server.shutdown()
    server.server_close()
    server.debug_ctx.renderer.close()


def _get(port: int, path: str, token: str | None = TOKEN, headers=None):
    url = f"http://127.0.0.1:{port}{path}"
    if token and "token=" not in url:
        sep = "&" if "?" in url else "?"
        url = f"{url}{sep}token={token}"
    request = urllib.request.Request(url, headers=headers or {})  # noqa: S310 - 固定 127.0.0.1
    try:
        with urllib.request.urlopen(request, timeout=15) as resp:  # noqa: S310
            return resp.status, resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


def test_static_index_and_csp(served):
    _, port = served
    status, body, headers = _get(port, "/", token=None)
    assert status == 200
    assert b"<html" in body.lower() or b"<!doctype" in body.lower()
    assert "content-security-policy" in {k.lower() for k in headers}
    assert headers.get("X-Content-Type-Options") == "nosniff"


def test_render_endpoint_png_and_dpi_bounds(served):
    _, port = served
    status, body, headers = _get(
        port, f"/api/v1/runs/{RUN_ID}/render/1.png?pdf=parse/prepared.pdf&dpi=110"
    )
    assert status == 200, body[:200]
    assert headers["Content-Type"] == "image/png"
    assert body[:8] == b"\x89PNG\r\n\x1a\n"

    for bad in ("10", "500"):
        status, _, _ = _get(
            port, f"/api/v1/runs/{RUN_ID}/render/1.png?pdf=parse/prepared.pdf&dpi={bad}"
        )
        assert status == 400, bad


def test_render_pdf_whitelist_rejects_traversal_and_outside(served):
    _, port = served
    status, _, _ = _get(
        port, f"/api/v1/runs/{RUN_ID}/render/1.png?pdf=../manifest.json&dpi=110"
    )
    assert status == 400  # 非 .pdf
    status, _, _ = _get(
        port, f"/api/v1/runs/{RUN_ID}/render/1.png?pdf=../manifest.pdf&dpi=110"
    )
    assert status == 404  # 路径穿越
    status, _, _ = _get(
        port, f"/api/v1/runs/{RUN_ID}/render/1.png?pdf=build/missing.pdf&dpi=110"
    )
    assert status == 404


def test_events_incremental_after_seq(served):
    _, port = served
    status, body, _ = _get(port, f"/api/v1/runs/{RUN_ID}/events?after_seq=0")
    assert status == 200
    payload = json.loads(body)
    assert [e["seq"] for e in payload["events"]] == list(range(1, 11))
    assert payload["last_seq"] == 10

    status, body, _ = _get(port, f"/api/v1/runs/{RUN_ID}/events?after_seq=5")
    payload = json.loads(body)
    assert [e["seq"] for e in payload["events"]] == [6, 7, 8, 9, 10]
    assert payload["last_seq"] == 10


def test_snapshot_and_artifact_whitelist(served):
    _, port = served
    status, body, _ = _get(port, f"/api/v1/runs/{RUN_ID}/snapshot/parse/page-frames.json")
    assert status == 200
    assert json.loads(body)["frames"][0]["width"] == 500
    status, _, _ = _get(port, f"/api/v1/runs/{RUN_ID}/snapshot/parse/missing.json")
    assert status == 404

    status, body, headers = _get(
        port, f"/api/v1/artifacts/{RUN_ID}/artifacts/parse/prepared.pdf"
    )
    assert status == 200
    assert headers["Content-Type"] == "application/pdf"
    assert "inline" in headers.get("Content-Disposition", "")
    status, _, _ = _get(port, f"/api/v1/artifacts/{RUN_ID}/artifacts/../manifest.json")
    assert status in (403, 404)


def test_cross_origin_local_port_rejected(served):
    """Origin 必须与请求同源（scheme/host/port 全一致），只比对 hostname 不够。"""
    _, port = served
    status, _, _ = _get(
        port,
        "/api/v1/runs",
        headers={"Origin": "http://127.0.0.1:1"},
    )
    assert status == 403
    status, _, _ = _get(
        port,
        "/api/v1/runs",
        headers={"Origin": f"http://127.0.0.1:{port}"},
    )
    assert status == 200
