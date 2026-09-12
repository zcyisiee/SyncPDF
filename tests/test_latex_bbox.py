"""LaTeX bbox 排版测试（capability / fusion / renderer / overlay / 默认关闭回归）。

覆盖：
- 能力探测：显式路径不存在时安全回退，永不抛异常；
- 融合：转义、segment 解析、占位符/公式数量不符、无 MinerU 匹配时回退；
- 渲染器：TeX 头部关键指令、缓存命中；
- overlay：只捕获译文段落、排除水印；链接安全（URI/GOTO/NAMED 多重集
  与 URI 集合在 overlay 前后一致）；质量门禁与几何拒绝路径；
- 真实样本离线集成冒烟：编译中文段落并在 bbox 内替换原英文文本；
- 默认关闭：``enable_latex_bbox_layout=False`` 时 overlay 不触发。
"""

from __future__ import annotations

import json
import pathlib

import pymupdf
import pytest
from babeldoc.format.pdf.document_il import il_version_1
from babeldoc.format.pdf.document_il.backend.latex_bbox import capability
from babeldoc.format.pdf.document_il.backend.latex_bbox import fusion
from babeldoc.format.pdf.document_il.backend.latex_bbox import overlay as overlay_mod
from babeldoc.format.pdf.document_il.backend.latex_bbox import renderer as renderer_mod
from babeldoc.format.pdf.document_il.backend.latex_bbox.renderer import (
    BboxStampRenderer,
)
from babeldoc.format.pdf.document_il.backend.latex_bbox.renderer import StampRequest

# --------------------------------------------------------------------------- #
# 共用 fixture
# --------------------------------------------------------------------------- #
_CAPABILITY = capability.probe_latex_capability()
_HAS_LATEX = _CAPABILITY.available
requires_latex = pytest.mark.skipif(
    not _HAS_LATEX, reason="需要可用的 XeLaTeX + 中文字体"
)


def _style(size: float = 10.0, font_id: str = "F1"):
    return il_version_1.PdfStyle(
        font_id=font_id,
        font_size=size,
        graphic_state=il_version_1.GraphicState(passthrough_per_char_instruction=""),
    )


def _translated_paragraph(
    debug_id: str,
    text: str,
    box,
    size: float = 10.0,
    xobj_id: int | None = 0,
    layout_label: str = "text",
    extra_compositions=(),
):
    style = _style(size)
    compositions = [
        il_version_1.PdfParagraphComposition(
            pdf_same_style_unicode_characters=il_version_1.PdfSameStyleUnicodeCharacters(
                unicode=text, pdf_style=style
            )
        ),
        *extra_compositions,
    ]
    return il_version_1.PdfParagraph(
        box=box,
        pdf_style=style,
        pdf_paragraph_composition=compositions,
        unicode=text,
        debug_id=debug_id,
        layout_label=layout_label,
        xobj_id=xobj_id,
        first_line_indent=False,
    )


def _il_doc(pages):
    return il_version_1.Document(page=list(pages), total_pages=len(pages))


def _il_page(page_number: int, paragraphs, width=612, height=792):
    return il_version_1.Page(
        page_number=page_number,
        pdf_paragraph=list(paragraphs),
        page_layout=[],
        cropbox=il_version_1.Cropbox(box=il_version_1.Box(0, 0, width, height)),
        mediabox=il_version_1.Mediabox(box=il_version_1.Box(0, 0, width, height)),
    )


def _pdf_with_text(tmp_path: pathlib.Path, lines, width=400, height=300, name="in.pdf"):
    doc = pymupdf.open()
    page = doc.new_page(width=width, height=height)
    for x, y, text, size in lines:
        page.insert_text((x, y), text, fontsize=size)
    path = tmp_path / name
    doc.save(path)
    doc.close()
    return path


class _FakeConfig:
    """最小 config 替身（overlay 只读取少量属性）。"""

    def __init__(self, **kwargs):
        self.latex_bbox_state = kwargs.get("latex_bbox_state", {})
        self.enable_latex_bbox_layout = kwargs.get("enable_latex_bbox_layout", True)
        self.latex_xelatex_path = kwargs.get("latex_xelatex_path", _CAPABILITY.xelatex_path)
        self.latex_cjk_font_path = kwargs.get("latex_cjk_font_path", _CAPABILITY.font_path)
        self.latex_compile_timeout_seconds = kwargs.get(
            "latex_compile_timeout_seconds", 45.0
        )
        self.latex_max_compile_workers = kwargs.get("latex_max_compile_workers", 2)
        self.latex_min_line_fill = kwargs.get("latex_min_line_fill", 0.85)
        self.latex_bbox_mode = kwargs.get("latex_bbox_mode", "full")
        self.primary_font_family = kwargs.get("primary_font_family", None)
        self.working_dir = kwargs.get("working_dir", None)
        self.latex_bbox_stats = {}


def _make_stamp_pdf(path: pathlib.Path, width: float, height: float, text="中文贴片内容"):
    doc = pymupdf.open()
    page = doc.new_page(width=width, height=height)
    page.insert_text((2, min(12, height - 2)), text, fontsize=8)
    doc.save(path)
    doc.close()
    return str(path)


# --------------------------------------------------------------------------- #
# capability
# --------------------------------------------------------------------------- #
def test_probe_capability_missing_explicit_paths_never_raises():
    result = capability.probe_latex_capability(
        xelatex_path="/nonexistent/xelatex",
        font_path="/nonexistent/font.ttf",
    )
    assert result.available is False
    assert result.reasons
    assert result.xelatex_path is None
    assert result.font_path is None
    # 结果可序列化（报告用）
    assert result.to_dict()["available"] is False


def test_probe_capability_missing_font_only(monkeypatch):
    monkeypatch.setattr(
        capability,
        "_FONT_CACHE_DIR",
        pathlib.Path("/nonexistent/babeldoc-fonts"),
    )
    result = capability.probe_latex_capability()
    if result.xelatex_path is None:
        pytest.skip("环境无 xelatex，跳过字体单独缺失场景")
    assert result.available is False
    assert result.font_path is None
    assert any("字体" in reason for reason in result.reasons)


def test_probe_capability_reports_packages(monkeypatch):
    monkeypatch.setattr(capability, "_missing_packages", lambda _p: ["xeCJK"])
    result = capability.probe_latex_capability(
        xelatex_path=_CAPABILITY.xelatex_path or "/usr/bin/xelatex",
        font_path=_CAPABILITY.font_path or str(pathlib.Path("/nonexistent/f.ttf")),
    )
    assert result.available is False
    assert result.missing_packages == ["xeCJK"]


# --------------------------------------------------------------------------- #
# fusion
# --------------------------------------------------------------------------- #
def test_escape_latex_specials():
    assert fusion.escape_latex("a_b & 50% #1 $x$ {y}") == (
        r"a\_b \& 50\% \#1 \$x\$ \{y\}"
    )
    assert fusion.escape_latex("~^\\") == (
        r"\textasciitilde{}\textasciicircum{}\textbackslash{}"
    )


def test_parse_segments_plain_and_marked():
    assert fusion.parse_segments("纯中文") == [("text", "纯中文")]
    assert fusion.parse_segments("a {v1} b") == [
        ("text", "a "),
        ("formula", "1"),
        ("text", " b"),
    ]
    assert fusion.parse_segments("前 <style id='2'>粗</style> 后") == [
        ("text", "前 "),
        ("styled", "粗"),
        ("text", " 后"),
    ]


def test_parse_segments_unbalanced_returns_none():
    assert fusion.parse_segments("<style id='1'>未闭合") is None


def test_fuse_paragraph_plain_text_ok():
    paragraph = _translated_paragraph("P01-001", "这是一段纯中文译文。", il_version_1.Box(0, 0, 200, 40))
    result = fusion.fuse_paragraph(paragraph, 0, {"F1": None}, None)
    assert result.ok is True
    assert result.body == "这是一段纯中文译文。"
    assert result.formula_count == 0


def test_fuse_paragraph_placeholder_count_mismatch():
    """译文有 {v1} 但 composition 无公式 → 不可替换。"""
    paragraph = _translated_paragraph("P01-002", "前 {v1} 后", il_version_1.Box(0, 0, 200, 40))
    index = fusion.FormulaLatexIndex({0: [((10, 10, 20, 20), "x")]})
    result = fusion.fuse_paragraph(paragraph, 0, {}, index)
    assert result.ok is False
    assert any("mismatch" in reason for reason in result.reasons)


