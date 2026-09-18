"""``bdt serve`` 的只读文档端点：列表 / 详情 / 阶段 / 段落 / 几何 / 检查。

测试自包含：fixture 用代码造最小 workdir（不读 ``tmp/`` 下的真实产物，不联网、
不启动 uvicorn），覆盖正常路径、缺产物降级、数量不一致 join、page 过滤、
``coord_system`` 标注、无快照 404、stage-state 的 manifest fallback。
"""

from __future__ import annotations

import datetime
import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="serve 需要 web extra: fastapi")

from babeldoc_tools.serve.app import create_app  # noqa: E402
from babeldoc_tools.serve.schemas import API_PREFIX  # noqa: E402
from babeldoc_tools.serve.schemas import STAGES  # noqa: E402
from babeldoc_tools.serve.store import DocumentStore  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# 写端点白名单是唯一来源：W07 只放行 job 的两条 POST，W08 加上传与 profile 写入
# （见那个模块的 ALLOWED_WRITE_ROUTES）。
from test_serve_app import assert_no_unexpected_write_routes  # noqa: E402

DID = "paper"
BARE = "bare"
RUN_ID = "20260917T100000Z-0000a1"

DOCUMENTS = f"{API_PREFIX}/documents"
DETAIL = f"{DOCUMENTS}/{DID}"
STAGE_STATE = f"{DETAIL}/stage-state"
PARAGRAPHS = f"{DETAIL}/paragraphs"
GEOMETRY = f"{DETAIL}/geometry"
CHECK = f"{DETAIL}/check"

#: run_state 的 at/updated_at 是**本地墙钟无时区**值（run.py::_now()）。
_RUN_STATE_STAGES = {
    "parse": {
        "status": "ok",
        "ok": True,
        "at": "2026-09-17T18:00:15",
        "duration_s": 15.83,
        "artifacts": {
            "document_md": "agent/document.md",
            "anchors": "agent/anchors.json",
        },
    },
    "translate": {
        "status": "ok",
        "ok": True,
        "at": "2026-09-17T18:04:00",
        "duration_s": 225.0,
    },
    "apply": {
        "status": "ok",
        "ok": True,
        "at": "2026-09-17T18:04:30",
        "duration_s": 4.07,
    },
    "build": {
        "status": "ok",
        "ok": True,
        "at": "2026-09-17T18:05:00",
        "duration_s": 23.85,
        "artifacts": {
            "mono_pdf": "output/paper-final.no_watermark.zh.mono.pdf",
            "output_dir": "output",
        },
    },
    "check": {
        "status": "ok",
        "ok": True,
        "at": "2026-09-17T18:05:30",
        "duration_s": 2.5,
    },
    "review": {
        "status": "ok",
        "ok": True,
        "at": "2026-09-17T18:06:00",
        "duration_s": 20.0,
    },
    "report": {
        "status": "ok",
        "ok": True,
        "at": "2026-09-17T18:06:05",
        "duration_s": 0.0,
    },
}

_RUN_STATE = {
    "version": 1,
    "pdf": "paper-final.pdf",
    "config": {"from": "parse", "layout": "mineru", "dual": False},
    "stages": _RUN_STATE_STAGES,
    "quality": {
        "check": {
            "verdict": "needs_fix",
            "blockers": 1,
            "layout": "ok",
            "links": "ok",
            "reasons": ["排版 lint 缺陷 P0=0 P1=1"],
        },
        "reviewer": {"verdict": "pass", "findings": 0},
        "fix_rounds": {"retranslate": 0, "layout": 0},
    },
    "updated_at": "2026-09-17T18:06:05",
}

#: manifest 只归档了 parse/check 两段（replay 或中断的真实形态），其余走 run_state。
_MANIFEST = {
    "schema_version": 1,
    "run_id": RUN_ID,
    "created_at": "2026-09-17T10:00:00+00:00",
    "finished_at": "2026-09-17T10:06:05+00:00",
    "status": "finished",
    "input": {},
    "config": {"stages": ["parse", "check"], "options": {}},
    "stages": {
        "parse": {
            "events": 2,
            "status": "ok",
            "started_at": "2026-09-17T10:00:00.500+00:00",
            "finished_at": "2026-09-17T10:00:16.330+00:00",
        },
        "check": {
            "events": 2,
            "status": "ok",
            "started_at": "2026-09-17T10:05:28.000+00:00",
            "finished_at": "2026-09-17T10:05:30.500+00:00",
        },
    },
    "artifact_count": 0,
}

