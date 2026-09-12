"""LaTeX 融合的四级分类与 MinerU 对齐一致性校验（P2）。

覆盖：

1. ``unicode_math.math_latex``：数学字母数字 / 希腊 / 上下标 / 运算符转写，
   未知字符返回 ``None``（降级信号）；
2. ``classify_formula``：``text → mineru → simple_math → fragment`` 四级与
   降级顺序；
3. alignment 一一匹配的**拒绝**路径：混用多个 ``formula_layout_id``、
   span 文本与原生字符不一致、同一 span 被复用、同 box 多 span（不确定）、
   MinerU LaTeX 与原生字符不相容；
4. ``renderer.text_layer_matches``：容忍渲染插入（断词连字符、同形字形、
   大小写差异），不容忍缺字；
5. 片段内联：``crop_fragment_pdf`` 从源 PDF 裁区域 + ``\\includegraphics``
   真实编译（无 XeLaTeX 时 skip）。
"""

from __future__ import annotations

import pymupdf
import pytest
from babeldoc.format.pdf.document_il import il_version_1
from babeldoc.format.pdf.document_il.backend.latex_bbox import fusion
from babeldoc.format.pdf.document_il.backend.latex_bbox import overlay as overlay_mod
from babeldoc.format.pdf.document_il.backend.latex_bbox import renderer as renderer_mod
from babeldoc.format.pdf.document_il.backend.latex_bbox.capability import (
    probe_latex_capability,
)
from babeldoc.format.pdf.document_il.backend.latex_bbox.unicode_math import math_latex

_CAPABILITY = probe_latex_capability()
requires_latex = pytest.mark.skipif(
    not _CAPABILITY.available, reason="需要 XeLaTeX + 中文字体"
)


def _style(size: float = 10.0, font_id: str = "F1"):
    return il_version_1.PdfStyle(
        font_id=font_id,
        font_size=size,
        graphic_state=il_version_1.GraphicState(passthrough_per_char_instruction=""),
    )


def _formula(native: str, layout_id: int | None = None, curve: bool = False):
    box = il_version_1.Box(100, 100, 100 + 6 * max(len(native), 1), 114)
    chars = [
        il_version_1.PdfCharacter(
            char_unicode=ch,
            box=il_version_1.Box(box.x + i * 5, box.y, box.x + i * 5 + 5, box.y2),
            pdf_character_id=i,
            pdf_style=_style(),
            formula_layout_id=layout_id,
            xobj_id=0,
        )
        for i, ch in enumerate(native)
    ]
    formula = il_version_1.PdfFormula(
        box=box, x_offset=0.0, y_offset=-2.0, pdf_character=chars
    )
    if curve:
        formula.pdf_curve = [il_version_1.PdfCurve(box=box)]
    return formula


def _index(page_index: int, layout_id: int, span_id: str, latex: str, text: str):
    return fusion.FormulaLatexIndex(
        {
            page_index: {
                layout_id: fusion.ProtectedFormula(
                    span_id=span_id, latex=latex, text=text
                )
            }
        }
    )


# --------------------------------------------------------------------------- #
# 1. unicode_math
# --------------------------------------------------------------------------- #
def test_math_latex_transliterates_math_alphanumerics_and_operators():
    assert math_latex("𝑛") == "n"
    assert math_latex("𝑛win") == r"n_{\mathrm{win}}"
    assert math_latex("𝐿× 𝑛") == r"L\times n"
    assert math_latex("1344× 1344") == r"1344\times 1344"
    assert math_latex("√512 ≈ 22") == r"\sqrt{512} \approx 22"
    assert math_latex("→65") == r"\to 65"
    assert math_latex("Δ𝑏") == r"\Delta b"
    assert math_latex("𝜏") == r"\tau"
    assert math_latex("𝑥²") == "x^{2}"


def test_math_latex_returns_none_for_unknown_characters():
    """未知字符 → None（调用方降级 fragment，不猜测）。"""
    assert math_latex("caf´") is None
    assert math_latex("(cid:3) 😀") is None
    assert math_latex("") is None


# --------------------------------------------------------------------------- #
# 2. classify_formula：四级与降级顺序
# --------------------------------------------------------------------------- #
def test_classify_text_class_for_plain_ascii_native():
    assert fusion.classify_formula(_formula("1 | "), 0, None).kind == "text"
    assert fusion.classify_formula(_formula("[62], "), 0, None).kind == "text"
    assert fusion.classify_formula(_formula("• "), 0, None).kind == "text"


