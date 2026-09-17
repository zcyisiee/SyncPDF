"""查看器端到端（Playwright + 系统 Chrome）：渲染、框对齐、嵌套选择、深链、窄屏。

Playwright 或 Chrome 缺失时 ``importorskip`` 跳过（计划允许：无浏览器时明确
报告未验证，不造假通过）。不依赖网络：fixture 全部本地生成。

运行：
    .venv/bin/python -m pytest tests/test_debug_viewer_e2e.py -q
"""

from __future__ import annotations

import json
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pymupdf
import pytest
from babeldoc_tools import debug_server

playwright_sync = pytest.importorskip(
    "playwright.sync_api", reason="未安装 playwright（浏览器 e2e 未验证）"
)

CHROME_CANDIDATES = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
)

RUN_ID = "20260916T000000Z-abc123"
TOKEN = "tok123"  # noqa: S105 - 测试固定 token
PAGE_W, PAGE_H = 500, 700


def _chrome_path() -> str | None:
    for candidate in CHROME_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    return None


def _build_run(workdir: Path) -> Path:
    """2 页 run：源 PDF、页面几何、layout/段落/选择快照、最终 mono PDF。"""
    run_dir = workdir / "debug" / "runs" / RUN_ID
    for sub in (
        "snapshots/parse",
        "snapshots/build",
        "artifacts/parse",
        "artifacts/build",
    ):
        (run_dir / sub).mkdir(parents=True, exist_ok=True)
    with pymupdf.open() as doc:
        for n in range(2):
            page = doc.new_page(width=PAGE_W, height=PAGE_H)
            page.insert_text((40, 260), f"source page {n + 1}", fontsize=14)
        doc.save(run_dir / "artifacts" / "parse" / "prepared.pdf")
        doc.save(run_dir / "artifacts" / "build" / "mono.pdf")

    frames = [
        {
            "page_index": i,
            "width": PAGE_W,
            "height": PAGE_H,
            "mediabox": [0, 0, PAGE_W, PAGE_H],
            "cropbox": [0, 0, PAGE_W, PAGE_H],
            "rotation": 0,
        }
        for i in range(2)
    ]
    (run_dir / "snapshots" / "parse" / "page-frames.json").write_text(
        json.dumps({"version": 1, "frames": frames}), encoding="utf-8"
    )
    (run_dir / "snapshots" / "parse" / "layout.json").write_text(
        json.dumps(
            {
                "version": 1,
                "pages": [
                    {
                        "page_index": i,
                        "height": PAGE_H,
                        "entities": [
                            {
                                "id": f"L{i + 1:02d}-001",
                                "label": "text",
                                "kind": "layout_region",
                                "page": i + 1,
                                "box": {"x0": 30, "y0": 50, "x1": 300, "y1": 190},
                                "attrs": {"conf": 0.99},
                            },
                            {
                                "id": f"L{i + 1:02d}-002",
                                "label": "unknown_nested",
                                "kind": "layout_region",
                                "page": i + 1,
                                "box": {"x0": 65, "y0": 80, "x1": 120, "y1": 120},
                                "attrs": {"conf": 0.7},
                            },
                        ],
                    }
                    for i in range(2)
                ],
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "snapshots" / "parse" / "paragraphs.json").write_text(
        json.dumps(
            {
                "entities": [
                    {
                        "id": "P01-001",
                        "page": 1,
                        "label": "text",
                        "box": {"x0": 75, "y0": 90, "x1": 100, "y1": 110},
                        "attrs": {"unicode": "SOURCE_TEXT"},
                    }
                ],
                "relations": [
                    {
                        "from_id": "P01-001",
                        "to_id": "L01-001",
                        "kind": "in_layout",
                        "method": "explicit",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "snapshots" / "parse" / "selection.json").write_text(
        json.dumps(
            {
                "selected": [{"id": "P01-001", "page": 0, "source": "SOURCE_TEXT"}],
                "skipped": [],
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "snapshots" / "build" / "typesetting_geometry.json").write_text(
        json.dumps(
            {
                "page_info": [{"cropbox": [0, 0, PAGE_W, PAGE_H]} for _ in range(2)],
                "paragraphs": [
                    {
                        "id": "P01-001",
                        "page": 1,
                        "layout_label": "text",
                        "src_box": [75, 590, 100, 610],
                        "layout_box": [75, 590, 100, 610],
                        "rendered_box": [80, 594, 96, 606],
                        "text": "译文",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "artifacts" / "build" / "latex_bbox_report.json").write_text(
        json.dumps(
            {"decisions": [{"debug_id": "P01-001", "reason": "applied", "stamp_box": [75, 90, 100, 110]}]}
        ),
        encoding="utf-8",
    )
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "run_id": RUN_ID,
                "created_at": "2026-09-16T00:00:00+00:00",
                "finished_at": "2026-09-16T00:01:00+00:00",
                "status": "finished",
                "stages": {"parse": {"status": "ok"}, "build": {"status": "ok"}},
                "artifacts": {
                    "artifacts/parse/prepared.pdf": {"path": "artifacts/parse/prepared.pdf"},
                    "artifacts/build/mono.pdf": {"path": "artifacts/build/mono.pdf"},
                },
            }
        ),
        encoding="utf-8",
    )
    (run_dir / "events.jsonl").write_text(
        json.dumps({"seq": 1, "stage": "parse", "kind": "stage_started", "data": {}}) + "\n",
        encoding="utf-8",
    )
    return run_dir


@pytest.fixture
def viewer(tmp_path):
    chrome = _chrome_path()
    if chrome is None:
        pytest.skip("未找到 Chrome/Chromium（浏览器 e2e 未验证）")
    workdir = tmp_path / "wd"
    _build_run(workdir)
    server = ThreadingHTTPServer(("127.0.0.1", 0), debug_server._Handler)
    server.daemon_threads = True
    server.debug_ctx = debug_server._Context(workdir, TOKEN)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        yield f"http://127.0.0.1:{port}/?token={TOKEN}#run={RUN_ID}&view=layout&page=1"
    finally:
        server.shutdown()
        server.server_close()
        server.debug_ctx.renderer.close()


def _loaded(page) -> None:
    page.wait_for_function("() => document.querySelector('.page img')?.naturalWidth > 0")
    page.wait_for_function(
        "() => document.querySelectorAll('.overlay[data-mounted] .box').length > 0"
    )


def test_viewer_renders_boxes_and_aligns(viewer):
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=_chrome_path(), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            errors: list[str] = []
            page.on("pageerror", lambda exc: errors.append(str(exc)))
            page.goto(viewer)
            _loaded(page)
            assert page.locator(".page").count() == 2
            assert page.locator('.box[data-eid="L01-001"]').count() == 1

            geometry = page.locator('.box[data-eid="L01-001"]').evaluate(
                """el => {
                    const box = el.getBoundingClientRect();
                    const image = el.closest('.page').querySelector('img').getBoundingClientRect();
                    return [(box.x - image.x) / image.width, (box.y - image.y) / image.height,
                            box.width / image.width, box.height / image.height];
                }"""
            )
            expected = [30 / PAGE_W, 50 / PAGE_H, 270 / PAGE_W, 140 / PAGE_H]
            assert max(abs(a - b) for a, b in zip(geometry, expected, strict=True)) < 0.01, geometry
            assert errors == []
        finally:
            browser.close()


def test_viewer_nested_selection_and_deep_link(viewer):
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=_chrome_path(), headless=True)
        try:
            page = browser.new_page(viewport={"width": 1440, "height": 1000})
            page.goto(viewer)
            _loaded(page)
            overlay = page.locator(".overlay").first.bounding_box()
            px = overlay["x"] + overlay["width"] * 85 / PAGE_W
            py = overlay["y"] + overlay["height"] * 100 / PAGE_H
            page.mouse.click(px, py)
            hits = page.locator("#hitList button")
            assert hits.count() == 3
            hits.filter(has_text="P01-001").click()
            assert page.locator('.box.is-selected[data-eid="P01-001"]').count() >= 1

            # 同文档内 entity 深链：切到 compile 再切回，选择应保留。
            page.locator('[data-view="compile"]').click()
            page.wait_for_selector("#compile-detail")
            page.wait_for_timeout(300)
            assert page.locator(".box.is-selected").count() >= 1
            page.locator('[data-view="layout"]').click()
            page.wait_for_timeout(300)
            assert page.locator('.box.is-selected[data-eid="P01-001"]').count() >= 1
        finally:
            browser.close()


def test_viewer_mobile_width_fits(viewer):
    with playwright_sync.sync_playwright() as pw:
        browser = pw.chromium.launch(executable_path=_chrome_path(), headless=True)
        try:
            page = browser.new_page(viewport={"width": 390, "height": 844})
            page.goto(viewer)
            _loaded(page)
            width = page.evaluate(
                "() => ({page: document.querySelector('.page').getBoundingClientRect().width,"
                " stage: document.querySelector('#stage').clientWidth})"
            )
            assert width["page"] <= width["stage"], width
            page.locator("#panelToggle").click()
            assert page.locator("#workbench").evaluate(
                "el => el.classList.contains('collapsed')"
            )
        finally:
            browser.close()