#: anchors 只有 2 行（且 page 是 0 基），几何有 3 段 → join 必须容忍数量不一致。
_ANCHORS = {
    "rows": [
        {
            "id": "P01-001",
            "page": 0,
            "layout_label": "title",
            "canonical": "Hello world.",
            "markdown": "Hello world.",
            "anchors": [],
        },
        {
            "id": "P01-002",
            "page": 0,
            "layout_label": "text",
            "canonical": "Second paragraph.",
            "markdown": "Second paragraph.",
            "anchors": [],
        },
    ],
    "skipped": [],
}

#: 只有 2 段有译文（P01-002 没译 → target 必须是 null）。
_TRANSLATED = [
    {"id": "P01-001", "target": "你好，世界。"},
    {"id": "P02-001", "target": "跨页段落。"},
]

_GEOMETRY = {
    "version": 1,
    "pages": 2,
    "page_info": [
        {"page": 1, "cropbox": [0.0, 0.0, 612.0, 792.0], "layout_regions": []},
        {"page": 2, "cropbox": [0.0, 0.0, 612.0, 792.0], "layout_regions": []},
    ],
    "paragraphs": [
        {
            "id": "P01-001",
            "page": 1,
            "layout_label": "title",
            "src_box": [66.585, 672.353, 544.64, 713.59],
            "layout_box": [66.585, 672.353, 544.64, 713.59],
            "rendered_box": [66.585, 696.375, 410.893, 713.59],
            "scale": 1.0,
            "font_scale": 1.0,
            "n_chars": 20,
            "n_lines": 1,
            "text": "你好，世界。",
        },
        {
            "id": "P01-002",
            "page": 1,
            "layout_label": "text",
            "src_box": [66.585, 640.0, 544.64, 660.0],
            "layout_box": [66.585, 640.0, 544.64, 660.0],
            "rendered_box": [66.585, 640.0, 500.0, 660.0],
            "scale": 0.9,
            "font_scale": 1.0,
            "n_chars": 30,
            "n_lines": 2,
            "text": "第二段没有被翻译。",
        },
        {
            "id": "P02-001",
            "page": 2,
            "layout_label": "text",
            "src_box": [66.585, 600.0, 544.64, 620.0],
            "layout_box": [66.585, 600.0, 544.64, 620.0],
            "rendered_box": [66.585, 600.0, 544.64, 620.0],
            "scale": 1.0,
            "font_scale": 1.0,
            "n_chars": 24,
            "n_lines": 1,
            "text": "跨页段落。",
        },
    ],
}

_LINT = {
    "findings": [
        {"code": "font_shrink", "sev": "P1", "id": "P01-002", "page": 1},
        {"code": "link_misaligned", "sev": "P2", "id": "P02-001", "page": 2},
        {"code": "font_shrink", "sev": "P2", "id": "P01-002", "page": 1},
    ],
    "counts": {"font_shrink": 2, "link_misaligned": 1},
    "metrics": {"paragraphs": 3, "pages": 2},
    "summary": {"total": 3, "by_severity": {"P1": 1, "P2": 2}, "blocking": 0},
}

_REVIEW_VERDICT = {
    "verdict": "pass",
    "blockers": [],
    "warnings": [{"code": "formula_splice", "sev": "P2", "id": "P02-001", "page": 2}],
    "metrics": {"rows": 2},
}

_LINK_AUDIT = {
    "source_pdf": "paper.pdf",
    "summary": {"verified": 3, "wrong_label": 0},
    "findings": [],
    "anchors_verified": True,
}

