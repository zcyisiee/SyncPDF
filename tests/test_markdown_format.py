"""Markdown 结构前缀的渲染 / 清洗 / 回填一致性（翻译格式优化）。

背景：翻译模型看到的是一篇连续 Markdown（`document.md`，一次调用整篇翻译）。
渲染阶段给段落加结构前缀（`#` 标题层級 / `- ` 列表 / `*` 图注 / `**` 表注），
让模型感知段落角色、保持层级与列表切分；回填阶段由 `_clean_markdown_body`
剥掉前缀，不让 `#`/`- ` 进入译文 IR。

本文件覆盖：
- 渲染：标题层级（含首个 `#` 被占用的顺序场景）、列表、caption；
- 清洗：成对/单侧/错配星号、列表前缀、`#{1,6}`；
- 往返：render → parse → clean 后正文与源文相等（无前缀残留）；
- apply：label 改写告警、空译文回退原文。
"""

from __future__ import annotations

import pytest
from babeldoc.tools.agent import markdown_view as mv


def _rows(*items):
    return [
        {"id": pid, "layout_label": label, "markdown": body}
        for pid, label, body in items
    ]


# --------------------------------------------------------------------------- #
# 渲染：标题层级
# --------------------------------------------------------------------------- #
class TestHeadingRendering:
    def test_first_title_takes_h1_rest_take_h2(self):
        md = mv.render_rows_markdown(
            _rows(
                ("P01-001", "title", "A Great Paper"),
                ("P01-002", "title", "I. Introduction"),
                ("P01-003", "title", "II. Method"),
            )
        )
        assert "# A Great Paper" in md
        assert "## I. Introduction" in md
        assert "## II. Method" in md

    def test_doc_title_takes_h1_when_present_first(self):
        md = mv.render_rows_markdown(
            _rows(
                ("P01-001", "doc_title", "DeepSeek-V4.1-Flash"),
                ("P01-002", "title", "1 Introduction"),
            )
        )
        assert "# DeepSeek-V4.1-Flash" in md
        assert "## 1 Introduction" in md

    def test_doc_title_after_title_downgrades_when_h1_taken(self):
        md = mv.render_rows_markdown(
            _rows(
                ("P01-001", "title", "A Great Paper"),
                ("P01-002", "doc_title", "Subtitle"),
            )
        )
        assert "# A Great Paper" in md
        assert "## Subtitle" in md

    def test_paragraph_title_is_h3(self):
        md = mv.render_rows_markdown(
            _rows(
                ("P01-001", "title", "A Great Paper"),
                ("P01-002", "paragraph_title", "2.1 Overview"),
            )
        )
        assert "### 2.1 Overview" in md

    def test_exactly_one_h1_in_whole_document(self):
        md = mv.render_rows_markdown(
            _rows(
                ("P01-001", "title", "A"),
                ("P01-002", "title", "B"),
                ("P01-003", "title", "C"),
            )
        )
        assert sum(1 for line in md.splitlines() if line.startswith("# ")) == 1

    def test_toc_entry_rendered_as_plain_line(self):
        md = mv.render_rows_markdown(_rows(("P02-002", "toc_entry", "1 Introduction")))
        assert "# 1 Introduction" not in md
        assert "\n1 Introduction\n" in md


# --------------------------------------------------------------------------- #
# 渲染：列表与 caption
# --------------------------------------------------------------------------- #
class TestListRendering:
    @pytest.mark.parametrize(
        "body",
        ["① First item", "• Bullet item", "■ Boxed item", "▪ Small square"],
    )
    def test_bullet_chars_get_list_prefix(self, body):
        md = mv.render_rows_markdown(_rows(("P01-001", "text", body)))
        assert f"- {body}" in md

    def test_existing_dash_prefix_not_duplicated(self):
        md = mv.render_rows_markdown(_rows(("P01-001", "text", "- Already a list")))
        assert "- Already a list" in md
        assert "- - Already a list" not in md

    def test_leading_anchor_before_bullet_still_detected(self):
        md = mv.render_rows_markdown(
            _rows(("P01-001", "text", "[[S1]]① First item[[/S1]]"))
        )
        assert "- [[S1]]① First item[[/S1]]" in md

    def test_ordinary_sentence_not_treated_as_list(self):
        md = mv.render_rows_markdown(
            _rows(("P01-001", "text", "The quick brown fox jumps."))
        )
        assert "\nThe quick brown fox jumps.\n" in md


