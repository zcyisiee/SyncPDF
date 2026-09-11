"""段落标记（`<!-- id=... label=... -->`）解析与渲染的往返一致性。

背景（真实回归）：native 布局的 layout_label 是 `"plain text"`（含空格），如果
标记正则的 label 字符集不允许空格，该段落标记会被整体跳过 → 段落被并入上一段 →
apply 判定"漏行"→ 回退原文（本次隔离测试中 21/32 段踩到）。
"""

from __future__ import annotations

from babeldoc.tools.agent import markdown_view as mv


def test_parse_label_with_space():
    md = "<!-- id=P01-003 label=plain text -->\n模型合并将多个模型整合。\n"
    parsed = mv.parse_translated_markdown(md)
    assert list(parsed) == ["P01-003"]
    body, label = parsed["P01-003"]
    assert label == "plain text"
    assert "模型合并" in body


def test_parse_label_with_slash_and_without_label():
    md = (
        "<!-- id=P01-001 label=figure/table -->\n甲\n"
        "<!-- id=P01-002 -->\n乙\n"
    )
    parsed = mv.parse_translated_markdown(md)
    assert list(parsed) == ["P01-001", "P01-002"]
    assert parsed["P01-001"][1] == "figure/table"
    assert parsed["P01-002"][1] == ""
    assert parsed["P01-002"][0].strip() == "乙"


def test_render_then_parse_round_trip():
    """渲染出的标记必须能被自己解析回来（含空格标签会被规范化成 token）。"""
    rows = [
        {"id": "P01-001", "layout_label": "plain text", "markdown": "正文甲"},
        {"id": "P01-002", "layout_label": "figure_caption", "markdown": "*图注*"},
        {"id": "P01-003", "layout_label": None, "markdown": "正文丙"},
    ]
    md = mv.render_rows_markdown(rows)
    parsed = mv.parse_translated_markdown(md)
    assert list(parsed) == ["P01-001", "P01-002", "P01-003"]
    assert parsed["P01-001"][1] == "plain_text"  # token 规范化
    assert parsed["P01-002"][1] == "figure_caption"
    assert parsed["P01-003"][1] == "text"
    assert "正文甲" in parsed["P01-001"][0]


def test_label_token_sanitises():
    assert mv._label_token("plain text") == "plain_text"
    assert mv._label_token("figure/table") == "figure_table"
    assert mv._label_token(None) == "text"
    assert mv._label_token("   ") == "text"


def test_header_comment_is_not_treated_as_paragraph_mark():
    """文件头注释不能被当成段落标记（否则每段都会错位）。"""
    md = mv.MD_HEADER + "\n<!-- id=P01-001 label=text -->\n甲\n"
    parsed = mv.parse_translated_markdown(md)
    assert list(parsed) == ["P01-001"]


def _ns(**kwargs):
    from types import SimpleNamespace

    return SimpleNamespace(**kwargs)


def test_selected_rows_markdown_omits_protected_paragraphs():
    """Markdown 渲染只应包含可翻译段落（受保护段落不进入 document.md）。

    用轻量 fake 对象走一遍“选择 → 渲染”链路，不调用任何 LLM / ONNX。
    """
    from babeldoc.tools.agent.translation_selection import (
        SelectionContext,
        select_page_paragraphs,
    )

    def box(x, y, x2, y2):
        return _ns(x=x, y=y, x2=x2, y2=y2)

    paragraphs = [
        _ns(unicode="A Great Paper", layout_label="title",
            box=box(72, 700, 500, 730), debug_id="P01-001"),
        _ns(unicode="Jane Doe, John Smith", layout_label="plain text",
            box=box(72, 660, 400, 680), debug_id="P01-002"),
        _ns(unicode="Abstract: body text.", layout_label="plain text",
            box=box(72, 600, 500, 650), debug_id="P01-003"),
        _ns(unicode="REFERENCES", layout_label="paragraph_title",
            box=box(72, 200, 500, 220), debug_id="P01-004"),
        _ns(unicode="Smith et al. 2020.", layout_label="fallback_line",
            box=box(72, 170, 500, 190), debug_id="P01-005"),
    ]
    page = _ns(
        pdf_paragraph=paragraphs,
        page_number=0,
        page_layout=[],
        cropbox=None,
    )
    context = SelectionContext()
    rows = []
    skipped = []
    for paragraph, decision in select_page_paragraphs(page, context):
        if not decision.translate:
            skipped.append((paragraph.debug_id, decision.reason))
            continue
        rows.append(
            {
                "id": paragraph.debug_id,
                "layout_label": paragraph.layout_label,
                "markdown": mv.canonical_to_markdown(paragraph.unicode),
            }
        )
    md = mv.render_rows_markdown(rows)
    assert "A Great Paper" in md
    assert "Abstract: body text." in md
    assert "Jane Doe" not in md
    assert "REFERENCES" not in md
    assert "Smith et al." not in md
    assert "P01-002" not in md and "P01-004" not in md and "P01-005" not in md
    assert [pid for pid, _ in skipped] == ["P01-002", "P01-004", "P01-005"]