_PARSE_SNAPSHOT = {
    "version": 1,
    "entities": [
        {
            "id": "P01-001",
            "kind": "paragraph",
            "label": "title",
            "page": 1,
            "box": {"x0": 66.585, "y0": 78.41, "x1": 544.64, "y1": 119.647},
            "attrs": {"unicode": "Hello world."},
        },
        {
            "id": "P01-002",
            "kind": "paragraph",
            "label": "text",
            "page": 1,
            "box": {"x0": 66.585, "y0": 132.0, "x1": 544.64, "y1": 152.0},
            "attrs": {"unicode": "Second paragraph."},
        },
        {
            "id": "P02-001",
            "kind": "paragraph",
            "label": "text",
            "page": 2,
            "box": {"x0": 66.585, "y0": 172.0, "x1": 544.64, "y1": 192.0},
            "attrs": {"unicode": "Third paragraph."},
        },
    ],
    "relations": [
        {"from_id": "P01-001", "to_id": "L01-001", "kind": "in_layout"},
        {"from_id": "P02-001", "to_id": "L02-001", "kind": "in_layout"},
    ],
}


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_jsonl(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )


def _make_workdir(base: Path, did: str) -> Path:
    """造一个完整 workdir（agent 产物 + 一个 debug run）。"""
    workdir = base / did
    agent = workdir / "agent"
    agent.mkdir(parents=True)
    _write_json(agent / "run_state.json", _RUN_STATE)
    _write_json(agent / "anchors.json", _ANCHORS)
    _write_jsonl(agent / "translated.jsonl", _TRANSLATED)
    _write_json(agent / "layout_geometry.json", _GEOMETRY)
    _write_json(agent / "layout_lint.json", _LINT)
    _write_json(agent / "review_verdict.json", _REVIEW_VERDICT)
    _write_json(agent / "link_audit.json", _LINK_AUDIT)
    (workdir / "output").mkdir()
    (workdir / "output" / "paper-final.no_watermark.zh.mono.pdf").write_bytes(
        b"%PDF-1.4"
    )
    run_dir = workdir / "debug" / "runs" / RUN_ID
    _write_json(run_dir / "manifest.json", _MANIFEST)
    _write_json(run_dir / "snapshots" / "parse" / "paragraphs.json", _PARSE_SNAPSHOT)
    return workdir


@pytest.fixture
def root(tmp_path: Path) -> Path:
    """一个完整文档 + 一个只有目录名的空文档。"""
    base = tmp_path / "root"
    base.mkdir()
    _make_workdir(base, DID)
    (base / BARE).mkdir()
    return base


@pytest.fixture
def workdir(root: Path) -> Path:
    return root / DID


@pytest.fixture
def client(root: Path):
    with TestClient(create_app(DocumentStore.for_root(root))) as test_client:
        yield test_client


def _paragraphs_by_id(body: list[dict]) -> dict[str, dict]:
    return {item["id"]: item for item in body}


# --------------------------------------------------------------------------- #
# 列表
# --------------------------------------------------------------------------- #
def test_documents_list_shape(client):
    """列表：did 顺序稳定，字段来自真实产物，时间是 UTC（毫秒 + Z）。"""
    response = client.get(DOCUMENTS)
    assert response.status_code == 200
    body = response.json()
    assert [item["did"] for item in body] == [BARE, DID]
    item, bare = body[1], body[0]

    assert set(item) == {
        "did",
        "title",
        "pages",
        "paragraph_count",
        "translated_count",
        "stage_summary",
        "updated_at",
    }
    assert item["title"] == "paper-final"  # run_state.pdf 的文件名（去扩展名）
    assert item["pages"] == 2
    assert item["paragraph_count"] == 3
    assert item["translated_count"] == 2
    assert item["stage_summary"] == dict.fromkeys(STAGES, "ok")
    # manifest 的 finished_at（UTC）优先于 run_state.updated_at（本地墙钟）
    assert item["updated_at"] == "2026-09-17T10:06:05.000Z"
    assert bare["updated_at"] is None


def test_documents_list_reports_missing_products_as_null(client):
    """没有任何产物：计数是 null（不是 0），阶段是 not_run（不谎报 ok）。"""
    body = client.get(DOCUMENTS).json()
    bare = body[0]
    assert bare["did"] == BARE
    assert bare["title"] is None
    assert bare["pages"] is None
    assert bare["paragraph_count"] is None
    assert bare["translated_count"] is None
    assert bare["stage_summary"] == dict.fromkeys(STAGES, "not_run")