def test_classify_prose_word_fragment_is_text_even_with_graphics():
    """正文词片被误聚成公式（B2Rrm 类）：即使带矢量图形也按文本处理。"""
    result = fusion.classify_formula(_formula("ng stora", layout_id=7, curve=True), 0, None)
    assert result.kind == "text"
    assert result.reason == "prose-word-fragment"


def test_classify_mineru_when_alignment_matches_and_agrees():
    formula = _formula("𝑛win", layout_id=7)
    index = _index(0, 7, "s1", r"n_{\mathrm{win}}", r"n_{\mathrm{win}}")
    result = fusion.classify_formula(formula, 0, index)
    assert result.kind == "mineru"
    assert result.latex == r"n_{\mathrm{win}}"
    assert result.span_id == "s1"


def test_classify_simple_math_without_alignment():
    result = fusion.classify_formula(_formula("𝑛win"), 0, None)
    assert result.kind == "simple_math"
    assert result.latex == r"n_{\mathrm{win}}"


def test_classify_fragment_when_untranslatable():
    result = fusion.classify_formula(_formula("caf´"), 1, None)
    assert result.kind == "fragment"
    assert result.reason == "untranslatable-native"
    assert result.fragment is not None and result.fragment.page == 1


def test_classify_fragment_when_latex_incompatible_with_native():
    """MinerU LaTeX 去命令后的字母数字与原生字符不相容 → 不用该 LaTeX。"""
    formula = _formula("𝑛", layout_id=7)
    index = _index(0, 7, "s1", r"\int \frac{xyz}{abc}", r"\int \frac{xyz}{abc}")
    result = fusion.classify_formula(formula, 0, index)
    assert result.kind != "mineru"
    assert result.reason.startswith("mineru-")


def test_classify_rejects_span_text_inconsistent_with_native():
    """span 文本与原生字符不一致（OCR 把正文认成公式）→ 拒绝 MinerU LaTeX。"""
    formula = _formula("𝕏𝕐", layout_id=7)
    index = _index(0, 7, "s1", r"\alpha\beta", r"\alpha\beta")
    result = fusion.classify_formula(formula, 0, index)
    assert result.kind != "mineru"
    assert result.reason == "mineru-span-inconsistent"


def test_classify_rejects_mixed_layout_ids():
    """公式字符跨多个保护区（或多个 lid）→ 不确定，不用 MinerU LaTeX。"""
    formula = _formula("𝑛win", layout_id=7)
    for char in formula.pdf_character[1:]:
        char.formula_layout_id = 8
    index = _index(0, 7, "s1", r"n_{\mathrm{win}}", r"n_{\mathrm{win}}")
    result = fusion.classify_formula(formula, 0, index)
    assert result.kind != "mineru"


def test_classify_rejects_reused_span():
    """同一 span 已被同段其它公式用过 → 不再复用（span 一一对应）。"""
    formula = _formula("𝑛win", layout_id=7)
    index = _index(0, 7, "s1", r"n_{\mathrm{win}}", r"n_{\mathrm{win}}")
    assert fusion.classify_formula(formula, 0, index, {"s1"}).kind != "mineru"
    assert fusion.classify_formula(formula, 0, index, set()).kind == "mineru"


def test_fuse_paragraph_assigns_distinct_spans_per_formula():
    """同段两个公式各自占一个 span；第二个若与第一个同 span 则降级。"""
    style = _style()
    first = _formula("𝑛win", layout_id=7)
    second = _formula("𝑛win", layout_id=7)
    paragraph = il_version_1.PdfParagraph(
        box=il_version_1.Box(0, 0, 300, 60),
        pdf_style=style,
        pdf_paragraph_composition=[
            il_version_1.PdfParagraphComposition(
                pdf_same_style_unicode_characters=il_version_1.PdfSameStyleUnicodeCharacters(
                    unicode="甲 ", pdf_style=style
                )
            ),
            il_version_1.PdfParagraphComposition(pdf_formula=first),
            il_version_1.PdfParagraphComposition(
                pdf_same_style_unicode_characters=il_version_1.PdfSameStyleUnicodeCharacters(
                    unicode=" 乙 ", pdf_style=style
                )
            ),
            il_version_1.PdfParagraphComposition(pdf_formula=second),
        ],
        unicode="甲 {v1} 乙 {v2}",
        debug_id="P01-200",
        layout_label="text",
        xobj_id=0,
    )
    index = _index(0, 7, "s1", r"n_{\mathrm{win}}", r"n_{\mathrm{win}}")
    result = fusion.fuse_paragraph(paragraph, 0, {"F1": None}, index)
    assert result.ok is True
    assert result.formula_classes == ["mineru", "simple_math"]
    assert result.formulas_matched == 2
    # 第二个公式复用同一 span → 降级为 Unicode 转写，但仍渲染成同一个数学片段
    assert result.body.count(r"$n_{\mathrm{win}}$") == 2


