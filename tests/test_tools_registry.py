"""工具包契约：注册表 / schema / dispatch 错误处理 / CLI / 真实工作目录冒烟。"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

from babeldoc_tools import registry  # noqa: E402

registry.load_builtin_tools()

EXPECTED_TOOLS = {
    "parse_document",
    "translate_document",
    "retranslate_ids",
    "apply_translation",
    "review_document",
    "backtranslate_check",
    "reconstruct_pdf",
    "render_pages",
    "dump_text_layer",
    "layout_set",
    "layout_lint",
    "layout_locate",
    "snapshot",
    "restore",
    "list_snapshots",
    "report",
}


def test_all_expected_tools_registered():
    assert EXPECTED_TOOLS == set(registry.tool_names())


def test_list_and_schema_contract():
    tools = registry.list_tools()
    assert len(tools) == len(registry.tool_names())
    for tool in tools:
        assert {"name", "group", "description", "input_schema"} <= set(tool)
        assert tool["input_schema"].get("type") == "object"
        assert "workdir" in tool["input_schema"]["properties"] or tool["name"] in (
            "render_pages",
    "dump_text_layer",
        )
    schema = registry.get_schema("layout_set")
    assert schemas_required(schema) >= {"workdir"}
    with pytest.raises(KeyError):
        registry.get_schema("nope")


def schemas_required(schema: dict) -> set:
    return set(schema.get("required") or [])


def test_dispatch_unknown_tool():
    result = registry.dispatch("nope", {})
    assert result["ok"] is False
    assert result["error"]["code"] == "unknown_tool"
    assert "parse_document" in result["error"]["available"]


def test_dispatch_invalid_args():
    result = registry.dispatch("layout_set", {"patch": {}})
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_args"
    assert "workdir" in result["error"]["message"]

    result = registry.dispatch("layout_lint", {"workdir": "x", "nope": 1})
    assert result["ok"] is False
    assert "未知参数" in result["error"]["message"]


def test_dispatch_wraps_tool_exception(tmp_path):
    result = registry.dispatch("layout_lint", {"workdir": str(tmp_path)})
    assert result["ok"] is False
    assert result["error"]["code"] == "workdir_missing"
    assert "parse_document" in result["error"]["message"]


def test_cli_list_and_schema():
    out = subprocess.run(
        [sys.executable, "-m", "babeldoc_tools", "list", "--compact"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert out.returncode == 0, out.stderr
    payload = json.loads(out.stdout)
    assert payload["ok"] is True
    names = {tool["name"] for tool in payload["data"]["tools"]}
    assert names == EXPECTED_TOOLS

    out = subprocess.run(
        [sys.executable, "-m", "babeldoc_tools", "schema", "layout_set"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["data"]["type"] == "object"


def test_cli_call_error_exit_code():
    out = subprocess.run(
        [sys.executable, "-m", "babeldoc_tools", "call", "nope", "--compact"],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    assert out.returncode == 1
    assert json.loads(out.stdout)["error"]["code"] == "unknown_tool"


# --------------------------------------------------------------------------- #
# 真实工作目录冒烟：快照 / 覆盖 / locate / lint（不触发重建）
# --------------------------------------------------------------------------- #
@pytest.fixture()
def fake_workdir(tmp_path):
    agent = tmp_path / "agent"
    agent.mkdir()
    (agent / "sheet.jsonl").write_text(
        json.dumps(
            {
                "id": "P01-001",
                "page": 1,
                "layout_label": "text",
                "source": "Hello world, this is a test document body.",
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )
    (agent / "anchors.json").write_text(
        json.dumps({"rows": [{"id": "P01-001", "layout_label": "text"}]}),
        encoding="utf-8",
    )
    (agent / "translated.jsonl").write_text(
        json.dumps({"id": "P01-001", "target": "你好，世界，这是一段测试文档正文。"}, ensure_ascii=False)
        + "\n",
        encoding="utf-8",
    )
    (agent / "layout_geometry.json").write_text(
        json.dumps(
            {
                "version": 1,
                "pages": 1,
                "overrides": {"paragraphs": {}, "pages": {}},
                "page_info": [
                    {"page": 1, "cropbox": [0, 0, 612, 792], "layout_regions": []}
                ],
                "paragraphs": [
                    {
                        "id": "P01-001",
                        "overridable": True,
                        "page": 1,
                        "layout_label": "text",
                        "src_box": [50, 600, 300, 700],
                        "layout_box": [50, 600, 300, 700],
                        "rendered_box": [50, 600, 300, 700],
                        "scale": 1.0,
                        "optimal_scale": 1.0,
                        "font_scale": 1.0,
                        "src_font_size": 10.0,
                        "mode_font_size": 10.0,
                        "min_font_size": 10.0,
                        "max_font_size": 10.0,
                        "n_chars": 16,
                        "n_lines": 2,
                        "text": "你好，世界，这是一段测试文档正文。",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    return tmp_path


def test_layout_set_lint_locate_snapshot_roundtrip(fake_workdir):
    workdir = str(fake_workdir)
    result = registry.dispatch(
        "layout_set",
        {
            "workdir": workdir,
            "patch": {"paragraphs": {"P01-001": {"scale_cap": 0.9}}},
            "reason": "测试",
        },
    )
    assert result["ok"] is True
    assert result["data"]["diff"]["added"] == ["paragraphs.P01-001"]

    lint = registry.dispatch("layout_lint", {"workdir": workdir})
    assert lint["ok"] is True
    assert lint["data"]["summary"]["total"] == 0

    locate = registry.dispatch(
        "layout_locate", {"workdir": workdir, "page": 1, "box": [60, 610, 290, 690]}
    )
    assert locate["ok"] is True
    assert locate["data"]["candidates"][0]["id"] == "P01-001"

    snap = registry.dispatch("snapshot", {"workdir": workdir, "name": "s1"})
    assert snap["ok"] is True
    assert "layout_overrides.json" in snap["data"]["files"]

    clear = registry.dispatch(
        "layout_set", {"workdir": workdir, "clear": True, "reason": "回滚"}
    )
    assert clear["ok"] is True
    assert (Path(workdir) / "agent" / "layout_overrides.json").exists()

    restore = registry.dispatch(
        "restore", {"workdir": workdir, "name": "s1", "apply": False}
    )
    assert restore["ok"] is True
    restored = json.loads(
        (Path(workdir) / "agent" / "layout_overrides.json").read_text(encoding="utf-8")
    )
    assert restored["paragraphs"]["P01-001"]["scale_cap"] == 0.9

    listed = registry.dispatch("list_snapshots", {"workdir": workdir})
    assert [item["name"] for item in listed["data"]["snapshots"]] == ["s1"]


def test_layout_set_invalid_patch_reports_errors(fake_workdir):
    result = registry.dispatch(
        "layout_set",
        {"workdir": str(fake_workdir), "patch": {"paragraphs": {"P01-001": {"nope": 1}}}},
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "invalid_patch"