def test_documents_list_stage_names_match_run_stages():
    """阶段词表与 ``babeldoc_tools.run::STAGES`` 一致（不重复拼写阶段名）。"""
    from babeldoc_tools.run import STAGES as RUN_STAGES

    assert STAGES == RUN_STAGES


# --------------------------------------------------------------------------- #
# 详情
# --------------------------------------------------------------------------- #
def test_document_detail_separates_quality_and_compile(client):
    """质量与编译分开：compile 在 W02 固定 none/0（没有真实编译产物就不造假）。"""
    response = client.get(DETAIL)
    assert response.status_code == 200
    body = response.json()

    assert body["compile"] == {
        "status": "none",
        "revision": 0,
        "stale": False,
        "artifact": None,
    }
    quality = body["quality"]
    # check 的门禁结论来自 run_state（合并了 lint/link），不是 review_verdict 的 pass
    assert quality["check"]["verdict"] == "needs_fix"
    assert quality["check"]["blockers"] == []
    assert quality["check"]["warnings"] == _REVIEW_VERDICT["warnings"]
    assert quality["check"]["reasons"] == ["排版 lint 缺陷 P0=0 P1=1"]
    assert quality["check"]["at"].endswith("Z")
    assert quality["check"]["at"] != "2026-09-17T18:05:30"  # 本地墙钟已归一成 UTC
    assert quality["reviewer"] == {
        "status": "pass",
        "fix_rounds": {"retranslate": 0, "layout": 0},
        "at": quality["reviewer"]["at"],
    }
    assert quality["reviewer"]["at"].endswith("Z")
    # 门禁 needs_fix → pipeline 不算成功（编译成功也不得置 true）
    assert quality["pipeline_ok"] is False


def test_document_detail_pdf_config_and_availability(client):
    body = client.get(DETAIL).json()
    assert body["pdf"]["source"] == "paper-final.pdf"
    assert body["pdf"]["outputs"] == [
        {
            "path": "output/paper-final.no_watermark.zh.mono.pdf",
            "exists": True,
            "bytes": 8,
        }
    ]
    assert body["config"]["layout"] == "mineru"
    assert body["available"] == {
        "run_state": True,
        "anchors": True,
        "translated": True,
        "geometry": True,
        "parse_snapshot": True,
        "review_verdict": True,
        "layout_lint": True,
        "link_audit": True,
    }


def test_document_detail_without_products_does_not_fail(client):
    """空 workdir：200 + 全 null/false（缺产物不报错，也不假报 green）。"""
    response = client.get(f"{DOCUMENTS}/{BARE}")
    assert response.status_code == 200
    body = response.json()
    assert body["pdf"] == {"source": None, "outputs": []}
    assert body["config"] is None
    assert body["quality"]["check"]["verdict"] == "not_available"
    assert body["quality"]["reviewer"]["status"] == "not_run"
    assert body["quality"]["pipeline_ok"] is False
    assert body["compile"]["status"] == "none"
    assert not any(body["available"].values())


def test_document_detail_tolerates_corrupt_product(client, workdir):
    """单个产物 JSON 损坏：不 500，该字段降级 + available 标 false。"""
    (workdir / "agent" / "review_verdict.json").write_text(
        "{not json", encoding="utf-8"
    )
    (workdir / "agent" / "layout_geometry.json").write_text("", encoding="utf-8")

    response = client.get(DETAIL)
    assert response.status_code == 200
    body = response.json()
    assert body["available"]["review_verdict"] is False
    assert body["available"]["geometry"] is False
    # 产物坏了 → 计数退回快照（3 段 / 2 页），不是 null 也不是编造值
    assert body["pages"] == 2
    assert body["paragraph_count"] == 3
    assert body["quality"]["check"]["verdict"] == "needs_fix"  # run_state 仍可用


