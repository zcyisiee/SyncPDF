"""角标引文链接保真：cornermark 分级渲染 + 印章标记链接矩形 + remap 优先级。

覆盖三块新机制（对应「上标引文还原到右上角」）：

1. ``fusion.classify_formula`` 的 ``cornermark`` 分级：按链接归属切 run、
   颜色/字号/抬升取自链接快照；非角标公式保持 text 行为（旧调用兼容）。
2. ``fusion._render_cornermark``：``\\raisebox``/``\\fontsize``/``\\textcolor``/
   ``\\href{bdoclink://l<N>}`` 的组合输出，括号（无链接 run）不上色不挂链接。
3. ``link_remap.remap_page_links`` 的 stamp 最高优先级 + ``overlay`` 的
   印章注记 → 页面矩形映射。
"""

from __future__ import annotations

import pymupdf
import pytest
from babeldoc.format.pdf.document_il import il_version_1
from babeldoc.format.pdf.document_il.backend.latex_bbox import fusion
from babeldoc.format.pdf.document_il.backend.latex_bbox.overlay import (
    LatexBboxOverlay,
)
from babeldoc.format.pdf.document_il.backend import link_remap

_SAMPLE = None  # lazy


def _sample_pdf():
    global _SAMPLE
    if _SAMPLE is None:
        from pathlib import Path

        path = Path(__file__).parent.parent / "samples" / "main.pdf"
        _SAMPLE = path if path.exists() else False
    return _SAMPLE


requires_sample = pytest.mark.skipif(
    not _sample_pdf(), reason="需要 samples/main.pdf（上标引文样本）"
)


def _style(size: float = 10.9, font_id: str = "F1"):
    return il_version_1.PdfStyle(
        font_id=font_id,
        font_size=size,
        graphic_state=il_version_1.GraphicState(
            passthrough_per_char_instruction=""
        ),
    )


def _corner_formula(native: str = "[1]", size: float = 7.97, y_offset: float = 2.1):
    """上标引文公式：字符 7.97pt、is_corner_mark=True。"""
    chars = [
        il_version_1.PdfCharacter(
            char_unicode=ch,
            box=il_version_1.Box(200 + i * 4, 500, 204 + i * 4, 508),
            pdf_character_id=i,
            pdf_style=_style(size),
            xobj_id=0,
        )
        for i, ch in enumerate(native)
    ]
    formula = il_version_1.PdfFormula(
        box=il_version_1.Box(200, 500, 200 + 4 * len(native), 508),
        x_offset=0.0,
        y_offset=y_offset,
        pdf_character=chars,
    )
    formula.is_corner_mark = True
    return formula


# --------------------------------------------------------------------------- #
# 1. classify_formula：cornermark 分级
# --------------------------------------------------------------------------- #
def test_classify_cornermark_with_link_membership():
    formula = _corner_formula("[1]")
    char_link = {id(formula.pdf_character[1]): 3}
    link_meta = {3: {"color": 0xFF, "font_size": 7.97, "raise_bp": 4.2}}
    result = fusion.classify_formula(
        formula,
        0,
        None,
        char_link=char_link,
        link_meta=link_meta,
        base_font_size=10.9,
    )
    assert result.kind == "cornermark"
    # run 按链接归属切分：'[' 无链接，'1' 挂链接 3，']' 无链接；
    # 括号 run own 字号小、就近继承链接 run 的抬升（同公式上标样式一致）
    assert result.corner_runs == [
        ("[", None, 7.97, 4.2),
        ("1", 3, 7.97, 4.2),
        ("]", None, 7.97, 4.2),
    ]
    assert result.native_text == "[1]"


def test_classify_cornermark_without_link_state_keeps_geometry():
    """无链接状态（high_level 路径）：仍分级 cornermark，只是无链接 run。"""
    result = fusion.classify_formula(
        _corner_formula(), 0, None, base_font_size=10.9
    )
    assert result.kind == "cornermark"
    # 无链接 → 抬升回退公式 y_offset
    assert result.corner_runs == [("[1]", None, 7.97, 2.1)]


def test_classify_covered_formula_without_corner_flag_is_cornermark():
    """空格前导的上标引文（``rectangles [1,2]``）：无角标标志但字符被链接
    覆盖 → 仍按 cornermark 分级，靠链接快照事实还原上标。"""
    formula = _corner_formula("[1,2]")
    formula.is_corner_mark = False
    char_link = {
        id(formula.pdf_character[1]): 1,
        id(formula.pdf_character[3]): 2,
    }
    link_meta = {
        1: {"color": 0xFF, "font_size": 7.97, "raise_bp": 3.96},
        2: {"color": 0xFF, "font_size": 7.97, "raise_bp": 3.96},
    }
    result = fusion.classify_formula(
        formula,
        0,
        None,
        char_link=char_link,
        link_meta=link_meta,
        base_font_size=10.9,
    )
    assert result.kind == "cornermark"
    assert [(r[0], r[1]) for r in result.corner_runs] == [
        ("[", None),
        ("1", 1),
        (",", None),
        ("2", 2),
        ("]", None),
    ]


