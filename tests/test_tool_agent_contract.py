"""U3 翻译/审查 Agent 契约：stdin/stdout 单一子进程协议。

不调真实模型：用 ``/bin/sh`` 假 Agent 验证
- ``run_translator`` 的三件事（stdin 进稿 / stdout 出稿 / 退出码语义）；
- ``translate`` 的 ``--translator`` / ``--markdown`` / ``--prompt-only`` 收敛；
- ``run`` 的 reviewer 契约与结构化失败码。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from babeldoc_tools import common
from babeldoc_tools import run as run_tool
from babeldoc_tools import translate as translate_tool

REPO_ROOT = Path(__file__).resolve().parents[1]

DOC_MD = (
    "<!-- BabelDOC markdown v1 -->\n\n"
    "<!-- id=P01-001 label=text -->\nHello world.\n\n"
    "<!-- id=P01-002 label=text -->\nSecond paragraph.\n"
)


def _cli(*argv: str) -> subprocess.CompletedProcess:
    # argv 全部由本测试构造，非外部输入拼接
    return subprocess.run(  # noqa: S603
        [sys.executable, "-m", "babeldoc_tools", *argv],
        capture_output=True,
        text=True,
        cwd=REPO_ROOT,
    )


def _script(tmp_path: Path, name: str, body: str) -> str:
    path = tmp_path / name
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)
    return str(path)


# --------------------------------------------------------------------------- #
# common.run_translator：唯一的 provider 机制
# --------------------------------------------------------------------------- #
def test_run_translator_pipes_prompt_to_stdin_and_returns_stdout(tmp_path):
    # 把 stdin 原样转大写再吐到 stdout；同时把收到的内容写到文件供断言
    seen = tmp_path / "seen.txt"
    command = _script(
        tmp_path,
        "agent.sh",
        f"#!/bin/sh\ntee {seen} | tr 'a-z' 'A-Z'\n",
    )
    out = common.run_translator("hello prompt", command, timeout_s=10)
    assert out.strip() == "HELLO PROMPT"
    assert seen.read_text(encoding="utf-8") == "hello prompt"


def test_run_translator_nonzero_exit_is_structured_error(tmp_path):
    command = _script(tmp_path, "bad.sh", "#!/bin/sh\necho boom >&2\nexit 3\n")
    with pytest.raises(common.ToolError) as excinfo:
        common.run_translator("x", command, timeout_s=10)
    assert excinfo.value.code == "translator_failed"
    assert "boom" in excinfo.value.message


def test_run_translator_empty_stdout_is_structured_error(tmp_path):
    command = _script(tmp_path, "silent.sh", "#!/bin/sh\ncat >/dev/null\n")
    with pytest.raises(common.ToolError) as excinfo:
        common.run_translator("x", command, timeout_s=10)
    assert excinfo.value.code == "translator_empty"


def test_run_translator_timeout_is_structured_error(tmp_path):
    command = _script(tmp_path, "slow.sh", "#!/bin/sh\nsleep 5\n")
    with pytest.raises(common.ToolError) as excinfo:
        common.run_translator("x", command, timeout_s=1)
    assert excinfo.value.code == "translator_timeout"


def test_run_translator_missing_command_is_structured_error():
    with pytest.raises(common.ToolError) as excinfo:
        common.run_translator("x", "/nonexistent/agent-command-xyz", timeout_s=10)
    assert excinfo.value.code == "translator_failed"


# --------------------------------------------------------------------------- #
# translate 子命令：--translator / --markdown / --prompt-only
# --------------------------------------------------------------------------- #
def _make_workdir(tmp_path: Path) -> Path:
    workdir = tmp_path / "wd"
    (workdir / "agent").mkdir(parents=True)
    (workdir / "agent" / "document.md").write_text(DOC_MD, encoding="utf-8")
    return workdir


@pytest.fixture
def no_missing_check(monkeypatch):
    """``missing_ids`` 需要真实 state.pkl；契约定点测试只关心命令往返，故中和它。"""
    from babeldoc.tools.agent import markdown_view

    monkeypatch.setattr(markdown_view, "missing_ids", lambda *_a, **_k: [])


def test_translate_with_translator_command(tmp_path, no_missing_check):  # noqa: ARG001
    workdir = _make_workdir(tmp_path)
    command = _script(
        tmp_path,
        "echo.sh",
        "#!/bin/sh\ncat >/dev/null\ncat " + str(workdir / "agent" / "document.md") + "\n",
    )
    payload = translate_tool.translate_document(
        str(workdir), translator=command, timeout=10
    )
    assert Path(payload["translated_md"]).read_text(encoding="utf-8") == DOC_MD
    assert (workdir / "agent" / "prompt.md").exists()


def test_translate_missing_translator_is_reported(tmp_path):
    workdir = _make_workdir(tmp_path)
    with pytest.raises(common.ToolError) as excinfo:
        translate_tool.translate_document(str(workdir))
    assert excinfo.value.code == "translator_missing"
    assert "--markdown" in excinfo.value.message
    assert "--prompt-only" in excinfo.value.message


def test_translate_prompt_only_does_not_call_command(tmp_path):
    workdir = _make_workdir(tmp_path)
    payload = translate_tool.translate_document(str(workdir), prompt_only=True)
    assert payload["prompt_only"] is True
    assert payload["translated_md"] is None
    assert (workdir / "agent" / "prompt.md").exists()


def test_translate_markdown_import_does_not_call_command(tmp_path, no_missing_check):  # noqa: ARG001
    workdir = _make_workdir(tmp_path)
    payload = translate_tool.translate_document(
        str(workdir), markdown=str(workdir / "agent" / "document.md"), timeout=10
    )
    assert Path(payload["translated_md"]).exists()


def test_cli_translate_missing_translator_exits_1(tmp_path):
    workdir = _make_workdir(tmp_path)
    out = _cli("translate", "--workdir", str(workdir))
    assert out.returncode == 1
    payload = json.loads(out.stdout)
    assert payload["ok"] is False
    assert payload["error"]["code"] == "translator_missing"


def test_cli_translate_help_drops_agy_flags():
    out = _cli("translate", "--help")
    assert out.returncode == 0, out.stderr
    assert "--translator" in out.stdout
    for flag in ("--model", "--effort", "--command"):
        assert flag not in out.stdout
    # --prompt 只能以 --prompt-only / --repair-prompt 出现，不能是独立旗标
    assert "  --prompt " not in out.stdout


# --------------------------------------------------------------------------- #
# reviewer 契约（run 的 review 阶段）
# --------------------------------------------------------------------------- #
def _reviewable_workdir(tmp_path: Path) -> Path:
    workdir = _make_workdir(tmp_path)
    agent = workdir / "agent"
    (agent / "translated.md").write_text(DOC_MD, encoding="utf-8")
    (agent / "apply_report.json").write_text(
        json.dumps({"ok": True, "applied": 2}), encoding="utf-8"
    )
    (agent / "review_verdict.json").write_text(
        json.dumps({"verdict": "pass"}), encoding="utf-8"
    )
    out = workdir / "output"
    out.mkdir(parents=True, exist_ok=True)
    (out / "paper.zh.mono.pdf").write_bytes(b"%PDF-1.4 stub\n")
    return workdir


def test_reviewer_records_agent_review(tmp_path):
    workdir = _reviewable_workdir(tmp_path)
    command = _script(
        tmp_path,
        "pass-rev.sh",
        '#!/bin/sh\ncat >/dev/null\nprintf \'%s\' \'{"verdict":"pass","findings":[]}\'\n',
    )
    result = run_tool.run_pipeline(
        str(workdir), from_stage="review", reviewer=command, timeout=10
    )
    assert result["ok"] is True
    review = json.loads(
        (workdir / "agent" / "agent_review.json").read_text(encoding="utf-8")
    )
    assert review["verdict"] == "pass"
    assert review["findings"] == []
    assert (workdir / "agent" / "review_prompt.md").exists()


def test_reviewer_invalid_json_is_reported(tmp_path):
    workdir = _reviewable_workdir(tmp_path)
    command = _script(tmp_path, "bad-rev.sh", "#!/bin/sh\ncat >/dev/null\necho not json\n")
    result = run_tool.run_pipeline(
        str(workdir), from_stage="review", reviewer=command, timeout=10
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "reviewer_invalid_json"
    # workdir 保留供恢复
    assert (workdir / "agent" / "review_prompt.md").exists()


def test_reviewer_failure_is_reported(tmp_path):
    workdir = _reviewable_workdir(tmp_path)
    command = _script(tmp_path, "fail-rev.sh", "#!/bin/sh\ncat >/dev/null\nexit 2\n")
    result = run_tool.run_pipeline(
        str(workdir), from_stage="review", reviewer=command, timeout=10
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "reviewer_failed"


def test_reviewer_bad_verdict_is_invalid_json(tmp_path):
    workdir = _reviewable_workdir(tmp_path)
    command = _script(
        tmp_path,
        "wrong-verdict.sh",
        '#!/bin/sh\ncat >/dev/null\nprintf \'%s\' \'{"verdict":"maybe","findings":[]}\'\n',
    )
    result = run_tool.run_pipeline(
        str(workdir), from_stage="review", reviewer=command, timeout=10
    )
    assert result["ok"] is False
    assert result["error"]["code"] == "reviewer_invalid_json"