# --------------------------------------------------------------------------- #
# 阶段状态
# --------------------------------------------------------------------------- #
def test_stage_state_prefers_manifest_timing(client):
    """manifest 有该段 → 用 recorder 的真实起止时间；没有 → 回退 run_state。"""
    response = client.get(STAGE_STATE)
    assert response.status_code == 200
    body = response.json()
    assert body["did"] == DID
    assert body["run_id"] == RUN_ID
    stages = {item["stage"]: item for item in body["stages"]}
    assert list(stages) == list(STAGES)

    parse = stages["parse"]
    assert parse["timing_source"] == "manifest"
    assert parse["started_at"] == "2026-09-17T10:00:00.500Z"
    assert parse["finished_at"] == "2026-09-17T10:00:16.330Z"
    assert parse["status"] == "ok"
    assert parse["ok"] is True
    # 实测耗时仍来自 run_state（manifest 只记起止时间）
    assert parse["duration_s"] == 15.83

    review = stages["review"]
    assert review["timing_source"] == "run_state"
    assert review["finished_at"].endswith("Z")
    started = datetime.datetime.fromisoformat(
        review["started_at"].replace("Z", "+00:00")
    )
    finished = datetime.datetime.fromisoformat(
        review["finished_at"].replace("Z", "+00:00")
    )
    assert (finished - started).total_seconds() == 20.0  # at - duration_s 反推起点


def test_stage_state_without_manifest_or_run_state(client):
    """没有 run 归档也没有 run_state：7 段仍要出现，全部 not_run / 无时间。"""
    body = client.get(f"{DOCUMENTS}/{BARE}/stage-state").json()
    assert body["run_id"] is None
    assert [item["stage"] for item in body["stages"]] == list(STAGES)
    for item in body["stages"]:
        assert item["status"] == "not_run"
        assert item["ok"] is None
        assert item["started_at"] is None
        assert item["finished_at"] is None
        assert item["timing_source"] is None


def test_stage_state_falls_back_when_manifest_missing(client, workdir):
    """没有 manifest：全部回退 run_state（timing_source=run_state）。"""
    (workdir / "debug" / "runs" / RUN_ID / "manifest.json").unlink()
    body = client.get(STAGE_STATE).json()
    assert body["run_id"] is None
    for item in body["stages"]:
        assert item["timing_source"] == "run_state"
        assert item["finished_at"].endswith("Z")
        assert item["started_at"] is not None


# --------------------------------------------------------------------------- #
# 段落 join
# --------------------------------------------------------------------------- #
def test_paragraphs_join_tolerates_count_mismatch(client):
    """几何 3 段 / anchors 2 行 / 译文 2 行：id 取并集，缺侧字段 null。"""
    response = client.get(PARAGRAPHS)
    assert response.status_code == 200
    items = response.json()
    assert [item["id"] for item in items] == ["P01-001", "P01-002", "P02-001"]
    by_id = _paragraphs_by_id(items)

    first = by_id["P01-001"]
    assert first["page"] == 1  # 归一到 1 基（anchors 里是 0 基）
    assert first["layout_label"] == "title"
    assert first["source"] == "Hello world."  # anchors.canonical
    assert first["target"] == "你好，世界。"
    assert first["geometry"]["src_box"] == [66.585, 672.353, 544.64, 713.59]
    assert first["layout_status"] == "ok"

    second = by_id["P01-002"]
    assert second["target"] is None  # 没有译文 → null（不是报错、也不是空串）
    assert second["source"] == "Second paragraph."
    assert second["layout_status"] == "P1"  # 最重缺陷级别

    third = by_id["P02-001"]
    assert third["page"] == 2
    assert third["source"] == "Third paragraph."  # anchors 缺 → 快照 attrs.unicode
    assert third["target"] == "跨页段落。"
    assert third["layout_status"] == "P2"


def test_paragraphs_page_filter(client):
    body = client.get(PARAGRAPHS, params={"page": 2}).json()
    assert [item["id"] for item in body] == ["P02-001"]
    assert client.get(PARAGRAPHS, params={"page": 99}).json() == []
    assert client.get(PARAGRAPHS, params={"page": 0}).status_code == 422