def test_classify_mixed_formula_keeps_body_part_ambient():
    """粘连混合公式（``A2 in[3]``）：正文号部分平排，仅小号部分上标。"""
    formula = _corner_formula("A2 in[3]")
    sizes = [10.9, 10.9, 10.9, 10.9, 10.9, 7.97, 7.97, 7.97]
    for char, size in zip(formula.pdf_character, sizes, strict=True):
        char.pdf_style = _style(size)
    char_link = {id(formula.pdf_character[6]): 5}  # '3'
    link_meta = {5: {"color": 0xFF, "font_size": 7.97, "raise_bp": 3.96}}
    result = fusion.classify_formula(
        formula,
        0,
        None,
        char_link=char_link,
        link_meta=link_meta,
        base_font_size=10.9,
    )
    assert result.kind == "cornermark"
    by_text = {run[0]: run for run in result.corner_runs}
    # 正文号 run：环境字号平排（不缩不放不抬）
    assert by_text["A2 in"] == ("A2 in", None, None, None)
    # 小号 run：own 字号 + 继承链接抬升；数字挂链接
    assert by_text["["] == ("[", None, 7.97, 3.96)
    assert by_text["3"] == ("3", 5, 7.97, 3.96)
    assert by_text["]"] == ("]", None, 7.97, 3.96)


def test_classify_non_cornermark_stays_text():
    """is_corner_mark=False 且无链接覆盖：保持旧行为 text 分级。"""
    formula = _corner_formula("[62], ")
    formula.is_corner_mark = False
    result = fusion.classify_formula(formula, 0, None, base_font_size=10.9)
    assert result.kind == "text"


def test_classify_legacy_caller_without_flag_or_base_stays_text():
    """旧调用（无 base_font_size、无角标标志、无链接）：保持 text 分级。"""
    formula = _corner_formula("[62], ")
    formula.is_corner_mark = None
    assert fusion.classify_formula(formula, 0, None).kind == "text"


# --------------------------------------------------------------------------- #
# 2. 渲染输出
# --------------------------------------------------------------------------- #
def test_render_cornermark_full_markup():
    classification = fusion.FormulaClassification(
        kind="cornermark",
        corner_runs=[
            ("[", None, 7.97, 4.2),
            ("1", 3, 7.97, 4.2),
            ("]", None, 7.97, 4.2),
        ],
    )
    link_meta = {3: {"color": 0xFF, "font_size": 7.97, "raise_bp": 4.2}}
    body = fusion._render_cornermark(classification, link_meta)
    assert "\\raisebox{4.20bp}" in body
    assert "\\fontsize{7.97bp}" in body
    assert "\\textcolor[RGB]{0,0,255}{" in body
    assert "\\href{bdoclink://l3}{" in body
    # 括号与数字同抬升同字号，但只有数字上色、挂链接
    assert body.count("\\href") == 1
    assert body.count("\\textcolor") == 1


def test_render_cornermark_without_link_meta_is_plain_superscript():
    classification = fusion.FormulaClassification(
        kind="cornermark",
        corner_runs=[("[1]", None, 7.97, 2.1)],
    )
    body = fusion._render_cornermark(classification, None)
    assert "\\raisebox{2.10bp}" in body
    assert "\\fontsize{7.97bp}" in body
    assert "href" not in body
    assert "textcolor" not in body


def test_render_cornermark_ambient_run_has_no_size_or_raise():
    """正文号 run（None/None）：按环境字号平排，无任何包装。"""
    classification = fusion.FormulaClassification(
        kind="cornermark",
        corner_runs=[("A2 in", None, None, None)],
    )
    assert fusion._render_cornermark(classification, None) == "A2 in"