# --------------------------------------------------------------------------- #
# 3. 文本层比较口径
# --------------------------------------------------------------------------- #
def test_text_layer_matches_tolerates_rendering_insertions():
    # 断词连字符：抽取比期望多字符
    assert renderer_mod.text_layer_matches("translation", "trans-lation")
    # 同形字形与大小写差异
    assert renderer_mod.text_layer_matches("Δx", "∆x")
    assert renderer_mod.text_layer_matches("win", "Win")
    # 数学字母数字经 NFKC 折叠
    assert renderer_mod.text_layer_matches("𝑛win", "nwin")


def test_text_layer_matches_rejects_missing_characters():
    assert not renderer_mod.text_layer_matches("abcdef", "abc")
    assert not renderer_mod.text_layer_matches("abc", "acb")
    assert not renderer_mod.text_layer_matches("中文译文", "中文译")


# --------------------------------------------------------------------------- #
# 4. 片段裁图与内联编译
# --------------------------------------------------------------------------- #
def _source_pdf(tmp_path, box=(30, 30, 90, 60)):
    doc = pymupdf.open()
    page = doc.new_page(width=400, height=300)
    page.insert_text((box[0], box[1] + 10), "F(x)=y", fontsize=9)
    path = tmp_path / "source.pdf"
    doc.save(path)
    doc.close()
    return path


def test_crop_fragment_pdf_extracts_region(tmp_path):
    source_path = _source_pdf(tmp_path)
    with pymupdf.open(source_path) as source:
        out = tmp_path / "frag.pdf"
        assert overlay_mod.crop_fragment_pdf(
            source, 0, pymupdf.Rect(25, 25, 95, 65), out
        )
        frag = pymupdf.open(out)
        try:
            assert len(frag) == 1
            assert frag[0].rect.width == pytest.approx(70.0, abs=0.01)
            assert frag[0].rect.height == pytest.approx(40.0, abs=0.01)
            assert "F(x)=y" in frag[0].get_text()
        finally:
            frag.close()
    # 越界/空白区域 → False
    with pymupdf.open(source_path) as source:
        assert not overlay_mod.crop_fragment_pdf(
            source, 5, pymupdf.Rect(25, 25, 95, 65), tmp_path / "bad.pdf"
        )
        # 退化尺寸（小于 0.5bp）→ 拒绝
        assert not overlay_mod.crop_fragment_pdf(
            source, 0, pymupdf.Rect(30, 30, 30.4, 60), tmp_path / "thin.pdf"
        )


@requires_latex
def test_fragment_includegraphics_compiles(tmp_path):
    """片段内联（``\\raisebox`` + ``\\includegraphics``）可真实编译。"""
    from babeldoc.format.pdf.document_il.backend.latex_bbox.renderer import (
        BboxStampRenderer,
    )
    from babeldoc.format.pdf.document_il.backend.latex_bbox.renderer import StampRequest

    source_path = _source_pdf(tmp_path)
    with pymupdf.open(source_path) as source:
        out = tmp_path / "frag.pdf"
        assert overlay_mod.crop_fragment_pdf(
            source, 0, pymupdf.Rect(25, 25, 95, 65), out
        )
    ref = fusion.FragmentRef(
        key="P01-300-0", page=0, box=(25.0, 25.0, 95.0, 65.0), y_offset=-2.0, height=14.0
    )
    body = "前文 " + fusion.render_fragment_latex(ref, str(out)) + " 后文"
    renderer = BboxStampRenderer(_CAPABILITY, timeout_seconds=45.0, max_workers=1)
    result = renderer.render_one(
        StampRequest(key="frag", body=body, width=240.0, height=40.0, font_size=10.0),
        tmp_path,
    )
    assert result.ok is True, result.reason
    with pymupdf.open(result.pdf_path) as compiled:
        # 裁出来的源区域自带文本层（保留矢量与文字），片段内容可见。
        assert "前文" in compiled[0].get_text()


