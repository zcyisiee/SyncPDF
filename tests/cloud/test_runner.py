"""引擎事件 → 用户进度的映射，以及 done / partial / failed 的判定。"""

from __future__ import annotations

import gzip
import json
from pathlib import Path

import pytest
from babeldoc_tools import rust_backend
from babeldoc_tools.cloud.runner import BOX_PAD
from babeldoc_tools.cloud.runner import EventMapper
from babeldoc_tools.cloud.runner import page_geometry

REPO = Path(__file__).resolve().parents[2]


def _identity(page: int, rect):
    return [page, *rect]


def _paragraph(pid: str, page: int, status: str, y: float = 100.0) -> dict:
    bbox = {"x0": 10.0, "y0": y, "x1": 50.0, "y1": y + 10}
    return {"type": "paragraph", "paragraph_id": pid, "page": page, "status": status, "source_bbox": bbox}


def _feed(mapper: EventMapper, events: list[dict]) -> list[tuple[str, dict]]:
    return [out for event in events for out in mapper.feed(event)]


def test_stage_boundaries_become_five_steps_and_milestones():
    mapper = EventMapper(4, 1, _identity)
    out = _feed(mapper, [
        {"type": "stage_started", "stage": s}
        for s in ("preflight", "source_analysis", "layout_analysis", "paragraph_analysis", "translating",
                  "typesetting", "validating", "publishing")
    ])
    assert [p["step"] for k, p in out if k == "step"] == [1, 2, 3, 4]
    assert [p["text"] for k, p in out if k == "milestone"] == ["解析完成，共 4 页", "版面识别完成", "开始翻译"]
    # 引擎直接跳过阶段时，经过的里程碑一次补齐
    skipped = _feed(EventMapper(1, 1, _identity), [{"type": "stage_started", "stage": "translating"}])
    assert [p.get("text", p.get("step")) for _, p in skipped] == ["解析完成，共 1 页", "版面识别完成", "开始翻译", 2]


def test_page_ready_counts_distinct_pages_and_marks_half_once():
    mapper = EventMapper(4, 3, _identity)
    out = _feed(mapper, [
        {"type": "page_ready", "page": 3, "revision": 1},
        {"type": "page_ready", "page": 1, "revision": 2},
        {"type": "page_ready", "page": 3, "revision": 3},  # 同页重发：只刷新 rev
        {"type": "page_ready", "page": 2, "revision": 4},
    ])
    pages = [p for k, p in out if k == "page"]
    assert [(p["page"], p["rev"], p["done"], p.get("text")) for p in pages] == [
        (3, "3.1", 1, "第一页译文已就绪"),
        (1, "3.2", 2, "第 1 页已完成"),
        (3, "3.3", 2, None),
        (2, "3.4", 3, "第 2 页已完成"),
    ]
    assert [p["text"] for k, p in out if k == "milestone"] == ["完成 50%"]
    assert [k for k, _ in out].index("milestone") == 2  # 紧跟第二页


def test_retained_paragraphs_become_boxes_and_warnings():
    """保留原文 = 回退，或翻译前被判定不可替换；同一判据既画框也计提醒。"""
    mapper = EventMapper(3, 1, _identity)
    code = sorted(rust_backend.BLOCKED_ISSUE_CODES)[0]
    out = _feed(mapper, [
        _paragraph("P01-001", 1, "typeset"),
        _paragraph("P01-002", 1, "not_replaced"),  # 公式等本来就不翻译：不是提醒
        _paragraph("P02-001", 2, "not_replaced", y=200),
        {"type": "issue", "code": code, "paragraph_id": "P02-001", "page": 2},
        _paragraph("P02-002", 2, "fallback", y=300),
        _paragraph("P03-001", 3, "translated"),  # 页没发布：收尾时算保留
        {"type": "issue", "code": "coverage_gap", "paragraph_id": None, "page": 3},
        {"type": "page_ready", "page": 1, "revision": 1},
        {"type": "page_ready", "page": 2, "revision": 2},
    ])
    live = {p["page"]: p["boxes"] for k, p in out if k == "page"}
    assert live == {1: [], 2: [[2, 10.0, 200.0, 50.0, 210.0], [2, 10.0, 300.0, 50.0, 310.0]]}

    events, stats = mapper.finish("partial", 12.34)
    warns = [p["text"] for k, p in events if k == "warn"]
    assert warns == ["第 2 页有 2 段保留原文", "第 3 页有 1 段保留原文", "第 3 页有少量内容未识别"]
    assert stats["warnings"] == 3 and stats["warn_pages"] == [2, 3]
    assert stats["boxes"]["2"] == live[2] and len(stats["boxes"]["3"]) == 1
    assert [p["text"] for k, p in events if k == "milestone"] == ["全部完成", "可以下载"]


def test_page_geometry_maps_pdf_user_rects_to_visible_page_fractions(tmp_path):
    import pymupdf

    path = tmp_path / "rot.pdf"
    doc = pymupdf.open()
    doc.new_page(width=600, height=800)
    rotated = doc.new_page(width=600, height=800)
    rotated.set_rotation(90)
    doc.save(path)
    to_fraction = page_geometry(path)
    # pdf_user 原点在左下：y1 越大越靠上
    left, top, width, height = to_fraction(1, (100 + BOX_PAD, 700 + BOX_PAD, 400 - BOX_PAD, 760 - BOX_PAD))
    assert (left, top, width, height) == pytest.approx((100 / 600, 40 / 800, 300 / 600, 60 / 800), abs=1e-4)
    # 旋转页：比例相对旋转后的可见页面，仍在 [0, 1] 内
    frac = to_fraction(2, (100, 700, 400, 760))
    assert all(0 <= v <= 1 for v in frac)
    assert frac[2:] == pytest.approx([(60 + 2 * BOX_PAD) / 800, (300 + 2 * BOX_PAD) / 600], abs=1e-4)