class TestCaptionRendering:
    def test_figure_caption_single_star(self):
        md = mv.render_rows_markdown(_rows(("P01-001", "figure_caption", "Figure 1: X")))
        assert "*Figure 1: X*" in md

    def test_table_caption_double_star(self):
        md = mv.render_rows_markdown(_rows(("P01-001", "table_caption", "TABLE II")))
        assert "**TABLE II**" in md


# --------------------------------------------------------------------------- #
# 清洗
# --------------------------------------------------------------------------- #
class TestCleanMarkdownBody:
    @pytest.mark.parametrize("level", ["#", "##", "###", "######"])
    def test_strips_heading_prefix_any_level(self, level):
        assert mv._clean_markdown_body(f"{level} 一、引言", "title") == "一、引言"

    def test_strips_list_prefix_only_when_source_was_list(self):
        # 源文是列表 → 我们加过 `- ` → 译文的前缀要剥
        assert (
            mv._clean_markdown_body("- 第一项", "text", "① First item") == "第一项"
        )

    def test_keeps_dash_when_source_was_not_list(self):
        # 源文不是列表 → 前缀不是我们加的 → 保留正文里的 `- `
        assert (
            mv._clean_markdown_body("-O3 是一个编译选项", "text", "-O3 is a flag")
            == "-O3 是一个编译选项"
        )

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("*图 1：示意*", "图 1：示意"),
            ("*图 1：示意", "图 1：示意"),  # 单侧
            ("图 1：示意*", "图 1：示意"),  # 单侧
            ("**图 1：示意**", "图 1：示意"),  # 错配（图注写成双星）
        ],
    )
    def test_figure_caption_star_tolerance(self, raw, expected):
        assert mv._clean_markdown_body(raw, "figure_caption") == expected

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("**表 II**", "表 II"),
            ("**表 II", "表 II"),
            ("表 II**", "表 II"),
            ("*表 II*", "表 II"),
        ],
    )
    def test_table_caption_star_tolerance(self, raw, expected):
        assert mv._clean_markdown_body(raw, "table_caption") == expected

    def test_strips_html_comment_residue(self):
        raw = "<!-- babeldoc-markdown v1 -->正文"
        assert mv._clean_markdown_body(raw, "text") == "正文"

    def test_folds_internal_newlines(self):
        assert (
            mv._clean_markdown_body("第一行\n第二行", "text") == "第一行 第二行"
        )

    def test_empty_body_stays_empty(self):
        assert mv._clean_markdown_body("   ", "text") == ""


# --------------------------------------------------------------------------- #
# 往返：render → parse → clean 无前缀残留
# --------------------------------------------------------------------------- #
class TestRoundTrip:
    @pytest.mark.parametrize(
        ("label", "body"),
        [
            ("title", "A Great Paper"),
            ("paragraph_title", "2.1 Overview"),
            ("figure_caption", "Figure 1: Architecture"),
            ("table_caption", "TABLE II"),
            ("text", "Ordinary sentence."),
            ("text", "① Circled list item"),
            ("text", "• Bullet item"),
        ],
    )
    def test_render_parse_clean_equals_source_body(self, label, body):
        md = mv.render_rows_markdown(_rows(("P01-001", label, body)))
        parsed = mv.parse_translated_markdown(md)
        parsed_body, _ = parsed["P01-001"]
        cleaned = mv._clean_markdown_body(parsed_body, label, body)
        assert cleaned == body

    def test_cleaned_body_has_no_structural_prefix(self):
        rows = [
            ("P01-001", "title", "A Great Paper"),
            ("P01-002", "paragraph_title", "2.1 Overview"),
            ("P01-003", "text", "① Item"),
        ]
        md = mv.render_rows_markdown(_rows(*rows))
        parsed = mv.parse_translated_markdown(md)
        for pid, label, source in rows:
            body, _ = parsed[pid]
            cleaned = mv._clean_markdown_body(body, label, source)
            assert not cleaned.startswith("#")
            assert not cleaned.startswith("- ")
            assert cleaned == source

    def test_old_translation_without_prefixes_still_parses(self):
        """兼容旧译文（模型未加前缀）：清洗不应改动正文。"""
        md = "<!-- id=P01-001 label=title -->\n旧译文标题\n"
        parsed = mv.parse_translated_markdown(md)
        body, _ = parsed["P01-001"]
        assert mv._clean_markdown_body(body, "title", "Old Title") == "旧译文标题"