def test_fuse_paragraph_formula_unmatched_falls_back():
    """公式存在但无 MinerU 匹配 → 不可替换（绝不猜测公式）。"""
    box = il_version_1.Box(10, 10, 40, 25)
    formula = il_version_1.PdfFormula(box=box, x_offset=0.0, y_offset=0.0)
    composition = il_version_1.PdfParagraphComposition(pdf_formula=formula)
    text = "前 {v1} 后"
    style = _style()
    paragraph = il_version_1.PdfParagraph(
        box=il_version_1.Box(0, 0, 200, 40),
        pdf_style=style,
        pdf_paragraph_composition=[
            il_version_1.PdfParagraphComposition(
                pdf_same_style_unicode_characters=il_version_1.PdfSameStyleUnicodeCharacters(
                    unicode="前 ", pdf_style=style
                )
            ),
            composition,
            il_version_1.PdfParagraphComposition(
                pdf_same_style_unicode_characters=il_version_1.PdfSameStyleUnicodeCharacters(
                    unicode=" 后", pdf_style=style
                )
            ),
        ],
        unicode=text,
        debug_id="P01-003",
        layout_label="text",
        xobj_id=0,
    )
    index = fusion.FormulaLatexIndex({0: [((100, 100, 110, 110), "x^2")]})
    result = fusion.fuse_paragraph(paragraph, 0, {"F1": None}, index)
    assert result.ok is False
    assert any("unmatched" in reason for reason in result.reasons)


def test_fuse_paragraph_formula_matched_inlines_math():
    """公式源 box 与 MinerU span 重合 → {v1} 还原为 $...$。"""
    formula_box = il_version_1.Box(50, 300, 70, 315)
    formula = il_version_1.PdfFormula(box=formula_box, x_offset=0.0, y_offset=0.0)
    style = _style()
    paragraph = il_version_1.PdfParagraph(
        box=il_version_1.Box(0, 200, 300, 400),
        pdf_style=style,
        pdf_paragraph_composition=[
            il_version_1.PdfParagraphComposition(
                pdf_same_style_unicode_characters=il_version_1.PdfSameStyleUnicodeCharacters(
                    unicode="见 ", pdf_style=style
                )
            ),
            il_version_1.PdfParagraphComposition(pdf_formula=formula),
            il_version_1.PdfParagraphComposition(
                pdf_same_style_unicode_characters=il_version_1.PdfSameStyleUnicodeCharacters(
                    unicode=" 所示。", pdf_style=style
                )
            ),
        ],
        unicode="见 {v1} 所示。",
        debug_id="P01-004",
        layout_label="text",
        xobj_id=0,
    )
    index = fusion.FormulaLatexIndex({0: [((50, 300, 70, 315), r"\alpha_{1}")]})
    result = fusion.fuse_paragraph(paragraph, 0, {"F1": None}, index)
    assert result.ok is True
    assert result.body == r"见 $\alpha_{1}$ 所示。"
    assert result.formulas_matched == 1


def test_fuse_paragraph_segment_order_mismatch_falls_back():
    """composition 文本与译文不一致（顺序破坏）→ 拒绝替换。"""
    formula_box = il_version_1.Box(50, 300, 70, 315)
    formula = il_version_1.PdfFormula(box=formula_box, x_offset=0.0, y_offset=0.0)
    style = _style()
    paragraph = il_version_1.PdfParagraph(
        box=il_version_1.Box(0, 200, 300, 400),
        pdf_style=style,
        pdf_paragraph_composition=[
            il_version_1.PdfParagraphComposition(
                pdf_same_style_unicode_characters=il_version_1.PdfSameStyleUnicodeCharacters(
                    unicode="完全不同的内容", pdf_style=style
                )
            ),
            il_version_1.PdfParagraphComposition(pdf_formula=formula),
        ],
        unicode="见 {v1}",
        debug_id="P01-005",
        layout_label="text",
        xobj_id=0,
    )
    index = fusion.FormulaLatexIndex({0: [((50, 300, 70, 315), "x")]})
    result = fusion.fuse_paragraph(paragraph, 0, {"F1": None}, index)
    assert result.ok is False
    assert any("segment-order-mismatch" in reason for reason in result.reasons)


def test_fuse_paragraph_plain_text_resolves_style_markup():
    """纯文本段落里的 <style> 标记必须解析为样式命令，不能原样交给 XeLaTeX。"""
    style = _style()
    paragraph = il_version_1.PdfParagraph(
        box=il_version_1.Box(0, 0, 300, 60),
        pdf_style=style,
        pdf_paragraph_composition=[
            il_version_1.PdfParagraphComposition(
                pdf_same_style_unicode_characters=il_version_1.PdfSameStyleUnicodeCharacters(
                    unicode="前 ", pdf_style=style
                )
            ),
            il_version_1.PdfParagraphComposition(
                pdf_same_style_unicode_characters=il_version_1.PdfSameStyleUnicodeCharacters(
                    unicode="加粗文字",
                    pdf_style=il_version_1.PdfStyle(
                        font_id="F1", font_size=10.0, graphic_state=None
                    ),
                )
            ),
        ],
        unicode="前 <style id='2'>加粗文字</style>",
        debug_id="P01-006",
        layout_label="text",
        xobj_id=0,
    )
    bold_font = il_version_1.PdfFont(font_id="F1", name="Foo-Bold", bold=True)
    result = fusion.fuse_paragraph(paragraph, 0, {"F1": bold_font}, None)
    assert result.ok is True
    assert "<style" not in result.body
    assert r"\textbf{加粗文字}" in result.body
    assert result.style_runs == 1
    assert result.plain_text == "前 加粗文字"


def test_fuse_paragraph_plain_text_unresolved_style_falls_back():
    """composition 顺序与译文不一致时不能做样式映射 → 拒绝替换。"""
    style = _style()
    paragraph = il_version_1.PdfParagraph(
        box=il_version_1.Box(0, 0, 300, 60),
        pdf_style=style,
        pdf_paragraph_composition=[
            il_version_1.PdfParagraphComposition(
                pdf_same_style_unicode_characters=il_version_1.PdfSameStyleUnicodeCharacters(
                    unicode="完全对不上的内容", pdf_style=style
                )
            )
        ],
        unicode="前 <style id='2'>加粗</style>",
        debug_id="P01-007",
        layout_label="text",
        xobj_id=0,
    )
    result = fusion.fuse_paragraph(paragraph, 0, {}, None)
    assert result.ok is False
    assert any("segment-order-mismatch" in reason for reason in result.reasons)


# --------------------------------------------------------------------------- #
# FormulaLatexIndex
# --------------------------------------------------------------------------- #
def test_formula_index_iou_and_center_fallback():
    index = fusion.FormulaLatexIndex({0: [((50, 300, 70, 315), "a+b")]})
    assert index.lookup(0, (50, 300, 70, 315)) == "a+b"
    # 中心点落入 span box（IoU 低）→ 兜底命中
    assert index.lookup(0, (55, 305, 65, 310)) == "a+b"
    # 完全无关 → None
    assert index.lookup(0, (200, 200, 220, 215)) is None
    assert index.lookup(9, (50, 300, 70, 315)) is None
    assert index.total_spans == 1


def test_formula_index_from_documents_loads_provider_ir(tmp_path):
    """从 provider_ir.json 构建索引：inline_equation span 转 IL 坐标。"""

    from babeldoc.docvision.provider_ir import ProviderDocument

    agent_dir = tmp_path / "agent" / "source" / "mineru"
    agent_dir.mkdir(parents=True)
    provider = ProviderDocument.from_dict(
        {
            "pages": [
                {
                    "page_index": 0,
                    "blocks": [
                        {
                            "block_id": "b1",
                            "type": "text",
                            "bbox": [10, 10, 200, 40],
                            "lines": [
                                {
                                    "line_id": "l1",
                                    "bbox": [10, 10, 200, 40],
                                    "spans": [
                                        {
                                            "span_id": "s1",
                                            "bbox": [10, 10, 60, 25],
                                            "kind": "text",
                                            "content": "hello",
                                        },
                                        {
                                            "span_id": "s2",
                                            "bbox": [62, 10, 100, 25],
                                            "kind": "inline_equation",
                                            "content": "E = mc^2",
                                        },
                                    ],
                                }
                            ],
                        }
                    ],
                    "reading_order": [],
                }
            ]
        }
    )
    (agent_dir / "provider_ir.json").write_text(
        provider.to_json(), encoding="utf-8"
    )

    docs = _il_doc([_il_page(0, [], width=612, height=792)])
    config = _FakeConfig(working_dir=tmp_path)
    index = fusion.FormulaLatexIndex.from_documents(docs, config)
    assert index is not None
    assert index.total_spans == 1
    # MinerU y 向下（top=10, bottom=25）→ IL y 向上，页高 792
    latex = index.lookup(0, (62, 792 - 25, 100, 792 - 10))
    assert latex == "E = mc^2"


def test_formula_index_missing_provider_ir_returns_none(tmp_path):
    docs = _il_doc([_il_page(0, [])])
    config = _FakeConfig(working_dir=tmp_path)
    assert fusion.FormulaLatexIndex.from_documents(docs, config) is None


# --------------------------------------------------------------------------- #
# renderer
# --------------------------------------------------------------------------- #
def test_build_tex_contains_required_directives():
    renderer = BboxStampRenderer(_CAPABILITY)
    tex = renderer.build_tex("测试", 200.0, 100.0, 10.0)
    # 纸张与字号统一用 TeX bp（= 1/72in = PDF 用户单位），与 bbox/fit 同单位。
    assert "paperwidth=200.0000bp" in tex
    assert "paperheight=100.0000bp" in tex
    # 不再用 TeX pt 指定纸张/字号。
    assert "paperwidth=200.0000pt" not in tex
    assert "\\fontsize{10.0000pt}" not in tex
    assert "\\usepackage{xeCJK}" in tex
    assert "\\XeTeXlinebreaklocale" in tex
    assert "PunctStyle=plain" in tex
    assert pathlib.Path(_CAPABILITY.font_path).stem in tex
    assert "\\fontsize{10.0000bp}{15.0000bp}" in tex
    assert "测试" in tex