def test_paragraphs_without_translated_jsonl_keeps_rows(client, workdir):
    """缺 translated.jsonl：target 全为 null，端点不报错。"""
    (workdir / "agent" / "translated.jsonl").unlink()
    response = client.get(PARAGRAPHS)
    assert response.status_code == 200
    items = response.json()
    assert [item["id"] for item in items] == ["P01-001", "P01-002", "P02-001"]
    assert {item["target"] for item in items} == {None}


def test_paragraphs_tolerates_corrupt_geometry(client, workdir):
    """layout_geometry.json 损坏：仍按别侧产物给出行（geometry 为 null）。"""
    (workdir / "agent" / "layout_geometry.json").write_text("{{{", encoding="utf-8")
    response = client.get(PARAGRAPHS)
    assert response.status_code == 200
    items = response.json()
    assert [item["id"] for item in items] == ["P01-001", "P01-002", "P02-001"]
    assert {item["geometry"] for item in items} == {None}


def test_paragraphs_tolerates_corrupt_translated_line(client, workdir):
    """translated.jsonl 单行损坏：跳过坏行，其余行照常用。"""
    (workdir / "agent" / "translated.jsonl").write_text(
        '{"id": "P01-001", "target": "你好，世界。"}\n{broken\n', encoding="utf-8"
    )
    response = client.get(PARAGRAPHS)
    assert response.status_code == 200
    by_id = _paragraphs_by_id(response.json())
    assert by_id["P01-001"]["target"] == "你好，世界。"
    assert by_id["P02-001"]["target"] is None


def test_paragraphs_without_any_product_is_404(client):
    """四份段落产物都没有：404 ``paragraphs_unavailable``（不拿空数组假成功）。"""
    response = client.get(f"{DOCUMENTS}/{BARE}/paragraphs")
    assert response.status_code == 404
    error = response.json()["error"]
    assert error["code"] == "paragraphs_unavailable"
    assert BARE in error["message"]


# --------------------------------------------------------------------------- #
# 几何
# --------------------------------------------------------------------------- #
def test_geometry_parse_marks_coord_system(client):
    response = client.get(GEOMETRY, params={"kind": "parse"})
    assert response.status_code == 200
    body = response.json()
    assert body["kind"] == "parse"
    assert body["coord_system"] == "pdf_topleft"
    assert body["run_id"] == RUN_ID
    assert body["page"] is None
    assert [entity["id"] for entity in body["entities"]] == [
        "P01-001",
        "P01-002",
        "P02-001",
    ]
    assert body["entities"][0]["box"] == {
        "x0": 66.585,
        "y0": 78.41,
        "x1": 544.64,
        "y1": 119.647,
    }
    assert len(body["relations"]) == 2


def test_geometry_parse_page_filter(client):
    body = client.get(GEOMETRY, params={"kind": "parse", "page": 2}).json()
    assert body["page"] == 2
    assert [entity["id"] for entity in body["entities"]] == ["P02-001"]
    # 关系只留两端在筛后实体里的
    assert [rel["to_id"] for rel in body["relations"]] == ["L02-001"]


def test_geometry_layout_marks_coord_system_and_page_info(client):
    body = client.get(GEOMETRY, params={"kind": "layout"}).json()
    assert body["kind"] == "layout"
    assert body["coord_system"] == "pdf_native"  # 原生 y 向上，不转换
    assert body["pages"] == 2
    assert [row["id"] for row in body["paragraphs"]] == [
        "P01-001",
        "P01-002",
        "P02-001",
    ]
    assert body["paragraphs"][0]["src_box"] == [66.585, 672.353, 544.64, 713.59]
    assert [row["page"] for row in body["page_info"]] == [1, 2]
    assert body["page_info"][0]["cropbox"] == [0.0, 0.0, 612.0, 792.0]


def test_geometry_layout_page_filter(client):
    body = client.get(GEOMETRY, params={"kind": "layout", "page": 2}).json()
    assert [row["id"] for row in body["paragraphs"]] == ["P02-001"]
    assert [row["page"] for row in body["page_info"]] == [2]