class _FakeConfig:
    """最小 config 替身（overlay 只读少量属性）。"""

    def __init__(self, **kwargs):
        self.latex_bbox_state = kwargs.get("latex_bbox_state", {})
        self.enable_latex_bbox_layout = True
        self.latex_xelatex_path = _CAPABILITY.xelatex_path
        self.latex_cjk_font_path = _CAPABILITY.font_path
        self.latex_compile_timeout_seconds = 45.0
        self.latex_max_compile_workers = 1
        self.latex_min_line_fill = kwargs.get("latex_min_line_fill", 0.85)
        self.latex_bbox_mode = "full"
        self.latex_source_pdf_path = kwargs.get("latex_source_pdf_path")
        self.primary_font_family = None
        self.working_dir = kwargs.get("working_dir")
        self.latex_bbox_stats = {}


def test_overlay_rejects_paragraph_when_fragment_source_missing(tmp_path):
    """拿不到源 PDF → 含片段的段落整段回退（不留空图片引用）。"""
    pdf = pymupdf.open()
    source_page = pdf.new_page(width=400, height=300)
    # 段内两行源文（full 模式要求源行数 ≥2 才入选）。
    source_page.insert_text((32, 70), "Original paragraph line one here", fontsize=9)
    source_page.insert_text((32, 95), "second line of the paragraph", fontsize=9)
    box = [30.0, 190.0, 300.0, 250.0]
    style = _style(9.0)
    paragraph = il_version_1.PdfParagraph(
        box=il_version_1.Box(*box),
        pdf_style=style,
        pdf_paragraph_composition=[
            il_version_1.PdfParagraphComposition(
                pdf_same_style_unicode_characters=il_version_1.PdfSameStyleUnicodeCharacters(
                    unicode="含片段译文", pdf_style=style
                )
            )
        ],
        unicode="含片段译文",
        debug_id="P01-400",
        layout_label="text",
        xobj_id=0,
        first_line_indent=False,
    )
    docs = il_version_1.Document(
        page=[
            il_version_1.Page(
                page_number=0,
                pdf_paragraph=[paragraph],
                page_layout=[],
                cropbox=il_version_1.Cropbox(box=il_version_1.Box(0, 0, 400, 300)),
                mediabox=il_version_1.Mediabox(box=il_version_1.Box(0, 0, 400, 300)),
            )
        ],
        total_pages=1,
    )
    body = "含片段译文 " + fusion.FRAGMENT_MACRO + "{P01-400-0}"
    state = {
        "paragraphs": {
            "P01-400": {
                "page": 0,
                "box": box,
                "font_size": 9.0,
                "layout_label": "text",
                "has_formula": True,
                "fuse_kinds": ["formula", "text"],
                "fuse_classes": ["fragment"],
                "fragment_keys": ["P01-400-0"],
            }
        },
        "bodies": {"P01-400": body},
        "plain_texts": {"P01-400": "含片段译文 "},
        "fusion_failures": {},
        "fragments": {
            "P01-400-0": {
                "page": 0,
                "box": box,
                "y_offset": -2.0,
                "height": 8.0,
            }
        },
        "fusion_stats": {"classes": {"fragment": 1}, "notes": {}},
        "provider_inline_spans": 1,
    }
    config = _FakeConfig(
        latex_bbox_state=state,
        latex_min_line_fill=1.01,
        working_dir=tmp_path,
        latex_source_pdf_path=str(tmp_path / "missing.pdf"),
    )
    overlay = overlay_mod.LatexBboxOverlay(pdf, docs, config)
    assert overlay.prepare() == set()
    # 源 PDF 路径不存在 → 含片段的段落整段回退，不进编译。
    assert overlay.stats["fallback_reasons"].get("fragment-source-missing") == 1
    assert overlay.stats["attempted"] == 0
    assert (
        overlay._decisions["P01-400"]["reason"] == "fragment-source-missing"
    )


def test_cid_placeholder_never_becomes_text_or_math():
    """(cid:N) 占位串不得判 text/simple_math，必须降级 fragment。"""
    from babeldoc.format.pdf.document_il.backend.latex_bbox import fusion
    from babeldoc.format.pdf.document_il.backend.latex_bbox.unicode_math import (
        math_latex,
    )

    assert fusion._is_text_native("(cid:98)") is False
    assert fusion._is_text_native("ng stora") is True  # 正常词片不受影响
    assert math_latex("(cid:2)") is None
    # 分类入口：native 为 (cid:98) 的公式单元应判 fragment
    formula = _formula("(cid:98)")
    verdict = fusion.classify_formula(formula, 0, None)
    assert verdict.kind == "fragment"
    # fragment 不产生 LaTeX body 内容
    assert not verdict.latex