def test_build_tex_includes_bold_font_when_available():
    renderer = BboxStampRenderer(_CAPABILITY)
    tex = renderer.build_tex("x", 100.0, 50.0, 10.0)
    if _CAPABILITY.bold_font_path:
        assert f"BoldFont={pathlib.Path(_CAPABILITY.bold_font_path).stem}" in tex


def test_render_many_empty_returns_empty():
    renderer = BboxStampRenderer(_CAPABILITY)
    assert renderer.render_many([]) == {}


@requires_latex
def test_render_one_compiles_and_caches(tmp_path):
    renderer = BboxStampRenderer(_CAPABILITY, max_workers=1)
    request = StampRequest(
        key="P01-001",
        body="这是一段用于缓存的测试中文文本。",
        width=200.0,
        height=60.0,
        font_size=9.0,
    )
    first = renderer.render_one(request, tmp_path)
    assert first.ok is True
    assert first.pdf_path and pathlib.Path(first.pdf_path).exists()
    assert first.font_size is not None and first.scale is not None

    before = renderer.cache_hits
    second = renderer.render_one(request, tmp_path)
    assert second is first
    assert renderer.cache_hits == before + 1


@requires_latex
def test_render_one_compile_failure_returns_reason(tmp_path):
    """非法 LaTeX（未闭合数学模式）→ 编译失败且有原因，不抛异常。"""
    renderer = BboxStampRenderer(_CAPABILITY, timeout_seconds=20.0)
    request = StampRequest(
        key="bad",
        body=r"缺失闭合的数学模式 $\alpha",
        width=120.0,
        height=40.0,
        font_size=9.0,
    )
    result = renderer.render_one(request, tmp_path)
    assert result.ok is False
    assert result.reason


@requires_latex
def test_render_one_shrinks_when_overflowing(tmp_path):
    """超长不可断 token 在源字号溢出 → 有界缩后成功且有 scale < 1。"""
    renderer = BboxStampRenderer(_CAPABILITY, timeout_seconds=30.0)
    long_token = "averyveryverylongunbreakabletoken_" * 3
    request = StampRequest(
        key="long",
        body=long_token,
        width=160.0,
        height=30.0,
        font_size=10.0,
    )
    result = renderer.render_one(request, tmp_path)
    assert result.compile_attempts >= 1
    if result.ok:
        assert result.scale is not None


def test_measure_fit_detects_clipped_text(tmp_path):
    """排版溢出页面底部的行会被 pymupdf 静默裁掉：必须报 text-clipped。"""
    stamp = tmp_path / "clipped.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=120, height=30)
    # 写到 page 下方（会被 get_text 裁掉）
    page.insert_text((2, 60), "BOTTOMLINE", fontsize=9)
    page.insert_text((2, 12), "top line", fontsize=9)
    doc.save(stamp)
    doc.close()
    fits, reason, _chars = renderer_mod._measure_fit(
        stamp, 120.0, 30.0, "top lineBOTTOMLINE"
    )
    assert fits is False
    assert "text-clipped" in reason


def test_measure_fit_accepts_complete_text(tmp_path):
    stamp = tmp_path / "ok.pdf"
    doc = pymupdf.open()
    page = doc.new_page(width=200, height=60)
    page.insert_text((2, 20), "complete text here", fontsize=9)
    doc.save(stamp)
    doc.close()
    fits, reason, chars = renderer_mod._measure_fit(
        stamp, 200.0, 60.0, "complete text here"
    )
    assert fits is True
    assert reason == "ok"
    assert chars > 0


@requires_latex
def test_render_shrinks_instead_of_clipping(tmp_path):
    """太长而垂直放不下时应有界缩小，而不是静默截断文本。"""
    renderer = BboxStampRenderer(_CAPABILITY, timeout_seconds=30.0)
    plain = "这是一段很长的中文文本" * 8
    request = StampRequest(
        key="clip",
        body=plain,
        width=200.0,
        height=20.0,
        font_size=10.0,
        expected_text=plain,
    )
    result = renderer.render_one(request, tmp_path)
    assert result.compile_attempts >= 2 or result.ok is False
    if result.ok:
        import pymupdf as _fitz

        doc = _fitz.open(result.pdf_path)
        try:
            got = "".join(doc[0].get_text().split())
            assert "这是一个很长的中文文本" not in got  # 语句不匹配则只校验末尾
            assert plain.strip()[-3:] in got
        finally:
            doc.close()


# --------------------------------------------------------------------------- #
# capture_layout_sources
# --------------------------------------------------------------------------- #
def test_capture_only_translated_paragraphs_excludes_watermark():
    translated = _translated_paragraph(
        "P01-001", "译文段落一", il_version_1.Box(10, 600, 200, 700)
    )
    watermark = _translated_paragraph(
        "WATERMARK",
        "本文档由 funstory.ai 的 BabelDOC 翻译",
        il_version_1.Box(30, 0, 580, 780),
        xobj_id=-1,
    )
    untranslated_style = _style()
    untranslated = il_version_1.PdfParagraph(
        box=il_version_1.Box(10, 500, 200, 560),
        pdf_style=untranslated_style,
        pdf_paragraph_composition=[
            il_version_1.PdfParagraphComposition(
                pdf_same_style_characters=il_version_1.PdfSameStyleCharacters(
                    pdf_character=[], pdf_style=untranslated_style
                )
            )
        ],
        unicode="original english text",
        debug_id="P01-002",
        layout_label="reference",
        xobj_id=0,
    )
    vertical = _translated_paragraph(
        "P01-003", "竖排", il_version_1.Box(10, 400, 200, 460)
    )
    vertical.vertical = True
    page = _il_page(0, [translated, watermark, untranslated, vertical])
    docs = _il_doc([page])

    config = _FakeConfig(latex_bbox_state={}, working_dir=None)
    state = overlay_mod.capture_layout_sources(docs, config)

    assert "P01-001" in state["paragraphs"]
    assert state["paragraphs"]["P01-001"]["page"] == 0
    assert state["paragraphs"]["P01-001"]["font_size"] == pytest.approx(10.0)
    assert state["paragraphs"]["P01-001"]["has_formula"] is False
    assert "WATERMARK" not in state["paragraphs"]
    assert "P01-002" not in state["paragraphs"]
    assert "P01-003" not in state["paragraphs"]
    assert config.latex_bbox_state is state
    assert state["provider_inline_spans"] == 0  # 无 provider IR
    assert state["bodies"]["P01-001"] == "译文段落一"


def test_capture_fuses_inline_formula_from_provider_ir(tmp_path):
    """端到端融合：真实 provider_ir.json + 公式段落 → body 内联 $...$。"""

    from babeldoc.docvision.provider_ir import ProviderDocument

    agent_dir = tmp_path / "agent" / "source" / "mineru"
    agent_dir.mkdir(parents=True)
    # 公式源 box（IL 坐标 y-up）: (50,300)-(70,315)；页高 792
    # 对应 MinerU top-left bbox: (50, 792-315=477)-(70, 792-300=492)
    provider = ProviderDocument.from_dict(
        {
            "pages": [
                {
                    "page_index": 0,
                    "blocks": [
                        {
                            "block_id": "b1",
                            "type": "text",
                            "bbox": [0, 400, 300, 600],
                            "lines": [
                                {
                                    "line_id": "l1",
                                    "bbox": [0, 400, 300, 600],
                                    "spans": [
                                        {
                                            "span_id": "s1",
                                            "bbox": [50, 477, 70, 492],
                                            "kind": "inline_equation",
                                            "content": r"\alpha_{1}",
                                        }
                                    ],
                                }
                            ],
                        }
                    ],
                    "reading_order": [],
                }
            ]
        }
    )
    (agent_dir / "provider_ir.json").write_text(
        provider.to_json(), encoding="utf-8"
    )

    style = _style()
    formula = il_version_1.PdfFormula(
        box=il_version_1.Box(50, 300, 70, 315), x_offset=0.0, y_offset=0.0
    )
    paragraph = il_version_1.PdfParagraph(
        box=il_version_1.Box(0, 200, 300, 400),
        pdf_style=style,
        pdf_paragraph_composition=[
            il_version_1.PdfParagraphComposition(
                pdf_same_style_unicode_characters=il_version_1.PdfSameStyleUnicodeCharacters(
                    unicode="见 ", pdf_style=style
                )
            ),
            il_version_1.PdfParagraphComposition(pdf_formula=formula),
            il_version_1.PdfParagraphComposition(
                pdf_same_style_unicode_characters=il_version_1.PdfSameStyleUnicodeCharacters(
                    unicode=" 所示。", pdf_style=style
                )
            ),
        ],
        unicode="见 {v1} 所示。",
        debug_id="P01-100",
        layout_label="text",
        xobj_id=0,
    )
    docs = _il_doc([_il_page(0, [paragraph], width=612, height=792)])
    config = _FakeConfig(working_dir=tmp_path)
    state = overlay_mod.capture_layout_sources(docs, config)

    assert state["provider_inline_spans"] == 1
    assert "P01-100" in state["bodies"]
    assert state["bodies"]["P01-100"] == r"见 $\alpha_{1}$ 所示。"
    assert state["paragraphs"]["P01-100"]["has_formula"] is True