def test_geometry_parse_without_snapshot_is_404_snapshot_unavailable(client, workdir):
    (
        workdir / "debug" / "runs" / RUN_ID / "snapshots" / "parse" / "paragraphs.json"
    ).unlink()
    response = client.get(GEOMETRY, params={"kind": "parse"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "snapshot_unavailable"


def test_geometry_parse_uses_newest_run_that_has_the_snapshot(client, workdir):
    """最新 run 还没写快照时不谎报缺快照：用次新的真实数据并报它的 run_id。"""
    newer = workdir / "debug" / "runs" / "20260917T110000Z-0000b2"
    _write_json(
        newer / "manifest.json",
        {
            "run_id": newer.name,
            "created_at": "2026-09-17T11:00:00+00:00",
            "status": "running",
            "stages": {},
        },
    )
    body = client.get(GEOMETRY, params={"kind": "parse"}).json()
    assert body["run_id"] == RUN_ID  # 数据实际来源的那个 run
    assert len(body["entities"]) == 3
    # 阶段状态仍用最新那个 run 的 manifest（它才是当前运行）
    assert client.get(STAGE_STATE).json()["run_id"] == newer.name


def test_geometry_layout_without_product_is_404(client, workdir):
    (workdir / "agent" / "layout_geometry.json").unlink()
    response = client.get(GEOMETRY, params={"kind": "layout"})
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "geometry_unavailable"


def test_geometry_requires_kind(client):
    response = client.get(GEOMETRY)
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


# --------------------------------------------------------------------------- #
# 检查
# --------------------------------------------------------------------------- #
def test_check_passes_through_three_sources(client):
    response = client.get(CHECK)
    assert response.status_code == 200
    body = response.json()
    assert body["did"] == DID
    assert body["review_verdict"] == _REVIEW_VERDICT
    assert body["layout_lint"] == _LINT
    assert body["link_audit"] == _LINK_AUDIT
    assert body["available"] == {"review": True, "lint": True, "link": True}


def test_check_reports_missing_source(client, workdir):
    (workdir / "agent" / "link_audit.json").unlink()
    body = client.get(CHECK).json()
    assert body["link_audit"] is None
    assert body["available"] == {"review": True, "lint": True, "link": False}


# --------------------------------------------------------------------------- #
# 错误与只读边界
# --------------------------------------------------------------------------- #
def test_unknown_document_uses_error_envelope(client):
    response = client.get(f"{DOCUMENTS}/nope")
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "document_not_found"


def test_invalid_document_id_uses_error_envelope(client):
    response = client.get(f"{DOCUMENTS}/.hidden")
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_document_id"


def test_error_codes_are_stable_across_endpoints(client):
    """带 did 的端点遇到未知文档都是同一套错误信封（前端只写一套解析）。"""
    for path in (
        f"{DOCUMENTS}/nope",
        f"{DOCUMENTS}/nope/stage-state",
        f"{DOCUMENTS}/nope/paragraphs",
        f"{DOCUMENTS}/nope/geometry",
        f"{DOCUMENTS}/nope/check",
    ):
        response = client.get(
            path, params={"kind": "layout"} if "geometry" in path else None
        )
        assert response.status_code == 404, path
        assert response.json()["error"]["code"] == "document_not_found", path


def test_openapi_lists_document_subresources_as_get_only(client):
    """文档的只读子资源仍然只有 GET（``/documents`` 本身 W08 多了上传的 POST）。"""
    schema = client.get("/openapi.json").json()
    for path in (
        f"{DOCUMENTS}/{{did}}",
        f"{DOCUMENTS}/{{did}}/stage-state",
        f"{DOCUMENTS}/{{did}}/paragraphs",
        f"{DOCUMENTS}/{{did}}/geometry",
        f"{DOCUMENTS}/{{did}}/check",
    ):
        assert set(schema["paths"][path]) == {"get"}, path
    # ``POST /documents`` 是 W08 的上传：写端点白名单（helper 是唯一来源）放行它
    assert set(schema["paths"][DOCUMENTS]) == {"get", "post"}
    assert_no_unexpected_write_routes(schema)


def test_workdir_mode_only_serves_that_document(root: Path):
    with TestClient(create_app(DocumentStore.for_workdir(root / DID))) as test_client:
        assert [item["did"] for item in test_client.get(DOCUMENTS).json()] == [DID]
        assert test_client.get(f"{DOCUMENTS}/{BARE}").status_code == 404