def _job(cloud, client, name: str, pages: int = 2):
    job = cloud.upload(client, cloud.pdf(name, pages)).json()
    assert cloud.runner.run_once()
    return client.get(f"/api/jobs/{job['id']}").json()


def test_partial_run_keeps_output_with_warnings(cloud, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "partial")
    client = cloud.client()
    job = _job(cloud, client, "partial.pdf")
    assert job["status"] == "partial"
    assert job["stats"]["warn_pages"] == [1, 2] and job["stats"]["warnings"] == 2
    assert len(job["stats"]["boxes"]["1"]) == 1
    assert client.get(f"/api/jobs/{job['id']}/download").status_code == 200
    items = client.get("/api/jobs").json()["items"]
    assert items[0]["warnings"] == 2
    tid = cloud.service.db.one("SELECT translation_id FROM jobs WHERE id = ?", (job["id"],))[0]
    raw = gzip.decompress((cloud.paths.translations / tid / "events.jsonl.gz").read_bytes())
    assert b"coverage_gap" in raw
    assert list(cloud.paths.work.iterdir()) == []


def test_failed_run_has_no_download_and_keeps_diagnostics(cloud, monkeypatch):
    monkeypatch.setenv("FAKE_MODE", "fail")
    client = cloud.client()
    job = _job(cloud, client, "fail.pdf")
    assert job["status"] == "failed" and job["final_rev"] is None
    assert job["stats"]["error"] == "engine_incomplete"
    events = cloud.service.events_after(job["id"], 0)
    assert [e for e in events if e["kind"] == "error"][0]["text"] == "翻译没有完成"
    last_step = [e for e in events if e["kind"] == "step"][-1]
    assert (last_step["step"], last_step["fail"]) == (2, True)  # 停在「翻译」这一步
    assert client.get(f"/api/jobs/{job['id']}/download").status_code == 409
    result = json.loads(cloud.service.db.one("SELECT result_json FROM translations")[0])
    assert "fatal: boom" in result["stderr_tail"] and result["fatal"] == "boom"


def test_runner_passes_cloud_typesetting_options_to_engine(cloud):
    client = cloud.client()
    _job(cloud, client, "args.pdf")
    calls = [json.loads(line) for line in (cloud.tmp / "engine-calls.jsonl").read_text().splitlines()]
    args = calls[-1]
    pairs = {args[i]: args[i + 1] for i in range(1, len(args) - 1) if args[i].startswith("--")}
    assert pairs["--model"] == "gemini-3.8-flash" and pairs["--thinking"] == "low"
    assert pairs["--line-height"] == "1.5" and pairs["--target-lang"] == "zh-CN"
    assert "--dual-output" not in args and "--font-scale" not in args


@pytest.mark.parametrize("thinking", ["low", "medium", "high"])
@pytest.mark.parametrize(
    ("translator", "model", "passes_thinking"),
    [("agy", "gemini-3.8-flash-{t}", False), ("pi", "gemini-3.8-flash", True)],
)
def test_runner_maps_thinking_per_translator(cloud, translator, model, passes_thinking, thinking):
    """agy 拒绝 --thinking，强度并入模型名后缀；pi 仍单独传 --thinking。"""
    cloud.runner.translator = translator
    client = cloud.client()
    cloud.upload(client, cloud.pdf("map.pdf"), thinking=thinking)
    assert cloud.runner.run_once()
    args = json.loads((cloud.tmp / "engine-calls.jsonl").read_text().splitlines()[-1])
    assert args[args.index("--translator") + 1] == translator
    assert args[args.index("--model") + 1] == model.format(t=thinking)
    assert ("--thinking" in args) is passes_thinking
    if passes_thinking:
        assert args[args.index("--thinking") + 1] == thinking
    # 缓存键仍按界面上的（模型, 强度）记录
    assert tuple(cloud.service.db.one("SELECT model, thinking FROM translations")) == ("gemini-3.8-flash", thinking)


@pytest.mark.skipif(
    not (rust_backend._ENGINE.is_file() and (REPO / "engine/fixtures/ci-test.pdf").is_file()),
    reason="需要 release syncpdf-cli 与 engine/fixtures/ci-test.pdf",
)
def test_real_engine_translates_and_exports_dual(tmp_path):
    """真实引擎 + fake:echo：完整跑一篇，再按需导出对照版（A3 横向、页数一致）。"""
    import pymupdf
    from babeldoc_tools.cloud.app import create_app
    from babeldoc_tools.cloud.auth import create_invite
    from fastapi.testclient import TestClient

    app = create_app(tmp_path / "root", translator="fake:echo", run_worker=False)
    with TestClient(app) as client:
        code = create_invite(app.state.service.db, "real", 5)["code"]
        client.post("/api/login", json={"code": code})
        source = REPO / "engine/fixtures/ci-test.pdf"
        with source.open("rb") as handle:
            job = client.post(
                "/api/jobs",
                files={"file": ("ci-test.pdf", handle, "application/pdf")},
                data={"model": "gemini-3.8-flash", "thinking": "low"},
            ).json()
        assert app.state.runner.run_once()
        view = client.get(f"/api/jobs/{job['id']}").json()
        assert view["status"] in ("done", "partial"), view
        pages = pymupdf.open(source).page_count
        dual = client.get(f"/api/jobs/{job['id']}/download?kind=dual")
        assert dual.status_code == 200
        out = tmp_path / "dual.pdf"
        out.write_bytes(dual.content)
        with pymupdf.open(out) as doc:
            assert doc.page_count == pages
            assert doc[0].rect.width > doc[0].rect.height