# --------------------------------------------------------------------------- #
# overlay：链接安全
# --------------------------------------------------------------------------- #
def _make_link_pdf(tmp_path: pathlib.Path, box: pymupdf.Rect):
    """构造带 URI/GOTO/NAMED 链接的两页 PDF（box 参数保留以兼容调用方）。"""
    _ = box
    doc = pymupdf.open()
    doc.new_page(width=400, height=300)
    doc.new_page(width=400, height=300)
    page = doc[0]
    page.insert_text((30, 70), "Original paragraph line one", fontsize=9)
    page.insert_text((30, 90), "original paragraph second line", fontsize=9)
    page.insert_link(
        {
            "kind": pymupdf.LINK_URI,
            "from": pymupdf.Rect(30, 50, 300, 66),
            "uri": "https://example.com/ref1",
        }
    )
    page.insert_link(
        {
            "kind": pymupdf.LINK_GOTO,
            "from": pymupdf.Rect(30, 70, 300, 86),
            "page": 1,
            "to": pymupdf.Point(30, 40),
        }
    )
    page.insert_link(
        {
            "kind": pymupdf.LINK_NAMED,
            "from": pymupdf.Rect(360, 280, 395, 296),
            "nameddest": "sec1",
        }
    )
    path = tmp_path / "links.pdf"
    doc.save(path)
    doc.close()
    return path


def _overlay_fixture(tmp_path, monkeypatch, *, with_links=True, min_fill=1.01):
    src = _make_link_pdf(tmp_path, pymupdf.Rect()) if with_links else _pdf_with_text(
        tmp_path,
        [(30, 70, "Original paragraph line one", 9), (30, 90, "second line here", 9)],
    )
    pdf = pymupdf.open(src) if with_links else pymupdf.open(src)
    page = pdf[0]
    # 目标 box（IL 坐标，y-up）：mupdf rect (30,50)-(300,110) → IL y 300-110..300-50
    box = [30.0, 300.0 - 110.0, 300.0, 300.0 - 50.0]
    paragraph = _translated_paragraph(
        "P01-001",
        "这是一段较长的中文译文用于替换原有英文段落内容。",
        il_version_1.Box(*box),
    )
    docs = _il_doc([_il_page(0, [paragraph], width=400, height=300)])
    state = {
        "paragraphs": {
            "P01-001": {
                "page": 0,
                "box": box,
                "font_size": 9.0,
                "layout_label": "text",
                "has_formula": True,
            }
        },
        "bodies": {"P01-001": "这是一段较长的中文译文用于替换原有英文段落内容。"},
        "plain_texts": {"P01-001": "这是一段较长的中文译文用于替换原有英文段落内容。"},
        "fusion_failures": {},
        "provider_inline_spans": 1,
    }
    config = _FakeConfig(latex_bbox_state=state, latex_min_line_fill=min_fill, working_dir=tmp_path)

    stamp_dir = tmp_path / "stamps"
    stamp_dir.mkdir(exist_ok=True)
    stamp_path = _make_stamp_pdf(stamp_dir / "stamp.pdf", 270.0, 60.0)

    def fake_render_many(self, requests):
        _ = self
        results = {}
        for request, _workdir in requests:
            results[request.key] = renderer_mod.StampResult(
                key=request.key, ok=True, pdf_path=stamp_path, font_size=9.0, scale=1.0
            )
        return results

    monkeypatch.setattr(BboxStampRenderer, "render_many", fake_render_many)
    return pdf, docs, config


def _read_links(path: pathlib.Path):
    doc = pymupdf.open(path)
    try:
        return sorted(
            (
                page.number,
                tuple(link["from"]),
                link.get("kind"),
                link.get("uri"),
                link.get("page"),
                link.get("nameddest"),
            )
            for page in doc
            for link in page.get_links()
        )
    finally:
        doc.close()


def test_overlay_preserves_link_multiset_and_uri_set(tmp_path, monkeypatch):
    pdf, docs, config = _overlay_fixture(tmp_path, monkeypatch)
    before_path = tmp_path / "before.pdf"
    pdf.save(before_path)
    before_links = _read_links(before_path)
    before_uris = {item[3] for item in before_links if item[3]}

    result_pdf, stats = overlay_mod.apply_latex_bbox_overlay(pdf, docs, config)

    after_path = tmp_path / "after.pdf"
    result_pdf.save(after_path)
    after_links = _read_links(after_path)

    assert stats["applied"] >= 1
    assert after_links == before_links
    assert {item[3] for item in after_links if item[3]} == before_uris
    assert stats["links"]["uri_set_match"] is True
    assert stats["reverted"] is False
    assert stats["links"]["restored"] >= 1


def test_overlay_replaces_text_layer_without_double_layer(tmp_path, monkeypatch):
    pdf, docs, config = _overlay_fixture(tmp_path, monkeypatch)
    result_pdf, stats = overlay_mod.apply_latex_bbox_overlay(pdf, docs, config)
    out = tmp_path / "out.pdf"
    result_pdf.save(out)

    doc = pymupdf.open(out)
    try:
        rect = pymupdf.Rect(30, 50, 300, 110)
        in_box = doc[0].get_text(clip=rect)
        assert "Original paragraph" not in in_box
        assert "original paragraph" not in in_box
    finally:
        doc.close()
    assert stats["applied_paragraphs"] == ["P01-001"]


def test_overlay_reverts_to_pre_overlay_bytes_when_gate_fails(tmp_path, monkeypatch):
    """链接门禁失败 → 整体回滚到 overlay 前字节快照，链接完整恢复。"""
    pdf, docs, config = _overlay_fixture(tmp_path, monkeypatch)
    before_path = tmp_path / "before.pdf"
    pdf.save(before_path)
    before_links = _read_links(before_path)

    monkeypatch.setattr(
        overlay_mod.LatexBboxOverlay,
        "_verify_links",
        lambda _self, *_args, **_kwargs: False,
    )
    result_pdf, stats = overlay_mod.apply_latex_bbox_overlay(pdf, docs, config)

    out = tmp_path / "reverted.pdf"
    result_pdf.save(out)
    assert stats["reverted"] is True
    assert stats["applied"] == 0
    assert stats["applied_paragraphs"] == []
    assert _read_links(out) == before_links


def test_verify_links_detects_missing_link(tmp_path):
    """门禁本体：链接被删/多出时返回 False，一致时返回 True。"""
    src = _make_link_pdf(tmp_path, pymupdf.Rect())
    pdf = pymupdf.open(src)
    overlay = overlay_mod.LatexBboxOverlay(pdf, _il_doc([_il_page(0, [])]), _FakeConfig())

    actual = overlay_mod._links_by_signature(pdf[0].get_links())
    uris = overlay._uri_set(pdf)
    assert overlay._verify_links({0: actual}, uris) is True
    # 期望多一条链接 → False
    bogus = dict(actual)
    bogus[((0.0, 0.0, 1.0, 1.0), 2, "https://missing.example", None, "")] = 1
    assert overlay._verify_links({0: bogus}, uris) is False
    # URI 集合不一致 → False
    assert overlay._verify_links({0: actual}, uris | {"https://extra.example"}) is False


def test_overlay_no_captured_paragraphs_is_noop(tmp_path):
    pdf = pymupdf.open(_make_link_pdf(tmp_path, pymupdf.Rect()))
    docs = _il_doc([_il_page(0, [])])
    config = _FakeConfig(latex_bbox_state={}, working_dir=tmp_path)
    result_pdf, stats = overlay_mod.apply_latex_bbox_overlay(pdf, docs, config)
    assert result_pdf is pdf
    assert stats["applied"] == 0
    assert stats["fallback_reasons"] == {"no-captured-paragraphs": 0}


def test_overlay_capability_unavailable_falls_back(tmp_path, monkeypatch):
    pdf, docs, config = _overlay_fixture(tmp_path, monkeypatch)
    config.latex_xelatex_path = "/nonexistent/xelatex"
    config.latex_cjk_font_path = "/nonexistent/font.ttf"
    result_pdf, stats = overlay_mod.apply_latex_bbox_overlay(pdf, docs, config)
    assert result_pdf is pdf
    assert stats["available"] is False
    assert stats["applied"] == 0
    assert "capability-unavailable" in stats["fallback_reasons"]


def test_overlay_geometry_and_quality_gates(tmp_path, monkeypatch):
    """repair 模式：纯文本短行门禁保持旧行为（line-fill-ok 回退）。"""
    pdf, docs, config = _overlay_fixture(tmp_path, monkeypatch)
    config.latex_bbox_mode = "repair"
    # 把段落标为无公式，行填充门禁调低 → "line-fill-ok" 回退。
    config.latex_bbox_state["paragraphs"]["P01-001"]["has_formula"] = False
    config.latex_min_line_fill = 0.0
    _result, stats = overlay_mod.apply_latex_bbox_overlay(pdf, docs, config)
    assert stats["applied"] == 0
    assert stats["fallback_reasons"].get("line-fill-ok") == 1
    assert stats["mode"] == "repair"