# --------------------------------------------------------------------------- #
# apply 报告：label 改写 / 空译文
# --------------------------------------------------------------------------- #
def _make_state(tmp_path, inputs, labels):
    """构造 apply_markdown 所需的最小 workdir（state.pkl + anchors.json）。"""
    import json
    import pickle

    agent = tmp_path / "agent"
    agent.mkdir(parents=True, exist_ok=True)
    (agent / "anchors.json").write_text(
        json.dumps(
            {
                "rows": [
                    {"id": pid, "page": 0, "layout_label": label, "source": ""}
                    for pid, label in labels.items()
                ]
            }
        ),
        encoding="utf-8",
    )
    state = {"inputs": inputs}
    with (agent / "state.pkl").open("wb") as f:
        pickle.dump(state, f)
    return tmp_path


@pytest.fixture()
def _apply_env(tmp_path, monkeypatch):
    """支持 apply_markdown 的轻量环境：state 与 anchors 由测试注入。"""

    def build(inputs, labels):
        return _make_state(tmp_path, inputs, labels)

    # workflow.apply 会读 state 并写回 IR —— 这里替换为记录型桩
    captured = {}

    def fake_apply(workdir, sheet):  # noqa: ARG001 - 只记录 sheet
        captured["sheet"] = sheet
        return {"ok": True, "applied": 0}

    monkeypatch.setattr(mv.workflow, "apply", fake_apply)
    return build, captured


def _ti(unicode_text):
    from types import SimpleNamespace

    return SimpleNamespace(unicode=unicode_text)


class TestApplyReport:
    def test_label_mismatch_recorded_as_warning(self, _apply_env, tmp_path):
        build, _ = _apply_env
        workdir = build(
            {"P01-001": _ti("A Great Paper")}, {"P01-001": "title"}
        )
        md = "<!-- id=P01-001 label=body -->\n一篇好论文\n"
        md_file = tmp_path / "translated.md"
        md_file.write_text(md, encoding="utf-8")
        report = mv.apply_markdown(workdir, md_file)
        assert report["label_mismatches"] == [
            {"id": "P01-001", "expected": "title", "got": "body"}
        ]
        assert any("label_mismatch" in w for w in report["warnings"])

    def test_matching_label_no_warning(self, _apply_env, tmp_path):
        build, _ = _apply_env
        workdir = build({"P01-001": _ti("A Great Paper")}, {"P01-001": "title"})
        md_file = tmp_path / "translated.md"
        md_file.write_text("<!-- id=P01-001 label=title -->\n一篇好论文\n", encoding="utf-8")
        report = mv.apply_markdown(workdir, md_file)
        assert report["label_mismatches"] == []
        assert not any("label_mismatch" in w for w in report["warnings"])

    def test_empty_translation_falls_back_to_source(self, _apply_env, tmp_path):
        build, captured = _apply_env
        workdir = build(
            {"P01-001": _ti("A Great Paper")}, {"P01-001": "title"}
        )
        md_file = tmp_path / "translated.md"
        md_file.write_text("<!-- id=P01-001 label=title -->\n\n", encoding="utf-8")
        report = mv.apply_markdown(workdir, md_file)
        assert report["empty_ids"] == ["P01-001"]
        assert any("empty_translation" in w for w in report["warnings"])
        # 回退原文写进 sheet
        import json

        sheet_line = json.loads(
            (tmp_path / "agent" / "translated.jsonl").read_text(encoding="utf-8").splitlines()[0]
        )
        assert sheet_line == {"id": "P01-001", "target": "A Great Paper"}

    def test_caption_markers_are_stripped_before_writeback(self, _apply_env, tmp_path):
        build, _ = _apply_env
        workdir = build(
            {"P01-001": _ti("Figure 1: Architecture")}, {"P01-001": "figure_caption"}
        )
        md_file = tmp_path / "translated.md"
        md_file.write_text(
            "<!-- id=P01-001 label=figure_caption -->\n**图 1：架构**\n", encoding="utf-8"
        )
        report = mv.apply_markdown(workdir, md_file)
        import json

        sheet_line = json.loads(
            (tmp_path / "agent" / "translated.jsonl").read_text(encoding="utf-8").splitlines()[0]
        )
        assert sheet_line["target"] == "图 1：架构"
        assert report["ok"] is True
