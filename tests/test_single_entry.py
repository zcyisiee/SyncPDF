"""单一入口守卫：`bdt` 是唯一对外接口，防止并行入口/同名包/死路径回归。

本文件是 U6 引入的"护栏测试"：任何新增的第二入口、被删模块的复活、
陈旧字符串的回归都会在这里失败。断言尽量基于**仓库事实**（文件系统 + git），
不依赖网络、PDF 或真实模型。
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from babeldoc_tools import common

REPO_ROOT = Path(__file__).resolve().parents[1]

#: 仓库中被删除、不得复活的并行入口/模块。
FORBIDDEN_PATHS = (
    "babeldoc/tools/agent/__main__.py",
    "experiments/markdown_translate.py",
    "experiments/batch_translate.py",
    "babeldoc/main.py",
    "babeldoc_core",
)

#: 已删除模块/接口的字符串，不得出现在受版本管理的文本里。
FORBIDDEN_PATTERNS = ("babeldoc_core", "tools/executor", "rpc_doclayout")


def _git_grep(pattern: str) -> list[str]:
    """在受版本管理的文件里搜索（cwd=仓库根）；命中行以 ``文件:行:内容`` 返回。"""
    git = shutil.which("git") or "git"
    # 排除本文件自身：FORBIDDEN_PATTERNS 的字面量会命中 grep。
    result = subprocess.run(  # noqa: S603 - 固定 argv，无外部输入
        [
            git,
            "grep",
            "-n",
            pattern,
            "--",
            ".",
            ":!.plan",
            ":!tests/test_single_entry.py",
        ],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )
    # git grep：无命中退出码 1（正常），其它非 0/1 才算错误
    if result.returncode not in (0, 1):
        raise AssertionError(f"git grep 失败: {result.stderr}")
    return [line for line in result.stdout.splitlines() if line.strip()]


def _cli(*argv: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    """跑 ``python -m babeldoc_tools``（= bdt），capture stdout/stderr。"""
    return subprocess.run(  # noqa: S603 - argv 由测试构造
        [sys.executable, "-m", "babeldoc_tools", *argv],
        capture_output=True,
        text=True,
        cwd=cwd or REPO_ROOT,
    )


# --------------------------------------------------------------------------- #
# 入口唯一性
# --------------------------------------------------------------------------- #
def test_project_scripts_only_bdt():
    """`[project.scripts]` 只注册 `bdt` 一项，不得有第二个控制台入口。"""
    tomllib = pytest.importorskip("tomllib")  # Python 3.10 无标准库 tomllib
    data = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = data["project"]["scripts"]
    assert list(scripts) == ["bdt"]
    assert scripts["bdt"] == "babeldoc_tools.__main__:main"


def test_single_babeldoc_tools_directory():
    """全仓只有一个 `babeldoc_tools` 包目录（排除 .venv/build/.git/tmp）。"""
    found = [
        path
        for path in REPO_ROOT.rglob("babeldoc_tools")
        if path.is_dir()
        and ".venv" not in path.parts
        and "build" not in path.parts
        and ".git" not in path.parts
        and "tmp" not in path.parts
    ]
    assert found == [REPO_ROOT / "babeldoc_tools"], found


@pytest.mark.parametrize("pattern", FORBIDDEN_PATTERNS)
def test_no_removed_module_references(pattern):
    """`git grep` 不到已删除模块（babeldoc_core / tools/executor / rpc_doclayout）。"""
    hits = _git_grep(pattern)
    assert hits == [], "\n".join(hits)


@pytest.mark.parametrize("relpath", FORBIDDEN_PATHS)
def test_forbidden_paths_do_not_exist(relpath):
    """被删除的并行入口与包不存在。"""
    assert not (REPO_ROOT / relpath).exists(), relpath


# --------------------------------------------------------------------------- #
# 翻译 / 审查子进程协议冒烟（不依赖 PDF 与网络）
# --------------------------------------------------------------------------- #
def test_subprocess_protocol_uses_shlex_split():
    """被调命令字符串经 `shlex.split` 拆成 argv、不经 shell。"""
    assert shlex.split('sh -c "printf hi"') == ["sh", "-c", "printf hi"]
    # 引号不匹配是明确的可预期错误（不是 shell 执行）
    with pytest.raises(common.ToolError) as excinfo:
        common._run_subprocess("prompt", 'echo "unterminated', 5, "translator")
    assert excinfo.value.code == "translator_failed"


def test_run_translator_reads_stdin_writes_stdout():
    """正常路径：命令从 stdin 读提示词、把译文写到 stdout。"""
    out = common.run_translator("HELLO", "cat", timeout_s=10)
    assert out == "HELLO"


def test_translator_failure_code():
    """命令非 0 退出 → `translator_failed`；stdout 为空 → `translator_empty`。"""
    with pytest.raises(common.ToolError) as excinfo:
        common.run_translator("prompt", "false", timeout_s=10)
    assert excinfo.value.code == "translator_failed"

    with pytest.raises(common.ToolError) as excinfo:
        common.run_translator("prompt", "true", timeout_s=10)
    assert excinfo.value.code == "translator_empty"

    with pytest.raises(common.ToolError) as excinfo:
        common.run_translator("prompt", "definitely-not-a-real-binary-xyz", timeout_s=10)
    assert excinfo.value.code == "translator_failed"


def test_prompt_paths_resolve_to_agents_dir():
    """提示词只在 `agents/` 下解析（prompts/ 回退已删除）。"""
    assert common.prompt_path("translator") == common.AGENTS_DIR / "translator.md"
    assert "prompts" not in str(common.AGENTS_DIR)
    # 不存在的提示词明确报错
    with pytest.raises(common.ToolError) as excinfo:
        common.load_prompt("no-such-prompt-xyz")
    assert excinfo.value.code == "prompt_missing"


def _make_min_workdir(tmp_path: Path, *, apply_ok: bool) -> Path:
    """构造最小 workdir：check 只需 sheet/translated/apply_report/state.pkl。"""
    import json
    import pickle

    agent = tmp_path / "wd" / "agent"
    agent.mkdir(parents=True)
    (agent / "state.pkl").write_bytes(pickle.dumps({"pdf_path": None}))
    (agent / "sheet.jsonl").write_text(
        json.dumps(
            {"id": "P01-001", "page": 1, "layout_label": "text", "source": "Hello world."}
        )
        + "\n",
        encoding="utf-8",
    )
    (agent / "translated.jsonl").write_text(
        json.dumps({"id": "P01-001", "target": "你好，世界。"}) + "\n", encoding="utf-8"
    )
    (agent / "apply_report.json").write_text(
        json.dumps(
            {
                "ok": apply_ok,
                "applied": 1 if apply_ok else 0,
                "violations": [] if apply_ok else ["extra_ids: P99-999"],
                "fallback_ids": [],
            }
        ),
        encoding="utf-8",
    )
    return tmp_path / "wd"


def test_check_strict_exit_code(tmp_path):
    """`check --strict`：verdict 非 pass → exit 1；默认仍 exit 0。"""
    import json

    failing = _make_min_workdir(tmp_path, apply_ok=False)
    strict = _cli("check", "--workdir", str(failing), "--skip-pdf-checks", "--strict")
    assert strict.returncode == 1
    assert json.loads(strict.stdout)["data"]["verdict"] == "needs_fix"

    lax = _cli("check", "--workdir", str(failing), "--skip-pdf-checks")
    assert lax.returncode == 0
    assert json.loads(lax.stdout)["data"]["verdict"] == "needs_fix"


def test_run_without_reviewer_stops_waiting(tmp_path):
    """无 `--reviewer`：run 停在 review，以 `waiting_for_reviewer` exit 1 结束。"""
    import json

    workdir = _make_min_workdir(tmp_path, apply_ok=True)
    (workdir / "agent" / "review_verdict.json").write_text("{}", encoding="utf-8")

    out = _cli("run", "--workdir", str(workdir), "--from", "review")

    assert out.returncode == 1
    payload = json.loads(out.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "waiting_for_reviewer"
    assert payload["data"]["stopped_at"] == "review"
    # 审查提示词已落盘，供上层 Agent 接手
    assert (workdir / "agent" / "review_prompt.md").exists()


def test_cli_help_lists_ten_subcommands():
    """`bdt --help` 暴露 10 个固定子命令（含 debug/serve），没有 call/list/schema 元命令。"""
    out = _cli("--help")
    assert out.returncode == 0, out.stderr
    for command in (
        "parse",
        "translate",
        "apply",
        "build",
        "check",
        "layout-set",
        "report",
        "debug",
        "run",
        "serve",
    ):
        assert command in out.stdout
    for removed in ("call", "list", "schema"):
        assert f" {removed} " not in out.stdout