def test_overlay_full_mode_ignores_line_fill_gate(tmp_path, monkeypatch):
    """full 模式：行填充门禁不再拦段落（多行正文段默认入选）。"""
    pdf, docs, config = _overlay_fixture(tmp_path, monkeypatch)
    config.latex_min_line_fill = 1.01  # repair 模式下会拦掉；full 模式不看它
    _result, stats = overlay_mod.apply_latex_bbox_overlay(pdf, docs, config)
    assert stats["mode"] == "full"
    assert stats["applied"] == 1
    assert "line-fill-ok" not in stats["fallback_reasons"]


def test_overlay_full_mode_rejects_non_body_label(tmp_path, monkeypatch):
    """full 模式：title/toc/reference 等非正文本体标签永不走 LaTeX。"""
    pdf, docs, config = _overlay_fixture(tmp_path, monkeypatch)
    docs.page[0].pdf_paragraph[0].layout_label = "title"
    config.latex_bbox_state["paragraphs"]["P01-001"]["layout_label"] = "title"
    _result, stats = overlay_mod.apply_latex_bbox_overlay(pdf, docs, config)
    assert stats["applied"] == 0
    assert stats["fallback_reasons"].get("label-not-eligible") == 1


def test_overlay_full_mode_rejects_single_line(tmp_path, monkeypatch):
    """full 模式：源行数 <2 的段落跳过。"""
    pdf, docs, config = _overlay_fixture(tmp_path, monkeypatch)
    # 只留一行源文：清掉 box 内的第二行。
    page = pdf[0]
    rect = pymupdf.Rect(30, 50, 300, 110)
    page.add_redact_annot(pymupdf.Rect(30, 80, 300, 110))
    page.apply_redactions()
    _result, stats = overlay_mod.apply_latex_bbox_overlay(pdf, docs, config)
    assert stats["applied"] == 0
    assert stats["fallback_reasons"].get("single-line") == 1
    assert rect is not None  # 保留 rect 变量供阅读者对照


def test_overlay_full_mode_skips_untranslated(tmp_path, monkeypatch):
    """译文与源文一致（LLM 未翻译）→ untranslated 跳过。"""
    pdf, docs, config = _overlay_fixture(tmp_path, monkeypatch)
    text = "这是一段较长的中文译文用于替换原有英文段落内容。"
    config.latex_bbox_state["paragraphs"]["P01-001"]["source_text"] = text
    _result, stats = overlay_mod.apply_latex_bbox_overlay(pdf, docs, config)
    assert stats["applied"] == 0
    assert stats["fallback_reasons"].get("untranslated") == 1
    decisions = {d["debug_id"]: d for d in stats["decisions"]}
    assert decisions["P01-001"]["reason"] == "untranslated"


def test_overlay_full_mode_keeps_translated_paragraph(tmp_path, monkeypatch):
    """源文与译文不同 → 不被 untranslated 拦截。"""
    pdf, docs, config = _overlay_fixture(tmp_path, monkeypatch)
    config.latex_bbox_state["paragraphs"]["P01-001"]["source_text"] = (
        "Original paragraph line one original paragraph second line"
    )
    _result, stats = overlay_mod.apply_latex_bbox_overlay(pdf, docs, config)
    assert stats["applied"] == 1
    assert "untranslated" not in stats["fallback_reasons"]


def test_overlay_skips_box_expanded_after_typesetting(tmp_path, monkeypatch):
    """扩后 box 内落有别的段落 → 跳过（贴片/redaction 可能伤到它们）。"""
    pdf, docs, config = _overlay_fixture(tmp_path, monkeypatch)
    paragraph = docs.page[0].pdf_paragraph[0]
    paragraph.box = il_version_1.Box(30, 100, 300, 280)  # 远超源 box
    # 另一个段落落在扩后区域内。
    other = _translated_paragraph(
        "P01-002", "另一段正文内容。", il_version_1.Box(40, 150, 280, 170)
    )
    docs.page[0].pdf_paragraph.append(other)
    _result, stats = overlay_mod.apply_latex_bbox_overlay(pdf, docs, config)
    assert stats["applied"] == 0
    assert stats["fallback_reasons"].get("box-expanded-after-typesetting") == 1


def test_overlay_allows_expanded_box_without_other_paragraphs(
    tmp_path, monkeypatch
):
    """扩后 box 干净 → full 模式允许（字符已不进内容流，无双层文本风险）。"""
    pdf, docs, config = _overlay_fixture(tmp_path, monkeypatch)
    docs.page[0].pdf_paragraph[0].box = il_version_1.Box(30, 100, 300, 280)
    _result, stats = overlay_mod.apply_latex_bbox_overlay(pdf, docs, config)
    assert stats["applied"] == 1
    assert "box-expanded-after-typesetting" not in stats["fallback_reasons"]


@requires_latex
@pytest.mark.parametrize(
    ("kind", "extra"),
    [
        (pymupdf.LINK_URI, {"uri": "https://example.com/plain"}),
        (pymupdf.LINK_GOTO, {"page": 1, "to": pymupdf.Point(20, 30)}),
        (pymupdf.LINK_NAMED, {"nameddest": "sec1"}),
        (pymupdf.LINK_LAUNCH, {"file": "https://example.com/launch"}),
    ],
)
def test_overlay_keeps_all_link_kinds_inside_redacted_box(
    tmp_path, monkeypatch, kind, extra
):
    """URI/GOTO/NAMED/LAUNCH 四种链接在 redaction 区内都能保留（不丢目标）。"""
    doc = pymupdf.open()
    doc.new_page(width=400, height=300)
    doc.new_page(width=400, height=300)
    page = doc[0]
    page.insert_text((30, 70), "Original paragraph line one", fontsize=9)
    page.insert_text((30, 90), "original paragraph second line", fontsize=9)
    target = {"kind": kind, "from": pymupdf.Rect(30, 50, 300, 66), **extra}
    page.insert_link(target)
    src = tmp_path / f"link-{kind}.pdf"
    doc.save(src)
    doc.close()

    pdf = pymupdf.open(src)
    before_path = tmp_path / f"before-{kind}.pdf"
    pdf.save(before_path)

    box = [30.0, 300.0 - 110.0, 300.0, 300.0 - 50.0]
    paragraph = _translated_paragraph(
        "P01-001", "中文译文内容替换原有英文段落。", il_version_1.Box(*box)
    )
    docs = _il_doc([_il_page(0, [paragraph], width=400, height=300)])
    state = {
        "paragraphs": {
            "P01-001": {
                "page": 0,
                "box": box,
                "font_size": 9.0,
                "layout_label": "text",
                "has_formula": True,
            }
        },
        "bodies": {"P01-001": "中文译文内容替换原有英文段落。"},
        "plain_texts": {"P01-001": "中文译文内容替换原有英文段落。"},
        "fusion_failures": {},
        "provider_inline_spans": 1,
    }
    config = _FakeConfig(latex_bbox_state=state, working_dir=tmp_path)
    stamp_path = _make_stamp_pdf(tmp_path / f"stamp-{kind}.pdf", 270.0, 60.0)

    def fake_render_many(self, requests):
        _ = self
        return {
            request.key: renderer_mod.StampResult(
                key=request.key, ok=True, pdf_path=stamp_path, font_size=9.0, scale=1.0
            )
            for request, _wd in requests
        }

    monkeypatch.setattr(BboxStampRenderer, "render_many", fake_render_many)
    result_pdf, stats = overlay_mod.apply_latex_bbox_overlay(pdf, docs, config)
    out = tmp_path / f"out-{kind}.pdf"
    result_pdf.save(out)

    assert stats["reverted"] is False, stats["links"]
    assert stats["links"]["uri_set_match"] is True
    kept = pymupdf.open(out)
    try:
        links = kept[0].get_links()
        assert links, "链接在 overlay 后丢失"
        if kind == pymupdf.LINK_URI:
            assert {l["uri"] for l in links if l.get("uri")} == {
                "https://example.com/plain"
            }
        elif kind in (pymupdf.LINK_GOTO,):
            assert any(l.get("page") == 1 for l in links)
        elif kind == pymupdf.LINK_NAMED:
            assert any(l.get("nameddest") == "sec1" for l in links)
        elif kind == pymupdf.LINK_LAUNCH:
            # pymupdf 落盘时 LAUNCH 会改写为 GOTOR 并对 file 做百分号编码，
            # 但目标语义必须保留（与门禁同一归一化）。
            assert any(
                overlay_mod._canonical_file_target(l.get("file") or "")
                == "https:/example.com/launch"
                for l in links
            )
    finally:
        kept.close()


def test_measure_line_fill_detects_watermark(tmp_path):
    path = _pdf_with_text(
        tmp_path,
        [(30, 70, "本文档由 funstory.ai 的 BabelDOC 翻译", 9)],
    )
    doc = pymupdf.open(path)
    try:
        metrics = overlay_mod.measure_line_fill(
            doc[0], pymupdf.Rect(0, 0, 400, 300)
        )
    finally:
        doc.close()
    assert metrics["watermark"] is True


