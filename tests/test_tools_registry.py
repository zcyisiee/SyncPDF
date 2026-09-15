"""``bdt`` CLI 契约：子命令存在性 / stdout 单行 JSON / 错误路径退出码。

U2 起工具层不再有注册表 / JSON Schema / ``call`` 元命令；测试改为针对
``babeldoc_tools.__main__`` 的 7 个固定子命令（``run`` 由后续阶段注册）。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

SUBCOMMANDS = {
    "parse",
    "translate",
    "apply",
    "build",
    "check",
    "layout-set",
    "report",
}


def _run(*argv: str) -> subprocess.CompletedProcess:
    # argv 全部由本测试构造，非外部输入拼接
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "babeldoc_tools", *argv],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )


def test_main_help_lists_subcommands():
    out = _run("--help")
    assert out.returncode == 0, out.stderr
    for name in SUBCOMMANDS:
        assert name in out.stdout


@pytest.mark.parametrize("name", sorted(SUBCOMMANDS))
def test_each_subcommand_help(name):
    out = _run(name, "--help")
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip()


def test_parse_help_exposes_layout_choices():
    out = _run("parse", "--help")
    assert out.returncode == 0, out.stderr
    assert "mineru" in out.stdout and "paddle" in out.stdout
    assert "--layout-coverage-threshold" in out.stdout
    assert "--mineru-cache-key" in out.stdout


def test_translate_help_exposes_ids_and_prompt_only():
    out = _run("translate", "--help")
    assert out.returncode == 0, out.stderr
    assert "--ids" in out.stdout
    assert "--prompt-only" in out.stdout
    assert "--markdown" in out.stdout


def test_build_help_exposes_render_and_dual():
    out = _run("build", "--help")
    assert out.returncode == 0, out.stderr
    assert "--render" in out.stdout
    assert "--dual" in out.stdout
    assert "--no-latex-bbox" in out.stdout


def test_missing_workdir_is_single_line_json_and_exit_1():
    out = _run("build", "--workdir", "/nonexistent/babeldoc-workdir")  # noqa: S108
    assert out.returncode == 1
    payload = json.loads(out.stdout)  # 单次 loads 必须成功（stdout 只有一行 JSON）
    assert payload["ok"] is False
    assert payload["error"]["code"] == "workdir_missing"


def test_translate_missing_workdir_is_json_error(tmp_path):
    out = _run("translate", "--workdir", str(tmp_path / "nope"))
    assert out.returncode == 1
    payload = json.loads(out.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "workdir_missing"


def test_layout_set_invalid_patch_json_is_reported(tmp_path):
    (tmp_path / "agent").mkdir()
    out = _run("layout-set", "--workdir", str(tmp_path), "--patch", "{not json}")
    assert out.returncode == 1
    payload = json.loads(out.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_patch"


def test_layout_set_patch_roundtrip(tmp_path):
    """真实工作目录冒烟：写覆盖 → 返回 diff（不触发重建）。"""
    agent = tmp_path / "agent"
    agent.mkdir()
    (agent / "anchors.json").write_text(
        json.dumps({"rows": [{"id": "P01-001", "layout_label": "text"}]}),
        encoding="utf-8",
    )
    out = _run(
        "layout-set",
        "--workdir",
        str(tmp_path),
        "--patch",
        json.dumps({"paragraphs": {"P01-001": {"scale_cap": 0.9}}}),
        "--reason",
        "测试",
    )
    assert out.returncode == 0, out.stderr
    payload = json.loads(out.stdout)
    assert payload["ok"] is True
    assert payload["data"]["diff"]["added"] == ["paragraphs.P01-001"]

    out = _run("layout-set", "--workdir", str(tmp_path), "--patch", '{"paragraphs": {"P01-001": {"nope": 1}}}')
    assert out.returncode == 1
    payload = json.loads(out.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "invalid_patch"

    out = _run("layout-set", "--workdir", str(tmp_path), "--clear", "--reason", "回滚")
    assert out.returncode == 0, out.stderr
    assert json.loads(out.stdout)["ok"] is True


def test_removed_meta_commands_are_gone():
    """``call`` / ``list`` / ``schema`` 元层随 U2 删除。"""
    for name in ("call", "list", "schema"):
        out = _run(name)
        assert out.returncode == 2  # argparse invalid choice
        assert "invalid choice" in out.stderr


def test_bdt_console_script_is_installed():
    """``bdt`` 入口点必须可用（[project.scripts] 注册后需 uv sync）。"""
    import shutil

    assert shutil.which("bdt") is not None