def test_fuse_paragraph_emits_cornermark_body():
    """端到端：译文 ``如文献{v1}所示`` 的 {v1} 是上标引文公式。"""
    formula = _corner_formula("[1]")
    paragraph = il_version_1.PdfParagraph(
        debug_id="P01-001",
        unicode="如文献{v1}所示",
        pdf_style=_style(10.9),
        pdf_paragraph_composition=[
            il_version_1.PdfParagraphComposition(
                pdf_same_style_unicode_characters=(
                    il_version_1.PdfSameStyleUnicodeCharacters(
                        pdf_style=_style(10.9), unicode="如文献"
                    )
                )
            ),
            il_version_1.PdfParagraphComposition(pdf_formula=formula),
            il_version_1.PdfParagraphComposition(
                pdf_same_style_unicode_characters=(
                    il_version_1.PdfSameStyleUnicodeCharacters(
                        pdf_style=_style(10.9), unicode="所示"
                    )
                )
            ),
        ],
    )
    char_link = {id(formula.pdf_character[1]): 7}
    link_meta = {7: {"color": 0xFF, "font_size": 7.97, "raise_bp": 4.2}}
    result = fusion.fuse_paragraph(
        paragraph, 0, {"F1": None}, None, char_link=char_link, link_meta=link_meta
    )
    assert result.ok, result.reasons
    assert "\\href{bdoclink://l7}" in result.body
    assert "\\raisebox{4.20bp}" in result.body
    assert result.plain_text == "如文献[1]所示"


# --------------------------------------------------------------------------- #
# 3. remap stamp 优先级 + overlay 印章注记映射
# --------------------------------------------------------------------------- #
def _page_with_link():
    build = pymupdf.open()
    page = build.new_page(width=600, height=800)
    page.insert_link(
        {
            "kind": pymupdf.LINK_URI,
            "from": pymupdf.Rect(300, 500, 310, 512),
            "uri": "https://example.com/cite",
        }
    )
    payload = build.tobytes()
    build.close()
    # pymupdf 读缓存怪癖：新建未保存文档 get_links() 为空，tobytes 重开才真实。
    doc = pymupdf.open("pdf", payload)
    return doc, doc[0]


def test_remap_prefers_stamp_rect_over_char_union():
    doc, page = _page_with_link()
    try:
        # 字符并集路径会命中的 alive 字符（故意给一个错误位置）
        char = il_version_1.PdfCharacter(
            char_unicode="1",
            box=il_version_1.Box(100, 100, 110, 110),
            pdf_style=_style(),
        )
        entry = {
            "link_index": 3,
            "from": [300, 500, 310, 512],
            "uri": "https://example.com/cite",
            "char_indices": [0],
            "paragraph_ids": [],
        }
        # stamp 矩形（页面坐标）：应优先于字符并集 (100,100)-(110,110)
        result = link_remap.remap_page_links(
            page,
            [entry],
            [char],
            {},
            page.rect.height,
            alive_ids={id(char)},
            stamp_rects={3: [pymupdf.Rect(400, 300, 412, 310)]},
        )
        assert result.remapped == 1
        assert result.stamp_resolved == 1
        assert result.fallback_paragraph == 0
        # insert/delete 之后 pymupdf 读缓存是脏的：tobytes 重开验证
        probe = pymupdf.open("pdf", doc.tobytes())
        try:
            new = [
                link
                for link in probe[0].get_links()
                if link["uri"] == "https://example.com/cite"
            ]
        finally:
            probe.close()
        assert len(new) == 1
        rect = new[0]["from"]
        assert rect.x0 == pytest.approx(400, abs=0.5)
        assert rect.y0 == pytest.approx(300, abs=0.5)
    finally:
        doc.close()


def test_remap_without_stamp_falls_back_to_char_union():
    doc, page = _page_with_link()
    try:
        char = il_version_1.PdfCharacter(
            char_unicode="1",
            box=il_version_1.Box(100, 700, 110, 710),
            pdf_style=_style(),
        )
        entry = {
            "link_index": 3,
            "from": [300, 500, 310, 512],
            "uri": "https://example.com/cite",
            "char_indices": [0],
            "paragraph_ids": [],
        }
        result = link_remap.remap_page_links(
            page, [entry], [char], {}, page.rect.height, alive_ids={id(char)}
        )
        assert result.remapped == 1
        assert result.stamp_resolved == 0
        # insert/delete 之后 pymupdf 读缓存是脏的：tobytes 重开验证
        probe = pymupdf.open("pdf", doc.tobytes())
        try:
            new = [
                link
                for link in probe[0].get_links()
                if link["uri"] == "https://example.com/cite"
            ]
        finally:
            probe.close()
        assert len(new) == 1
        rect = new[0]["from"]
        assert rect.x0 == pytest.approx(100, abs=0.5)
        assert rect.y0 == pytest.approx(800 - 710, abs=0.5)
    finally:
        doc.close()