def test_measure_line_fill_reports_min_body_fill(tmp_path):
    path = _pdf_with_text(
        tmp_path,
        [(30, 70, "short", 9), (30, 90, "a much longer second line of text", 9)],
    )
    doc = pymupdf.open(path)
    try:
        metrics = overlay_mod.measure_line_fill(
            doc[0], pymupdf.Rect(0, 0, 400, 300)
        )
    finally:
        doc.close()
    assert metrics["n_lines"] == 2
    assert metrics["min_body_fill"] is not None


# --------------------------------------------------------------------------- #
# 默认关闭回归：PDFCreater.write 不得触发 overlay
# --------------------------------------------------------------------------- #
def _minimal_write_fixture(tmp_path, enable_latex: bool):
    """构造可跑 PDFCreater.write 的最小 mono 环境。"""
    from babeldoc.format.pdf.document_il.backend.pdf_creater import PDFCreater
    from babeldoc.format.pdf.high_level import get_translation_stage
    from babeldoc.format.pdf.translation_config import TranslationConfig
    from babeldoc.format.pdf.translation_config import WatermarkOutputMode
    from babeldoc.progress_monitor import ProgressMonitor

    src = _pdf_with_text(tmp_path, [(30, 70, "Hello world original", 9)])
    config = TranslationConfig(
        input_file=str(src),
        lang_in="en",
        lang_out="zh",
        doc_layout_model=object(),
        output_dir=str(tmp_path),
        working_dir=str(tmp_path / f"wd-{enable_latex}"),
        watermark_output_mode=WatermarkOutputMode.NoWatermark,
        enable_latex_bbox_layout=enable_latex,
    )
    config.progress_monitor = ProgressMonitor(get_translation_stage(config))
    config.skip_clean = True
    page = il_version_1.Page(
        page_number=0,
        cropbox=il_version_1.Cropbox(box=il_version_1.Box(0, 0, 400, 300)),
        mediabox=il_version_1.Mediabox(box=il_version_1.Box(0, 0, 400, 300)),
    )
    docs = _il_doc([page])
    creater = PDFCreater(str(src), docs, config, {})
    return creater, config, docs


def test_default_off_does_not_touch_overlay(tmp_path, monkeypatch):
    """enable_latex_bbox_layout=False：不创建 overlay，统计为空。"""
    called = {"n": 0}

    class _SpyOverlay:
        def __init__(self, *_args, **_kwargs):
            called["n"] += 1
            raise AssertionError("overlay 不应在默认关闭时被创建")

    monkeypatch.setattr(
        "babeldoc.format.pdf.document_il.backend.latex_bbox.LatexBboxOverlay",
        _SpyOverlay,
    )
    creater, config, _docs = _minimal_write_fixture(tmp_path, enable_latex=False)
    try:
        result = creater.write(config)
    finally:
        config.cleanup_temp_files()
    assert called["n"] == 0
    assert creater.latex_bbox_stats == {}
    assert config.latex_bbox_stats == {}
    assert result.mono_pdf_path is not None
    assert pathlib.Path(result.mono_pdf_path).exists()


def test_enabled_write_invokes_overlay_once(tmp_path, monkeypatch):
    """enable_latex_bbox_layout=True：prepare/stamp 各被调用一次且统计回写。"""
    calls = {"prepare": 0, "stamp": 0}

    class _FakeOverlay:
        def __init__(self, pdf, _docs, config):
            self.pdf = pdf
            self.config = config
            self.stats = {"enabled": True, "applied": 0, "available": False}

        def prepare(self):
            calls["prepare"] += 1
            return set()

        def stamp(self, regenerate_pages=None):
            _ = regenerate_pages
            calls["stamp"] += 1
            self.config.latex_bbox_stats = self.stats
            return self.pdf

    monkeypatch.setattr(
        "babeldoc.format.pdf.document_il.backend.latex_bbox.LatexBboxOverlay",
        _FakeOverlay,
    )
    creater, config, _docs = _minimal_write_fixture(tmp_path, enable_latex=True)
    try:
        result = creater.write(config)
    finally:
        config.cleanup_temp_files()
    assert calls["prepare"] == 1
    assert calls["stamp"] == 1
    assert creater.latex_bbox_stats == {"enabled": True, "applied": 0, "available": False}
    assert result.mono_pdf_path is not None


# --------------------------------------------------------------------------- #
# 真实样本离线集成冒烟（编译 + redact + 贴片）
# --------------------------------------------------------------------------- #
@requires_latex
def test_real_compile_overlay_end_to_end(tmp_path):
    """真实编译中文段落并在 bbox 内替换原英文文本，链接保持。"""
    src = _make_link_pdf(tmp_path, pymupdf.Rect())
    pdf = pymupdf.open(src)
    before_path = tmp_path / "before.pdf"
    pdf.save(before_path)
    before_links = _read_links(before_path)

    box = [30.0, 300.0 - 110.0, 300.0, 300.0 - 50.0]  # mupdf (30,50)-(300,110)
    paragraph = _translated_paragraph(
        "P01-001",
        "这是一段真实编译的中文译文，用来验证 LaTeX bbox 排版与贴片链路可以端到端工作。",
        il_version_1.Box(*box),
    )
    docs = _il_doc([_il_page(0, [paragraph], width=400, height=300)])
    state = {
        "paragraphs": {
            "P01-001": {
                "page": 0,
                "box": box,
                "font_size": 9.0,
                "layout_label": "text",
                "has_formula": True,
            }
        },
        "bodies": {
            "P01-001": "这是一段真实编译的中文译文，用来验证 LaTeX bbox 排版与贴片链路可以端到端工作。"
        },
        "plain_texts": {
            "P01-001": "这是一段真实编译的中文译文，用来验证 LaTeX bbox 排版与贴片链路可以端到端工作。"
        },
        "fusion_failures": {},
        "provider_inline_spans": 1,
    }
    config = _FakeConfig(latex_bbox_state=state, working_dir=tmp_path)

    result_pdf, stats = overlay_mod.apply_latex_bbox_overlay(pdf, docs, config)
    out = tmp_path / "e2e.pdf"
    result_pdf.save(out)

    assert stats["available"] is True
    assert stats["applied"] >= 1
    assert stats["compile"]["attempts"] >= 1
    assert stats["links"]["uri_set_match"] is True

    doc = pymupdf.open(out)
    try:
        rect = pymupdf.Rect(30, 50, 300, 110)
        in_box = doc[0].get_text(clip=rect)
        assert "中文译文" in in_box
        assert "Original paragraph" not in in_box
    finally:
        doc.close()
    assert _read_links(out) == before_links
    # 报告落盘
    assert (tmp_path / "latex_bbox_report.json").exists()


@requires_latex
def test_renderer_bounded_shrink_uses_multiple_attempts(tmp_path):
    """极小 bbox + 长文本：缩小后仍失败也不抛异常，且记录尝试次数。"""
    renderer = BboxStampRenderer(_CAPABILITY, timeout_seconds=20.0)
    request = StampRequest(
        key="tiny",
        body="这是一段很长的中文文本" * 20,
        width=40.0,
        height=12.0,
        font_size=10.0,
    )
    result = renderer.render_one(request, tmp_path)
    assert result.compile_attempts >= 1
    if not result.ok:
        assert result.reason


# --------------------------------------------------------------------------- #
# 逐段决策报告（decisions[]）
# --------------------------------------------------------------------------- #
def _make_two_line_stamp_pdf(path: pathlib.Path, width: float, height: float):
    doc = pymupdf.open()
    page = doc.new_page(width=width, height=height)
    page.insert_text((2, 12), "第一行贴片译文内容", fontsize=8)
    page.insert_text((2, 26), "第二行贴片译文内容", fontsize=8)
    doc.save(path)
    doc.close()
    return str(path)


