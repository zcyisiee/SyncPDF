"""目录页/条目识别测试（板块 4）。

覆盖：行切分（标题/引导线/页码三段）、页级判定（标题 or 条目占比）、
层级推断（编号模式 + 缩进聚类）、低置信度不改结构、条目化改写
（toc_entry 可翻译 + toc_entry_page 保护）、书签快照。
"""

from __future__ import annotations

import json

from babeldoc.format.pdf.document_il import il_version_1
from babeldoc.format.pdf.document_il.midend.toc_detector import TocEntry
from babeldoc.format.pdf.document_il.midend.toc_detector import build_entry_paragraphs
from babeldoc.format.pdf.document_il.midend.toc_detector import detect_toc_page
from babeldoc.format.pdf.document_il.midend.toc_detector import split_toc_line


def _style():
    return il_version_1.PdfStyle(
        font_id="F1",
        font_size=10.0,
        graphic_state=il_version_1.GraphicState(
            passthrough_per_char_instruction=""
        ),
    )


def _char(text: str, x: float, y: float = 700.0, size: float = 10.0) -> il_version_1.PdfCharacter:
    box = il_version_1.Box(x=x, y=y, x2=x + size * 0.5, y2=y + size)
    return il_version_1.PdfCharacter(
        pdf_style=_style(),
        box=box,
        visual_bbox=il_version_1.VisualBbox(box=box),
        char_unicode=text,
        advance=size * 0.5,
        xobj_id=0,
    )


def _line(chars, y=700.0, x2=520.0):
    return il_version_1.PdfParagraphComposition(
        pdf_line=il_version_1.PdfLine(
            pdf_character=chars,
            box=il_version_1.Box(
                x=min(c.box.x for c in chars),
                y=y,
                x2=x2,
                y2=y + 10,
            ),
        )
    )


def _paragraph(comps, label="text", debug_id="P01-001"):
    return il_version_1.PdfParagraph(
        box=il_version_1.Box(70, 100, 520, 740),
        pdf_style=_style(),
        pdf_paragraph_composition=comps,
        unicode="toc page",
        debug_id=debug_id,
        layout_label=label,
        xobj_id=0,
    )


def _page(paragraphs, page_number=1):
    return il_version_1.Page(
        page_number=page_number,
        pdf_paragraph=paragraphs,
        page_layout=[],
        cropbox=il_version_1.Cropbox(
            box=il_version_1.Box(x=0, y=0, x2=595, y2=841)
        ),
        pdf_character=[],
    )


def _toc_entry_line(title: str, page_label: str, x0=71.0, y=700.0, leader="."):
    """构造一行目录条目字符：标题 + 引导线 + 页码（页码右对齐到 x≈519）。"""
    chars = []
    x = x0
    for ch in title:
        chars.append(_char(ch, x, y))
        x += 3.0
    for _ in range(10):
        chars.append(_char(leader, x, y))
        x += 3.0
    # 页码右缘贴 520
    page_x = 520 - len(page_label) * 3.0
    for ch in page_label:
        chars.append(_char(ch, page_x, y))
        page_x += 3.0
    return chars


class TestSplitTocLine:
    def test_splits_heading_leader_page(self):
        chars = _toc_entry_line("1 Introduction", "4")
        split = split_toc_line(chars, right_margin=520.0)
        assert split is not None
        assert (
            "".join(c.char_unicode for c in split.heading_chars).replace(" ", "")
            == "1Introduction"
        )
        assert (
            "".join(c.char_unicode for c in split.page_chars) == "4"
        )

    def test_rejects_line_without_trailing_page_number(self):
        chars = [_char(c, 71 + i * 3) for i, c in enumerate("plain body text")]
        assert split_toc_line(chars, right_margin=520.0) is None

    def test_rejects_page_number_not_at_right_margin(self):
        # 页码在行中间（远离右边界）→ 不是目录页码
        chars = [_char(c, 71 + i * 3) for i, c in enumerate("see 12 notes here")]
        assert split_toc_line(chars, right_margin=520.0) is None


