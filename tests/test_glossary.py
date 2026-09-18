"""术语表（W13）：CSV 编解码/校验 + 提示词注入 + ``bdt translate --glossaries``。

分三层，都是**不调真实模型**的定点测试：

1. :mod:`babeldoc_tools.glossary`（CLI 与 serve 共用的唯一一份词表实现）：CSV 往返、
   校验、去重排序、提示词段渲染；
2. :func:`babeldoc_tools.common.load_prompt` 的 ``{glossary}`` 占位行：有词表替换、
   空词表整行省略，**且不带词表时的提示词与加占位符之前的模板逐字节一致**；
3. 注入链路：``translate_document(glossaries=...)`` 把术语块送进子进程的 stdin
   （stub translator 把 stdin 抄下来），``--ids`` 重译**不注入**。
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

import pytest
from babeldoc_tools import common
from babeldoc_tools import glossary
from babeldoc_tools import translate as translate_tool
from babeldoc_tools.glossary import GlossaryEntry

REPO_ROOT = Path(__file__).resolve().parents[1]

DOC_MD = (
    "<!-- BabelDOC markdown v1 -->\n\n"
    "<!-- id=P01-001 label=text -->\nHello world.\n\n"
    "<!-- id=P01-002 label=text -->\nSecond paragraph.\n"
)


def _cli(*argv: str) -> subprocess.CompletedProcess:
    return subprocess.run(  # noqa: S603 - argv 全部由本测试构造
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
# CSV 编解码 + 校验（CLI 与 serve 共用这一份）
# --------------------------------------------------------------------------- #
def test_csv_round_trip_keeps_order_columns_and_note():
    entries = [
        GlossaryEntry("attention", "注意力"),
        GlossaryEntry("ThinLTO", "ThinLTO 链接时优化", "厂商写法"),
    ]
    text = glossary.render_csv(entries)
    assert text.splitlines()[0] == "source,target,note"
    assert glossary.parse_csv(text) == entries
    # 第三条 note 为空时 CSV 里是空字段，读回来仍是 None（不是空串）
    assert glossary.parse_csv(text)[0].note is None


def test_parse_csv_requires_source_and_target_columns():
    with pytest.raises(common.ToolError) as excinfo:
        glossary.parse_csv("term,translation\na,b\n")
    assert excinfo.value.code == "glossary_invalid"
    assert "source" in excinfo.value.message


def test_parse_csv_tolerates_utf8_bom():
    entries = glossary.parse_csv("\ufeffsource,target\na,b\n")
    assert entries == [GlossaryEntry("a", "b", None)]


def test_normalize_dedupes_by_source_last_one_wins_and_sorts():
    normalized = glossary.normalize_entries(
        [
            GlossaryEntry("b", "乙"),
            GlossaryEntry("a", "甲"),
            GlossaryEntry("  b  ", " 乙（覆盖） ", " 备注 "),
        ]
    )
    assert normalized == [
        GlossaryEntry("a", "甲"),
        GlossaryEntry("b", "乙（覆盖）", "备注"),
    ]


@pytest.mark.parametrize(
    ("raw", "field"),
    [
        (GlossaryEntry("", "甲"), "source"),
        (GlossaryEntry("   ", "甲"), "source"),
        (GlossaryEntry("a", ""), "target"),
    ],
)
def test_normalize_rejects_blank_side_with_index_and_field(raw, field):
    with pytest.raises(common.ToolError) as excinfo:
        glossary.normalize_entries([raw])
    assert excinfo.value.code == "glossary_invalid"
    assert excinfo.value.extra["index"] == 0
    assert excinfo.value.extra["field"] == field


def test_normalize_enforces_length_limits():
    with pytest.raises(common.ToolError) as excinfo:
        glossary.normalize_entries(
            [GlossaryEntry("a" * (glossary.MAX_SOURCE_CHARS + 1), "甲")]
        )
    assert excinfo.value.extra["field"] == "source"
    with pytest.raises(common.ToolError) as excinfo:
        glossary.normalize_entries(
            [
                GlossaryEntry(
                    "a", "甲", "x" * (glossary.MAX_NOTE_CHARS + 1)
                )
            ]
        )
    assert excinfo.value.extra["field"] == "note"


def test_load_entries_missing_and_broken_file_are_structured_errors(tmp_path):
    with pytest.raises(common.ToolError) as excinfo:
        glossary.load_entries(tmp_path / "nope.csv")
    assert excinfo.value.code == "glossary_missing"

    broken = tmp_path / "broken.csv"
    broken.write_text("term,translation\na,b\n", encoding="utf-8")
    with pytest.raises(common.ToolError) as excinfo:
        glossary.load_entries(broken)
    assert excinfo.value.code == "glossary_invalid"


def test_render_prompt_block_is_empty_for_empty_table_and_lists_pairs():
    assert glossary.render_prompt_block([]) == ""
    block = glossary.render_prompt_block(
        [GlossaryEntry("attention", "注意力"), GlossaryEntry("LTO", "链接时优化", "全大写")]
    )
    assert block.startswith(glossary.PROMPT_HEADING + "\n")
    assert "\n- attention → 注意力\n" in block
    assert "\n- LTO → 链接时优化（全大写）\n" in block


# --------------------------------------------------------------------------- #
# load_prompt 的 {glossary} 占位行
# --------------------------------------------------------------------------- #
def _template_body(path: Path) -> str:
    """取模板 ```text 块（与 :func:`common.load_prompt` 同一读法）。"""
    return re.search(r"```text\n(.*?)```", path.read_text(encoding="utf-8"), re.S).group(
        1
    )


def test_load_prompt_is_byte_identical_to_the_template_without_the_placeholder():
    """不带词表时渲染结果 = 「把占位行整行删掉」的模板（W13 向后兼容的硬要求）。"""
    template = common.AGENTS_DIR / "translator.md"
    expected = _template_body(template).replace("{glossary}\n", "").replace(
        "{document}", "DOC"
    )
    assert common.load_prompt("translator", document="DOC") == expected
    # 空词表（不是 None）走同一条路
    assert common.load_prompt("translator", document="DOC", glossary=[]) == expected


def test_load_prompt_renders_glossary_between_requirements_and_document():
    rendered = common.load_prompt(
        "translator",
        document="DOC",
        glossary=[GlossaryEntry("attention", "注意力")],
    )
    assert "{glossary}" not in rendered
    assert "\n## 术语约束（词表）\n" in rendered
    assert "- attention → 注意力" in rendered
    # 词表段在文档之前、要求之后
    assert rendered.index("术语约束") < rendered.index("## 待翻译文档开始")
    assert rendered.rstrip().endswith("## 待翻译文档结束")


def test_load_prompt_without_the_placeholder_does_not_error(tmp_path):
    template = tmp_path / "t.md"
    template.write_text("```text\nhello {document}\n```\n", encoding="utf-8")
    assert common.load_prompt(str(template), document="X") == "hello X\n"
    assert (
        common.load_prompt(
            str(template), document="X", glossary=[GlossaryEntry("a", "甲")]
        )
        == "hello X\n"
    )


# --------------------------------------------------------------------------- #
# 注入链路：bdt translate --glossaries <csv>
# --------------------------------------------------------------------------- #
def _make_workdir(tmp_path: Path) -> Path:
    """最小 workdir：``document.md`` + ``anchors.json``（重译路径读它做合并）。"""
    workdir = tmp_path / "wd"
    agent = workdir / "agent"
    agent.mkdir(parents=True)
    (agent / "document.md").write_text(DOC_MD, encoding="utf-8")
    rows = [
        {
            "id": pid,
            "page": 0,
            "layout_label": "text",
            "canonical": f"Source of {pid}",
            "markdown": f"Source of {pid}",
            "anchors": "[]",
        }
        for pid in ("P01-001", "P01-002")
    ]
    (agent / "anchors.json").write_text(
        json.dumps({"rows": rows, "skipped": []}), encoding="utf-8"
    )
    return workdir


@pytest.fixture
def no_missing_check(monkeypatch):
    """``missing_ids`` 需要真实 state.pkl；定点测试只关心提示词内容，故中和它。"""
    from babeldoc.tools.agent import markdown_view

    monkeypatch.setattr(markdown_view, "missing_ids", lambda *_a, **_k: [])


def _echo_stdin_script(tmp_path: Path, workdir: Path, seen: Path) -> str:
    """stub translator：把收到的提示词抄到 ``seen``，再把原文当译文吐回去。"""
    return _script(
        tmp_path,
        "translator.sh",
        f"#!/bin/sh\ntee {seen} >/dev/null\ncat {workdir / 'agent' / 'document.md'}\n",
    )


def _glossary_csv(tmp_path: Path) -> Path:
    path = tmp_path / "glossary.csv"
    path.write_text(
        glossary.render_csv(
            [GlossaryEntry("attention", "注意力"), GlossaryEntry("LTO", "链接时优化")]
        ),
        encoding="utf-8",
    )
    return path


def test_translate_injects_glossary_into_the_translator_prompt(
    tmp_path, no_missing_check  # noqa: ARG001
):
    workdir = _make_workdir(tmp_path)
    seen = tmp_path / "seen-prompt.txt"
    command = _echo_stdin_script(tmp_path, workdir, seen)
    csv_path = _glossary_csv(tmp_path)

    translate_tool.translate_document(
        str(workdir), translator=command, timeout=10, glossaries=str(csv_path)
    )

    prompt = seen.read_text(encoding="utf-8")
    assert "## 术语约束（词表）" in prompt
    assert "- attention → 注意力" in prompt
    assert "- LTO → 链接时优化" in prompt
    # 术语块在待翻译文档之前，文档本体没被改
    assert prompt.index("术语约束") < prompt.index("## 待翻译文档开始")
    assert "Hello world." in prompt
    # 落盘的提示词与送进子进程的一致（prompt.md 是证据）
    assert (workdir / "agent" / "prompt.md").read_text(encoding="utf-8") == prompt


def test_translate_without_glossary_does_not_inject(
    tmp_path, no_missing_check  # noqa: ARG001
):
    workdir = _make_workdir(tmp_path)
    seen = tmp_path / "seen-prompt.txt"
    command = _echo_stdin_script(tmp_path, workdir, seen)

    translate_tool.translate_document(str(workdir), translator=command, timeout=10)

    prompt = seen.read_text(encoding="utf-8")
    assert "术语约束" not in prompt and "{glossary}" not in prompt


def test_retranslate_by_ids_does_not_inject_glossary(
    tmp_path, no_missing_check  # noqa: ARG001
):
    """重译候选不注入（红线）：走 translator-repair 模板，提示词里没有术语块。"""
    workdir = _make_workdir(tmp_path)
    seen = tmp_path / "seen-retry.txt"
    command = _script(
        tmp_path,
        "retry.sh",
        "#!/bin/sh\ntee "
        + str(seen)
        + "\ncat <<'EOF'\n<!-- id=P01-001 label=text -->\n你好世界。\nEOF\n",
    )
    csv_path = _glossary_csv(tmp_path)

    translate_tool.translate_document(
        str(workdir),
        ids=["P01-001"],
        translator=command,
        timeout=10,
        glossaries=str(csv_path),
    )

    prompt = seen.read_text(encoding="utf-8")
    assert "术语约束" not in prompt and "attention" not in prompt


def test_translate_missing_glossary_file_is_a_structured_error(tmp_path):
    workdir = _make_workdir(tmp_path)
    with pytest.raises(common.ToolError) as excinfo:
        translate_tool.translate_document(
            str(workdir), prompt_only=True, glossaries=str(tmp_path / "nope.csv")
        )
    assert excinfo.value.code == "glossary_missing"


def test_cli_exposes_glossaries_on_translate_and_run():
    for command in ("translate", "run"):
        help_text = _cli(command, "--help")
        assert help_text.returncode == 0, help_text.stderr
        assert "--glossaries" in help_text.stdout


def test_cli_translate_prompt_only_writes_the_glossary_block(tmp_path):
    """CLI 级验收：``--glossaries`` 一路透传到提示词（--prompt-only 只写文件）。"""
    workdir = _make_workdir(tmp_path)
    csv_path = _glossary_csv(tmp_path)
    result = _cli(
        "translate",
        "--workdir",
        str(workdir),
        "--prompt-only",
        "--glossaries",
        str(csv_path),
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["ok"] is True
    prompt = (workdir / "agent" / "prompt.md").read_text(encoding="utf-8")
    assert "- attention → 注意力" in prompt