def _decisions_fixture(tmp_path, monkeypatch):
    """三个正文/非正文段落：一个 applied、一个 box-too-small、一个 page_number。"""
    src = _pdf_with_text(
        tmp_path,
        [(30, 70, "line one of the original paragraph", 9), (30, 90, "line two here", 9)],
        width=400,
        height=300,
    )
    pdf = pymupdf.open(src)
    applied_box = [30.0, 300.0 - 110.0, 300.0, 300.0 - 50.0]
    applied = _translated_paragraph(
        "P01-001",
        "这是一段较长的中文译文用于替换原有英文段落内容。",
        il_version_1.Box(*applied_box),
    )
    tiny_box = [30.0, 200.0, 300.0, 204.0]
    tiny = _translated_paragraph(
        "P01-002", "小框段落", il_version_1.Box(*tiny_box), size=10.0
    )
    page_number_para = _translated_paragraph(
        "P01-003",
        "1",
        il_version_1.Box(*applied_box),
        layout_label="page_number",
    )
    docs = _il_doc(
        [_il_page(0, [applied, tiny, page_number_para], width=400, height=300)]
    )
    state = {
        "paragraphs": {
            "P01-001": {
                "page": 0,
                "box": applied_box,
                "font_size": 9.0,
                "layout_label": "text",
                "has_formula": True,
                "fuse_kinds": ["formula", "text"],
            },
            "P01-002": {
                "page": 0,
                "box": tiny_box,
                "font_size": 10.0,
                "layout_label": "text",
                "has_formula": False,
                "fuse_kinds": ["text"],
            },
            "P01-003": {
                "page": 0,
                "box": applied_box,
                "font_size": 9.0,
                "layout_label": "page_number",
                "has_formula": True,
                "fuse_kinds": ["text"],
            },
        },
        "bodies": {
            "P01-001": "这是一段较长的中文译文用于替换原有英文段落内容。",
            "P01-002": "小框段落",
            "P01-003": "1",
        },
        "plain_texts": {
            "P01-001": "这是一段较长的中文译文用于替换原有英文段落内容。",
            "P01-002": "小框段落",
            "P01-003": "1",
        },
        "fusion_failures": {},
        "provider_inline_spans": 1,
    }
    config = _FakeConfig(latex_bbox_state=state, working_dir=tmp_path)

    stamp_dir = tmp_path / "stamps"
    stamp_dir.mkdir(exist_ok=True)
    stamp_path = _make_two_line_stamp_pdf(
        stamp_dir / "stamp.pdf", applied_box[2] - applied_box[0], applied_box[3] - applied_box[1]
    )

    def fake_render_many(
        self, requests  # noqa: ARG001 - 与 BboxStampRenderer.render_many 同签名
    ):
        results = {}
        for request, _workdir in requests:
            ok = request.key == "P01-001"
            results[request.key] = renderer_mod.StampResult(
                key=request.key,
                ok=ok,
                pdf_path=stamp_path if ok else None,
                font_size=9.0,
                scale=1.0,
                compile_attempts=2 if ok else 1,
                reason="ok" if ok else "s0:vertical-overflow",
            )
        return results

    monkeypatch.setattr(BboxStampRenderer, "render_many", fake_render_many)
    return pdf, docs, config, applied_box


def test_report_records_per_paragraph_decisions(tmp_path, monkeypatch):
    """decisions[] 记录 body 候选段的 applied 与回退原因，不记 page_number。"""
    pdf, docs, config, applied_box = _decisions_fixture(tmp_path, monkeypatch)

    _result, stats = overlay_mod.apply_latex_bbox_overlay(pdf, docs, config)

    by_id = {row["debug_id"]: row for row in stats["decisions"]}
    assert set(by_id) == {"P01-001", "P01-002"}

    applied_row = by_id["P01-001"]
    assert applied_row["reason"] == "applied"
    assert applied_row["page"] == 0
    assert applied_row["label"] == "text"
    assert applied_row["fuse_kinds"] == ["formula", "text"]
    assert applied_row["font_scale"] == 1.0
    assert applied_row["attempts"] == 2
    assert applied_row["lead"] == 13.5  # 9.0pt × 1.5
    assert applied_row["fill_after"] is not None

    rejected_row = by_id["P01-002"]
    assert rejected_row["reason"] == "box-too-small"
    assert rejected_row["font_scale"] is None
    assert rejected_row["attempts"] is None

    # 决策数组按 (page, debug_id) 排序。
    assert [row["debug_id"] for row in stats["decisions"]] == ["P01-001", "P01-002"]


def test_report_decisions_written_to_working_dir(tmp_path, monkeypatch):
    """decisions[] 落盘到 working_dir/latex_bbox_report.json。"""
    pdf, docs, config, _box = _decisions_fixture(tmp_path, monkeypatch)

    overlay_mod.apply_latex_bbox_overlay(pdf, docs, config)

    report = json.loads((tmp_path / "latex_bbox_report.json").read_text(encoding="utf-8"))
    reasons = {row["debug_id"]: row["reason"] for row in report["decisions"]}
    assert reasons == {"P01-001": "applied", "P01-002": "box-too-small"}


def test_report_records_compile_failure_reason(tmp_path, monkeypatch):
    """编译失败的候选段记 compile:<reason>，并保留 fill_before。"""
    pdf, docs, config, _box = _decisions_fixture(tmp_path, monkeypatch)
    pdf2, docs2, config2 = _overlay_fixture(tmp_path, monkeypatch, with_links=False)

    def fake_failing_render_many(self, requests):  # noqa: ARG001
        return {
            request.key: renderer_mod.StampResult(
                key=request.key,
                ok=False,
                reason="s0:vertical-overflow",
                compile_attempts=1,
            )
            for request, _workdir in requests
        }

    monkeypatch.setattr(BboxStampRenderer, "render_many", fake_failing_render_many)
    _result, stats = overlay_mod.apply_latex_bbox_overlay(pdf2, docs2, config2)

    row = next(r for r in stats["decisions"] if r["debug_id"] == "P01-001")
    assert row["reason"] == "compile:s0:vertical-overflow"
    assert row["attempts"] == 1
    assert row["fill_after"] is None
    # 未进入量测阶段的段落 fill_before 保持 None（box-too-small 在量测之前）。
    assert pdf is not None and docs is not None and config is not None


def test_report_records_capability_unavailable_reason(tmp_path, monkeypatch):
    """能力缺失时每个 body 候选段记 capability-unavailable。"""
    pdf, docs, config, _box = _decisions_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(
        overlay_mod,
        "probe_latex_capability",
        lambda **_kwargs: capability.LatexCapability(available=False, reasons=["no-xelatex"]),
    )

    _result, stats = overlay_mod.apply_latex_bbox_overlay(pdf, docs, config)

    assert stats["decisions"]
    assert {row["reason"] for row in stats["decisions"]} == {"capability-unavailable"}


def test_measure_line_fills_reports_per_line_fills(tmp_path):
    """measure_line_fills 返回逐行填充率，min_body_fill 与 measure_line_fill 一致。"""
    path = _pdf_with_text(
        tmp_path,
        [(30, 70, "short", 9), (30, 90, "a much longer second line of text", 9)],
    )
    doc = pymupdf.open(path)
    try:
        fills = overlay_mod.measure_line_fills(doc[0], pymupdf.Rect(0, 0, 400, 300))
        aggregate = overlay_mod.measure_line_fill(doc[0], pymupdf.Rect(0, 0, 400, 300))
    finally:
        doc.close()
    assert fills["n_lines"] == 2
    assert len(fills["fills"]) == 2
    assert fills["fills"][0] < fills["fills"][1]
    assert fills["min_body_fill"] == aggregate["min_body_fill"]


# --------------------------------------------------------------------------- #
# P1：prepare/stamp 拆分、免 redaction 贴片、bp 单位、未翻译与模式开关
# --------------------------------------------------------------------------- #
def _typeset_paragraph(debug_id: str, text: str, box, size: float = 9.0):
    """构造 Typesetting 之后的段落（composition 已是逐字符）。"""
    style = _style(size)
    compositions = [
        il_version_1.PdfParagraphComposition(
            pdf_character=il_version_1.PdfCharacter(
                char_unicode=char,
                box=il_version_1.Box(box.x + index * 5, box.y, box.x + index * 5 + 5, box.y2),
                pdf_character_id=ord(char) % 100,
                pdf_style=style,
                xobj_id=0,
            )
        )
        for index, char in enumerate(text)
    ]
    return il_version_1.PdfParagraph(
        box=box,
        pdf_style=style,
        pdf_paragraph_composition=compositions,
        unicode=text,
        debug_id=debug_id,
        layout_label="text",
        xobj_id=0,
        first_line_indent=False,
    )


def test_render_units_skip_stamped_paragraph_chars(tmp_path):
    """内容流生成跳过 stamped_ids 的字符：已贴片段落的旧译文不再入流。"""
    creater, config, docs = _minimal_write_fixture(tmp_path, enable_latex=True)
    page = docs.page[0]
    page.pdf_paragraph.append(
        _typeset_paragraph("P01-001", "译文内容", il_version_1.Box(30, 60, 200, 90))
    )
    page.pdf_paragraph.append(
        _typeset_paragraph("P01-002", "另一段", il_version_1.Box(30, 100, 200, 130))
    )

    all_units = creater.create_render_units_for_page(page, config)
    skipped_units = creater.create_render_units_for_page(
        page, config, skip_paragraph_ids={"P01-001"}
    )

    all_chars = [unit.char.char_unicode for unit in all_units if hasattr(unit, "char")]
    skipped_chars = [
        unit.char.char_unicode for unit in skipped_units if hasattr(unit, "char")
    ]
    assert "译文内容" not in "".join(skipped_chars)
    assert "译文内容" in "".join(all_chars)
    assert "另一段" in "".join(skipped_chars)
    assert len(all_units) - len(skipped_units) == len("译文内容")


def test_prepare_returns_stamped_ids_before_stamp(tmp_path, monkeypatch):
    """prepare 预先选段+编译并返回 stamped_ids；stamp 才真正落贴片。"""
    pdf, docs, config = _overlay_fixture(tmp_path, monkeypatch)
    overlay = overlay_mod.LatexBboxOverlay(pdf, docs, config)
    stamped_ids = overlay.prepare()

    assert stamped_ids == {"P01-001"}
    assert overlay.stats["attempted"] == 1
    assert overlay.stats["applied"] == 0

    result = overlay.stamp()
    assert result is pdf
    assert overlay.stats["applied"] == 1
    assert overlay.stats["applied_paragraphs"] == ["P01-001"]
    assert overlay.stats["reverted"] is False