def test_overlay_collect_stamp_links_maps_rect():
    """印章内 bdoclink 注记 → 页面坐标矩形（1:1 仿射）。"""
    stamp = pymupdf.open()
    stamp_page = stamp.new_page(width=200, height=50)
    stamp_page.insert_link(
        {
            "kind": pymupdf.LINK_URI,
            "from": pymupdf.Rect(120, 10, 130, 20),
            "uri": "bdoclink://l5",
        }
    )
    # pymupdf 读缓存怪癖：新建未保存文档 get_links 可能为空，tobytes 重开后真实
    stamp_bytes = stamp.tobytes()
    stamp.close()

    page_doc, _ = _page_with_link()
    try:
        from types import SimpleNamespace

        overlay = LatexBboxOverlay(
            page_doc, None, SimpleNamespace(latex_bbox_mode="full")
        )
        job = {"rect": pymupdf.Rect(60, 100, 260, 150)}
        stamp_doc = pymupdf.open("pdf", stamp_bytes)
        try:
            found = overlay._collect_stamp_links(stamp_doc, 2, job)
        finally:
            stamp_doc.close()
        assert found == 1
        rects = overlay.stamp_link_rects[2][5]
        assert len(rects) == 1
        # 仿射：stamp (120,10) → job.x0 + 120*1, job.y0 + 10*1
        assert rects[0].x0 == pytest.approx(60 + 120, abs=0.5)
        assert rects[0].y0 == pytest.approx(100 + 10, abs=0.5)
        assert rects[0].x1 == pytest.approx(60 + 130, abs=0.5)
    finally:
        page_doc.close()


# --------------------------------------------------------------------------- #
# 4. 链接快照的源样式事实（真实样本）
# --------------------------------------------------------------------------- #
@requires_sample
def test_snapshot_links_records_superscript_facts():
    from babeldoc.tools.agent import link_snapshot

    snapshot = link_snapshot.snapshot_links(_sample_pdf())
    cite_entries = [
        entry
        for entries in snapshot.values()
        for entry in entries
        if entry["kind"] == "NAMED" or entry.get("page") is not None
    ]
    # 样本含 31 条引文内跳（cite.*）：快照应记录出蓝色小字上标的事实
    blue_superscript = [
        e
        for e in cite_entries
        if e.get("color") == 0xFF and e.get("font_size") and e["font_size"] < 9
    ]
    assert blue_superscript, "上标引文应记录到蓝色 + 小字号"
    assert all(
        e.get("raise_bp") is not None and e["raise_bp"] > 1.0
        for e in blue_superscript
    ), "上标引文应记录到正的基线抬升"


# --------------------------------------------------------------------------- #
# 5. 行内正文链接：styled run 按样式对象身份挂标记链接
# --------------------------------------------------------------------------- #
def _translated_paragraph():
    """md-apply 后的译文段落形态：composition 只剩文本 run + 段落 unicode
    带 <style> 标记（公式段另算）。"""
    body_style = _style(10.9)
    link_style = _style(10.9, font_id="F9")
    return il_version_1.PdfParagraph(
        debug_id="P01-010",
        unicode="约束解码见第 <style id='7'>3.1</style> 节。",
        pdf_style=body_style,
        pdf_paragraph_composition=[
            il_version_1.PdfParagraphComposition(
                pdf_same_style_unicode_characters=(
                    il_version_1.PdfSameStyleUnicodeCharacters(
                        pdf_style=body_style, unicode="约束解码见第 "
                    )
                )
            ),
            il_version_1.PdfParagraphComposition(
                pdf_same_style_unicode_characters=(
                    il_version_1.PdfSameStyleUnicodeCharacters(
                        pdf_style=link_style, unicode="3.1"
                    )
                )
            ),
            il_version_1.PdfParagraphComposition(
                pdf_same_style_unicode_characters=(
                    il_version_1.PdfSameStyleUnicodeCharacters(
                        pdf_style=body_style, unicode=" 节。"
                    )
                )
            ),
        ],
    )


def test_fuse_paragraph_marks_inline_styled_link():
    paragraph = _translated_paragraph()
    link_style = (
        paragraph.pdf_paragraph_composition[1]
        .pdf_same_style_unicode_characters.pdf_style
    )
    result = fusion.fuse_paragraph(
        paragraph,
        0,
        {"F1": None},
        None,
        style_link={id(link_style): 3},
        link_meta={3: {"color": 0xFF, "font_size": 10.9, "raise_bp": 0.0}},
    )
    assert result.ok, result.reasons
    assert "\\href{bdoclink://l3}{" in result.body
    assert "\\textcolor[RGB]{0,0,255}{" in result.body
    assert result.plain_text == "约束解码见第 3.1 节。"


def test_fuse_paragraph_without_style_link_leaves_styled_plain():
    result = fusion.fuse_paragraph(
        _translated_paragraph(), 0, {"F1": None}, None
    )
    assert result.ok, result.reasons
    assert "href" not in result.body
    assert "textcolor" not in result.body