class TestDetectTocPage:
    def test_contents_title_and_entries(self):
        title = _paragraph(
            [_line([_char(c, 71 + i * 8, 780) for i, c in enumerate("Contents")])],
            label="title",
            debug_id="P01-001",
        )
        entry_lines = []
        for i, (num, label) in enumerate(
            [("1", "Introduction"), ("2", "Architecture"), ("2.1", "Overview"),
             ("2.2", "Details"), ("3", "Results"), ("4", "Conclusion"),
             ("5", "Extras"), ("6", "End")]
        ):
            entry_lines.append(_line(_toc_entry_line(f"{num} {label}", "7", y=100 + i * 15)))
        body = _paragraph(entry_lines, debug_id="P01-002")
        info = detect_toc_page(_page([title, body]))
        assert info.is_toc
        assert len(info.entries) == 8
        assert info.entries[0].level == 1
        assert info.entries[2].level == 2  # 2.1 → level 2
        assert info.entries[0].printed_page_label == "7"
        assert not info.low_confidence

    def test_normal_text_page_not_toc(self):
        comps = [_line([_char(c, 71 + i * 6, 100 + j * 15) for i, c in enumerate("just some ordinary body text here")]) for j in range(10)]
        body = _paragraph(comps)
        info = detect_toc_page(_page([body]))
        assert not info.is_toc
        assert not info.low_confidence


class TestBuildEntryParagraphs:
    def test_entry_becomes_translatable_heading_and_protected_page(self):
        chars = _toc_entry_line("1 Introduction", "4")
        entry = TocEntry(
            entry_index=1,
            heading_text="1 Introduction",
            printed_page_label="4",
            leader_kind="dots",
            indent_x0=71.0,
            level=1,
            heading_chars=[c for c in chars if c.char_unicode not in (".", "4")],
            leader_chars=[c for c in chars if c.char_unicode == "."],
            page_chars=[c for c in chars if c.char_unicode == "4"],
            line_box=il_version_1.Box(71, 700, 520, 710),
        )
        page = _page([])
        paragraphs = build_entry_paragraphs(page, entry, marker_id=99)
        assert len(paragraphs) == 2
        heading, page_para = paragraphs
        assert heading.layout_label == "toc_entry"
        assert heading.unicode == "1 Introduction"
        assert page_para.layout_label == "toc_entry_page"
        # 引导线 + 页码字符全部拿到公式哨兵标记（防降级为普通文本）
        assert all(c.formula_layout_id == 99 for c in page_para.pdf_paragraph_composition[0].pdf_formula.pdf_character)


class TestBookmarksSnapshot:
    def test_snapshot_bookmarks_shape(self, tmp_path):
        # 构造一个带书签的最小 PDF
        import pymupdf
        from babeldoc.tools.agent import link_snapshot

        doc = pymupdf.open()
        doc.new_page()
        doc.new_page()
        doc.set_toc([[1, "Intro", 1], [2, "Details", 2]])
        pdf_path = tmp_path / "bm.pdf"
        doc.save(str(pdf_path))
        doc.close()

        out = tmp_path / "bookmarks.json"
        result = link_snapshot.write_bookmarks(pdf_path, out)
        assert result["count"] == 2
        data = json.loads(out.read_text(encoding="utf-8"))
        assert data[0]["level"] == 1
        assert data[0]["title"] == "Intro"
        assert data[0]["page"] == 1
        assert data[1]["title"] == "Details"
        assert data[1]["page"] == 2


class TestSelectionProtection:
    def test_toc_entry_page_is_protected(self):
        from babeldoc.tools.agent.translation_selection import protected_reason

        assert protected_reason("toc_entry_page") == "toc_entry_page"
        assert protected_reason("toc_entry") is None  # 标题可翻译