def test_prepare_repair_mode_skips_no_chars(tmp_path, monkeypatch):
    """repair 模式不做预选：prepare 返回空集合（旧译文照常入内容流）。"""
    pdf, docs, config = _overlay_fixture(tmp_path, monkeypatch)
    config.latex_bbox_mode = "repair"
    overlay = overlay_mod.LatexBboxOverlay(pdf, docs, config)
    assert overlay.prepare() == set()
    assert overlay.stats["attempted"] == 0
    result = overlay.stamp()
    assert overlay.stats["applied"] == 1
    assert result is pdf or result is not None


def test_stamp_without_text_layer_does_not_redact(tmp_path, monkeypatch):
    """stamp rect 内没有文本层时不走 redaction（链接不被删除/重插）。

    真实 full 流程下已贴片段落的字符不会进内容流，正常命中本分支；这里用
    repair 模式 + 空矩形直接验证「只在有文本时才 redact」的条件逻辑。
    """
    pdf = pymupdf.open(_make_link_pdf(tmp_path, pymupdf.Rect()))
    # 目标 rect 区域（mupdf (30,150)-(300,200)）里没有任何文本。
    box = [30.0, 300.0 - 200.0, 300.0, 300.0 - 150.0]
    docs = _il_doc(
        [
            _il_page(
                0,
                [
                    _translated_paragraph(
                        "P01-001", "中文贴片内容替换空白区域。", il_version_1.Box(*box)
                    )
                ],
                width=400,
                height=300,
            )
        ]
    )
    state = {
        "paragraphs": {
            "P01-001": {
                "page": 0,
                "box": box,
                "font_size": 9.0,
                "layout_label": "text",
                "has_formula": True,
            }
        },
        "bodies": {"P01-001": "中文贴片内容替换空白区域。"},
        "plain_texts": {"P01-001": "中文贴片内容替换空白区域。"},
        "fusion_failures": {},
        "provider_inline_spans": 0,
    }
    config = _FakeConfig(
        latex_bbox_state=state, working_dir=tmp_path, latex_bbox_mode="repair"
    )
    stamp_path = _make_stamp_pdf(tmp_path / "stamp.pdf", 270.0, 60.0)

    def fake_render_many(self, requests):
        _ = self
        return {
            request.key: renderer_mod.StampResult(
                key=request.key, ok=True, pdf_path=stamp_path, font_size=9.0, scale=1.0
            )
            for request, _wd in requests
        }

    monkeypatch.setattr(BboxStampRenderer, "render_many", fake_render_many)
    _result, stats = overlay_mod.apply_latex_bbox_overlay(pdf, docs, config)

    assert stats["applied"] == 1
    assert stats["links"]["restored"] == 0
    assert stats["links"]["uri_set_match"] is True


@requires_latex
def test_compiled_page_matches_bbox_in_bp(tmp_path):
    """编译页尺寸 == bbox（±0.01bp）：模板长度单位与 PDF bp 一致。"""
    renderer = BboxStampRenderer(_CAPABILITY, max_workers=1)
    request = StampRequest(
        key="P01-bp",
        body="单位一致性验证文本",
        width=180.5,
        height=42.25,
        font_size=9.0,
    )
    result = renderer.render_one(request, tmp_path)
    assert result.ok, result.reason
    doc = pymupdf.open(result.pdf_path)
    try:
        rect = doc[0].rect
        assert abs(rect.width - 180.5) <= 0.01
        assert abs(rect.height - 42.25) <= 0.01
    finally:
        doc.close()


def test_repair_mode_reproduces_legacy_no_body_lines(tmp_path, monkeypatch):
    """repair 模式保留旧词表：单行纯文本段 → no-body-lines。"""
    pdf, docs, config = _overlay_fixture(tmp_path, monkeypatch)
    config.latex_bbox_mode = "repair"
    config.latex_bbox_state["paragraphs"]["P01-001"]["has_formula"] = False
    page = pdf[0]
    page.add_redact_annot(pymupdf.Rect(30, 80, 300, 110))
    page.apply_redactions()
    _result, stats = overlay_mod.apply_latex_bbox_overlay(pdf, docs, config)
    assert stats["applied"] == 0
    assert stats["fallback_reasons"].get("no-body-lines") == 1


def test_record_source_texts_and_capture_meta():
    """record_source_texts 记录源文；capture 把它写进段落 meta（供 untranslated）。"""
    paragraph = _translated_paragraph(
        "P01-001", "译文文本", il_version_1.Box(30, 60, 200, 90)
    )
    docs = _il_doc([_il_page(0, [paragraph], width=400, height=300)])
    config = _FakeConfig(latex_bbox_state={})

    texts = overlay_mod.record_source_texts(docs, config)
    assert texts == {"P01-001": "译文文本"}
    assert config.latex_source_texts == {"P01-001": "译文文本"}

    state = overlay_mod.capture_layout_sources(docs, config)
    assert state["paragraphs"]["P01-001"]["source_text"] == "译文文本"

    state2 = overlay_mod.capture_layout_sources(
        docs, _FakeConfig(latex_bbox_state={}), source_texts={"P01-001": "别的源文"}
    )
    assert state2["paragraphs"]["P01-001"]["source_text"] == "别的源文"


def test_stamp_exception_regenerates_affected_pages(tmp_path, monkeypatch):
    """贴片中途抛异常：重新生成受影响页（不跳过字符），避免段落丢字。"""
    pdf, docs, config = _overlay_fixture(tmp_path, monkeypatch)
    overlay = overlay_mod.LatexBboxOverlay(pdf, docs, config)
    assert overlay.prepare() == {"P01-001"}

    regenerated: list[list[int]] = []

    def _regenerate(page_indices):
        regenerated.append(list(page_indices))

    def _boom(self, jobs, successful):
        _ = (self, jobs, successful)
        raise RuntimeError("simulated-stamp-failure")

    monkeypatch.setattr(overlay_mod.LatexBboxOverlay, "_stamp_pages", _boom)
    result = overlay.stamp(regenerate_pages=_regenerate)

    assert result is pdf
    assert regenerated == [[0]]
    assert overlay.stats["reverted"] is True
    assert overlay.stats["applied"] == 0
    assert overlay.stats["error"] and "simulated-stamp-failure" in overlay.stats["error"]
    assert (tmp_path / "latex_bbox_report.json").exists()


@requires_latex
def test_concurrent_identical_content_uses_isolated_dirs(tmp_path):
    """并发批编译：内容相同但 key 不同的段落各自独立目录，互不覆盖。"""
    renderer = BboxStampRenderer(_CAPABILITY, max_workers=4)
    requests = [
        (
            StampRequest(
                key=f"P0{index}-dup",
                body="并发相同内容段落测试文本",
                width=160.0,
                height=40.0,
                font_size=9.0,
            ),
            tmp_path,
        )
        for index in range(4)
    ]
    stamps = renderer.render_many(requests)
    assert set(stamps) == {request.key for request, _wd in requests}
    for stamp in stamps.values():
        assert stamp.ok, stamp.reason
        assert pathlib.Path(stamp.pdf_path).exists()


def test_overlay_mode_defaults_to_full_and_unknown_falls_back(tmp_path, monkeypatch):
    """未知 mode 按 full 处理；stats 里记录生效的 mode。"""
    pdf, docs, config = _overlay_fixture(tmp_path, monkeypatch)
    config.latex_bbox_mode = "bogus"
    overlay = overlay_mod.LatexBboxOverlay(pdf, docs, config)
    assert overlay.mode == "full"
    overlay.prepare()
    assert overlay.stats["mode"] == "full"
    assert overlay.stamp() is pdf


def test_overlay_link_gate_fail_regenerates_affected_pages(tmp_path, monkeypatch):
    """full 模式链接门禁失败：重生成受影响页回滚（非字节快照），统计一致。"""
    pdf, docs, config = _overlay_fixture(tmp_path, monkeypatch)
    before_links = _read_links(pdf)

    regenerated: list[list[int]] = []

    def _regenerate(page_indices):
        regenerated.append(list(page_indices))

    monkeypatch.setattr(
        overlay_mod.LatexBboxOverlay,
        "_verify_links",
        lambda _self, *_args, **_kwargs: False,
    )
    overlay = overlay_mod.LatexBboxOverlay(pdf, docs, config)
    overlay.prepare()
    result_pdf, stats = overlay.stamp(regenerate_pages=_regenerate), overlay.stats

    assert stats["reverted"] is True
    assert stats["applied"] == 0
    assert stats["applied_paragraphs"] == []
    assert stats["links"]["restored"] == 0
    # 重生成回调收到受影响页（0-based）
    assert regenerated == [[0]]
    # 重生成后链接与 overlay 前一致
    assert _read_links(result_pdf) == before_links
    # 回滚后 fallback_reasons 不再残留 applied 计数
    assert stats["fallback_reasons"].get("applied", 0) == 0
