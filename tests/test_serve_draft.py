"""``bdt serve`` 草稿读写：revision 单调、乐观并发、字段校验（``api.md`` §3.3，W09）。

这一层是纯服务端逻辑，用真路由 + 真 DraftStore（不 mock），只给最小 workdir：
草稿文件落在 ``<workdir>/.bdt-serve/draft.json``，与 doc 的产物区（``agent/``）分开。
编译侧的行为（防抖、隔离副本、发布）在 ``tests/test_serve_compile.py``。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

pytest.importorskip("fastapi", reason="serve 需要 web extra: fastapi")

from babeldoc_tools.common import ToolError  # noqa: E402
from babeldoc_tools.serve.app import create_app  # noqa: E402
from babeldoc_tools.serve.compile import DEBOUNCE_SECONDS  # noqa: E402
from babeldoc_tools.serve.draft import DRAFT_FILE  # noqa: E402
from babeldoc_tools.serve.draft import DraftStore  # noqa: E402
from babeldoc_tools.serve.draft import read_draft  # noqa: E402
from babeldoc_tools.serve.draft import validate_changes  # noqa: E402
from babeldoc_tools.serve.schemas import API_PREFIX as API  # noqa: E402
from babeldoc_tools.serve.store import DocumentStore  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

#: 段落 id（形状同 anchors.json / layout_overrides 的 ``_ID_RE``）。
PID = "P05-002"

DRAFT_URL = f"{API}/documents/alpha/draft"


def _make_workdir(root: Path, did: str) -> Path:
    """最小 workdir：``agent/translated.md`` + ``anchors.json``（编译侧要用）。"""
    agent = root / did / "agent"
    agent.mkdir(parents=True)
    (agent / "translated.md").write_text(
        "<!--MD_HEADER-->\n\n<!-- id=P05-002 label=text -->\n原译文\n",
        encoding="utf-8",
    )
    (agent / "anchors.json").write_text(
        json.dumps({"rows": [{"id": PID, "layout_label": "text"}]}),
        encoding="utf-8",
    )
    (agent / "run_state.json").write_text("{}\n", encoding="utf-8")
    return root / did


@pytest.fixture
def root(tmp_path: Path) -> Path:
    base = tmp_path / "root"
    _make_workdir(base, "alpha")
    _make_workdir(base, "beta")
    return base


@pytest.fixture
def client(root: Path):
    # 防抖窗口拉长：草稿用例只验"写了盘、revision 对了"，不让编译在测试里跑起来。
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr("babeldoc_tools.serve.compile.DEBOUNCE_SECONDS", 60.0)
        with TestClient(create_app(DocumentStore.for_root(root))) as test_client:
            yield test_client


def get_draft(client, did: str = "alpha") -> dict:
    response = client.get(f"{API}/documents/{did}/draft")
    assert response.status_code == 200, response.text
    return response.json()


def patch_draft(client, body: dict, did: str = "alpha"):
    return client.patch(f"{API}/documents/{did}/draft", json=body)


# --------------------------------------------------------------------------- #
# 冷启动与最小写入
# --------------------------------------------------------------------------- #
def test_default_debounce_is_1_5s():
    """EXECUTION.md 纠偏 7 的固定窗口：1.5s（改这里必须同时改文档）。"""
    assert DEBOUNCE_SECONDS == 1.5


def test_get_empty_draft_is_revision_zero_not_404(client):
    body = get_draft(client)
    assert body == {"revision": 0, "updated_at": None, "paragraphs": {}}


def test_get_does_not_create_draft_file(client, root):
    """GET 是只读的：冷启动空草稿不落盘（只有写才建文件）。"""
    assert get_draft(client)["revision"] == 0
    assert not (root / "alpha" / ".bdt-serve" / DRAFT_FILE).exists()


def test_patch_from_zero_bumps_to_one(client, root):
    response = patch_draft(
        client,
        {
            "base_revision": 0,
            "paragraphs": {PID: {"target": "改后的译文"}},
        },
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["revision"] == 1
    assert body["paragraphs"][PID]["target"] == "改后的译文"
    assert body["paragraphs"][PID]["layout"] is None
    assert body["updated_at"] and body["paragraphs"][PID]["updated_at"]

    on_disk = json.loads((root / "alpha" / ".bdt-serve" / DRAFT_FILE).read_text())
    assert on_disk["revision"] == 1
    assert get_draft(client)["revision"] == 1


def test_patch_revision_is_monotonic_and_layout_is_recorded(client):
    for index in range(1, 4):
        body = patch_draft(
            client,
            {
                "base_revision": index - 1,
                "paragraphs": {PID: {"target": f"第{index}版", "layout": {"scale_cap": 0.9}}},
            },
        ).json()
        assert body["revision"] == index
    assert body["paragraphs"][PID]["layout"] == {"scale_cap": 0.9}


def test_patch_base_revision_mismatch_is_409_with_current_revision(client):
    patch_draft(client, {"base_revision": 0, "paragraphs": {PID: {"target": "A"}}})
    response = patch_draft(
        client, {"base_revision": 0, "paragraphs": {PID: {"target": "B"}}}
    )
    assert response.status_code == 409
    error = response.json()["error"]
    assert error["code"] == "revision_conflict"
    assert error["detail"]["current_revision"] == 1
    assert error["detail"]["base_revision"] == 0
    # 冲突不改盘：还是第一次写的那一版
    assert get_draft(client)["paragraphs"][PID]["target"] == "A"


def test_field_and_paragraph_null_delete(client):
    patch_draft(
        client,
        {
            "base_revision": 0,
            "paragraphs": {
                PID: {"target": "A", "layout": {"font_scale": 1.1}},
                "P06-001": {"target": "B"},
            },
        },
    )
    # 字段级 null：只删 layout
    body = patch_draft(
        client, {"base_revision": 1, "paragraphs": {PID: {"layout": None}}}
    ).json()
    assert body["paragraphs"][PID]["target"] == "A"
    assert body["paragraphs"][PID]["layout"] is None
    # 整段 null：删掉该段
    body = patch_draft(client, {"base_revision": 2, "paragraphs": {"P06-001": None}}).json()
    assert "P06-001" not in body["paragraphs"]


def test_two_null_fields_drop_the_paragraph(client):
    patch_draft(client, {"base_revision": 0, "paragraphs": {PID: {"target": "A"}}})
    body = patch_draft(
        client, {"base_revision": 1, "paragraphs": {PID: {"target": None}}}
    ).json()
    assert body["paragraphs"] == {}  # 两个字段都没了 = 该段没有覆盖
    assert body["revision"] == 2  # 但 revision 照样 +1


def test_delete_clears_but_revision_never_goes_back(client):
    patch_draft(client, {"base_revision": 0, "paragraphs": {PID: {"target": "A"}}})
    response = client.delete(DRAFT_URL)
    assert response.status_code == 200, response.text
    assert response.json()["revision"] == 2
    assert response.json()["paragraphs"] == {}
    again = client.delete(DRAFT_URL).json()
    assert again["revision"] == 3  # 清空也 +1，不回退
    assert again["paragraphs"] == {}


def test_delete_on_cold_start_bumps_from_zero(client):
    response = client.delete(DRAFT_URL)
    assert response.status_code == 200
    assert response.json()["revision"] == 1


# --------------------------------------------------------------------------- #
# 校验（422 draft_invalid，逐字段路径）
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "layout",
    [
        {"scale_cap": 0.05},  # < 0.1
        {"scale_cap": 5.5},  # > 5.0
        {"font_scale": 0.1},  # < 0.2
        {"font_scale": 5.1},  # > 5.0
        {"line_skip": 0.7},  # < 0.8
        {"line_skip": 3.1},  # > 3.0
        {"box_scale": 0.2},  # < 0.3
        {"box_scale": 5.1},  # > 5.0
        {"scale_cap": "0.9"},  # 类型
        {"box": [10.0, 20.0, 5.0, 40.0]},  # x2 <= x
        {"box": [10.0, 20.0, 30.0, 15.0]},  # y2 <= y
        {"box": [10.0, 20.0, 30.0]},  # 不是四元
        {"box": ["a", 1, 2, 3]},  # 非数字
        {"nope": 1},  # 未知键
    ],
)
def test_invalid_layout_values_are_422_draft_invalid(client, layout):
    response = patch_draft(
        client, {"base_revision": 0, "paragraphs": {PID: {"layout": layout}}}
    )
    assert response.status_code == 422, response.text
    error = response.json()["error"]
    assert error["code"] == "draft_invalid"
    assert error["detail"]["errors"]
    assert get_draft(client)["revision"] == 0  # 校验失败不改盘


def test_valid_layout_boundaries_are_accepted(client):
    layout = {
        "scale_cap": 0.1,
        "font_scale": 0.2,
        "line_skip": 0.8,
        "box_scale": 0.3,
        "box": [10.0, 20.0, 30.0, 40.0],
        "force_break_after_text": ["术语"],
        "force_break_after_offset": [3],
    }
    response = patch_draft(
        client, {"base_revision": 0, "paragraphs": {PID: {"layout": layout}}}
    )
    assert response.status_code == 200, response.text
    assert response.json()["paragraphs"][PID]["layout"] == layout


@pytest.mark.parametrize(
    "entry",
    [
        {"target": 5},  # 不是字符串
        {"target": ["a"]},
        {"unknown": 1},  # 未知字段
        "不是对象",
    ],
)
def test_invalid_target_and_entry_shapes_are_draft_invalid(client, entry):
    response = patch_draft(client, {"base_revision": 0, "paragraphs": {PID: entry}})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "draft_invalid"


def test_invalid_paragraph_id_is_draft_invalid(client):
    response = patch_draft(
        client, {"base_revision": 0, "paragraphs": {"../etc/passwd": {"target": "x"}}}
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "draft_invalid"


def test_missing_base_revision_is_validation_error(client):
    response = patch_draft(client, {"paragraphs": {PID: {"target": "A"}}})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "validation_error"


def test_unknown_document_is_404(client):
    assert client.get(f"{API}/documents/nope/draft").status_code == 404
    assert (
        patch_draft(client, {"base_revision": 0, "paragraphs": {}}, did="nope").status_code
        == 404
    )
    assert client.delete(f"{API}/documents/nope/draft").status_code == 404


def test_escape_attempt_is_rejected(client):
    response = client.get(f"{API}/documents/..%2Fetc/draft")
    assert response.status_code in (400, 404)


# --------------------------------------------------------------------------- #
# 并发与容错
# --------------------------------------------------------------------------- #
def test_concurrent_patches_serialise_on_one_lock(tmp_path):
    """同一个 did 的并发 PATCH：只有拿到同一个 base_revision 的那个成功。"""
    workdir = _make_workdir(tmp_path / "root", "alpha")

    async def race():
        store = DraftStore(workdir)
        return await asyncio.gather(
            store.patch(base_revision=0, paragraphs={PID: {"target": "A"}}),
            store.patch(base_revision=0, paragraphs={PID: {"target": "B"}}),
            return_exceptions=True,
        )

    results = asyncio.run(race())
    ok = [item for item in results if not isinstance(item, BaseException)]
    conflicts = [item for item in results if isinstance(item, ToolError)]
    assert len(ok) == 1
    assert len(conflicts) == 1 and conflicts[0].code == "revision_conflict"
    assert read_draft(workdir).revision == 1


def test_two_documents_do_not_block_each_other(client):
    first = patch_draft(client, {"base_revision": 0, "paragraphs": {PID: {"target": "A"}}})
    second = patch_draft(
        client, {"base_revision": 0, "paragraphs": {PID: {"target": "B"}}}, did="beta"
    )
    assert first.status_code == 200 and second.status_code == 200
    assert get_draft(client, "beta")["paragraphs"][PID]["target"] == "B"


def test_broken_draft_file_falls_back_to_empty_and_is_rewritten(client, root):
    path = root / "alpha" / ".bdt-serve" / DRAFT_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{ 这不是 JSON", encoding="utf-8")
    assert get_draft(client)["revision"] == 0
    body = patch_draft(client, {"base_revision": 0, "paragraphs": {PID: {"target": "A"}}}).json()
    assert body["revision"] == 1
    assert json.loads(path.read_text())["revision"] == 1


def test_validate_changes_is_pure_and_normalises():
    assert validate_changes({PID: None}) == {PID: None}
    with pytest.raises(ToolError) as excinfo:
        validate_changes({"P05-002": {"layout": {"line_skip": 9}}})
    assert excinfo.value.code == "draft_invalid"
