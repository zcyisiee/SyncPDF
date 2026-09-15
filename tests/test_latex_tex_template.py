r"""LaTeX 模板（P3-1/2/3/4）：字体、断词容差、缩进、行距与 bp 单位。

覆盖：

1. 字体与产品一致：``\setmainfont`` = Noto Serif/Sans，``\setCJKmainfont`` =
   Source Han Serif/Sans CN（按段落 serif 标志；显式字体优先）；
2. 断词与容差指令齐全（``babel[english]``/``url``/hyphenpenalty/tolerance/
   emergencystretch/lineskiplimit/XeTeXlinebreakskip），且**不用** ``\sloppy``；
3. 首行缩进与悬挂（``\parindent`` / ``\hangindent``+``\hangafter``）、
   ``\topskip``（有源几何时才写）；
4. ``derive_lead`` clamp 与真实编译（无 XeLaTeX 时 skip）。
"""

from __future__ import annotations

import pathlib

import numpy as np
import pymupdf
import pytest
from babeldoc.format.pdf.document_il.backend.latex_bbox import (
    capability as capability_mod,
)
from babeldoc.format.pdf.document_il.backend.latex_bbox.renderer import (
    BboxStampRenderer,
)
from babeldoc.format.pdf.document_il.backend.latex_bbox.renderer import StampRequest
from babeldoc.format.pdf.document_il.backend.latex_bbox.renderer import derive_lead
from babeldoc.format.pdf.document_il.backend.latex_bbox.renderer_batch import (
    BatchStampRenderer,
)

_CAPABILITY = capability_mod.probe_latex_capability()
requires_latex = pytest.mark.skipif(
    not _CAPABILITY.available, reason="需要 XeLaTeX + 中文字体"
)


def _renderer():
    return BboxStampRenderer(_CAPABILITY)


# --------------------------------------------------------------------------- #
# P3-1 字体
# --------------------------------------------------------------------------- #
def test_template_sets_product_serif_fonts():
    tex = _renderer().build_tex("正文", 200.0, 60.0, 9.0, serif=True)

    serif_latin = _CAPABILITY.latin_serif_fonts
    if serif_latin:
        assert "\\setmainfont[" in tex
        assert pathlib.Path(serif_latin["regular"]).stem in tex
        if serif_latin.get("bold"):
            assert f"BoldFont={pathlib.Path(serif_latin['bold']).stem}" in tex
        if serif_latin.get("italic"):
            assert f"ItalicFont={pathlib.Path(serif_latin['italic']).stem}" in tex
        if serif_latin.get("bolditalic"):
            assert (
                f"BoldItalicFont={pathlib.Path(serif_latin['bolditalic']).stem}" in tex
            )
    cjk_serif = _CAPABILITY.cjk_serif_fonts
    if cjk_serif:
        assert "\\setCJKmainfont[" in tex
        assert pathlib.Path(cjk_serif["regular"]).stem in tex
        assert f"BoldFont={pathlib.Path(cjk_serif['bold']).stem}" in tex


def test_template_uses_sans_fonts_for_sans_paragraph():
    tex = _renderer().build_tex("正文", 200.0, 60.0, 9.0, serif=False)

    sans_latin = _CAPABILITY.latin_sans_fonts
    if sans_latin:
        assert pathlib.Path(sans_latin["regular"]).stem in tex
    cjk_sans = _CAPABILITY.cjk_sans_fonts
    if cjk_sans:
        assert pathlib.Path(cjk_sans["regular"]).stem in tex


def test_template_prefers_explicit_cjk_font_over_family():
    explicit = _CAPABILITY.font_path
    tex = _renderer().build_tex("正文", 200.0, 60.0, 9.0, serif=True)
    # 未显式指定时用字体族（serif）；显式指定时只用该字体。
    if _CAPABILITY.font_explicit and explicit:
        assert f"UprightFont={pathlib.Path(explicit).stem}" in tex


def test_capability_reports_font_sets():
    assert _CAPABILITY.latin_serif_fonts is None or isinstance(
        _CAPABILITY.latin_serif_fonts, dict
    )
    assert _CAPABILITY.cjk_serif_fonts is None or isinstance(
        _CAPABILITY.cjk_serif_fonts, dict
    )
    payload = _CAPABILITY.to_dict()
    assert "latin_serif_fonts" in payload and "cjk_sans_fonts" in payload
    assert _CAPABILITY.latin_fonts(True) == _CAPABILITY.latin_serif_fonts
    assert _CAPABILITY.cjk_fonts(False) == _CAPABILITY.cjk_sans_fonts


# --------------------------------------------------------------------------- #
# P3-2 断词与容差
# --------------------------------------------------------------------------- #
def test_template_enables_hyphenation_and_tolerance():
    tex = _renderer().build_tex("A long english sentence", 200.0, 60.0, 9.0)

    assert "\\usepackage[english]{babel}" in tex
    assert "\\usepackage{url}" in tex
    assert "\\hyphenpenalty=50" in tex
    assert "\\tolerance=1500" in tex
    assert "\\emergencystretch=1em" in tex
    assert "\\lineskiplimit=0pt" in tex
    assert "\\lineskip=1pt" in tex
    assert "\\lineskiplimit=-\\maxdimen" not in tex
    assert "\\XeTeXlinebreakskip = 0pt plus 0.3em" in tex
    # 不用 \sloppy（会牺牲字间距质量）。
    assert "\\sloppy" not in tex


# --------------------------------------------------------------------------- #
# P3-3 缩进 / P3-4 行距
# --------------------------------------------------------------------------- #
def test_template_sets_indent_hang_and_topskip():
    tex = _renderer().build_tex(
        "正文",
        200.0,
        60.0,
        9.0,
        lead=11.0,
        parindent=6.0,
        hangindent=0.0,
        topskip=0.5,
    )
    assert "\\setlength{\\parindent}{6.0000bp}" in tex
    # 行距用源行距（bp），而非固定 1.5×。
    assert "\\fontsize{9.0000bp}{11.0000bp}" in tex
    # ascent + 字体 ascender 的 topskip 已写出。
    assert "\\setlength{\\topskip}{" in tex
    assert "\\hangindent" not in tex


def test_template_hanging_indent_uses_hangindent_and_hangafter():
    tex = _renderer().build_tex(
        "正文", 200.0, 60.0, 9.0, parindent=0.0, hangindent=4.0
    )
    assert "\\setlength{\\parindent}{0.0000bp}" in tex
    assert "\\setlength{\\hangindent}{4.0000bp}\\hangafter=1" in tex


def test_template_without_geometry_omits_topskip():
    tex = _renderer().build_tex("正文", 200.0, 60.0, 9.0, topskip=None)

    assert "\\topskip" not in tex


def test_stamp_request_indentation_derives_parindent_and_hang():
    positive = StampRequest(
        key="a", body="x", width=100.0, height=50.0, font_size=9.0, first_line_dx=6.0
    )
    hanging = StampRequest(
        key="b", body="x", width=100.0, height=50.0, font_size=9.0, first_line_dx=-5.0
    )

    assert positive.indentation == (6.0, 0.0)
    # 悬挂：首行在段左缘（parindent=0），其余行缩进 |dx|（hangindent）。
    assert hanging.indentation == (0.0, 5.0)


def test_derive_lead_clamps_source_pitch():
    # 区间内（1.4×字号）原样保留。
    assert derive_lead(9.0, 12.6) == 12.6
    # 小于 1.3×字号 → 抬到下限。
    assert derive_lead(9.0, 3.0) == pytest.approx(11.7)
    # 大于 1.6×字号 → 压到上限。
    assert derive_lead(9.0, 30.0) == pytest.approx(14.4)
    # 无源行距 → 产品默认系数。
    assert derive_lead(9.0, None) == pytest.approx(13.5)


def test_cache_key_includes_geometry_fields():
    base = StampRequest(
        key="a", body="x", width=100.0, height=50.0, font_size=9.0, lead=11.0
    )
    other = StampRequest(
        key="a", body="x", width=100.0, height=50.0, font_size=9.0, lead=12.0
    )

    assert base.cache_key != other.cache_key


# --------------------------------------------------------------------------- #
# 真实编译
# --------------------------------------------------------------------------- #
@requires_latex
@pytest.mark.parametrize("batch", [False, True], ids=["single", "batch"])
def test_compiled_tall_inline_formulas_have_row_clearance(tmp_path, batch):
    formula = r"$x_{i_{j_k}}^{a^{b^c}} + \frac{\frac{a}{b}}{\frac{c}{d}}$"
    request = StampRequest(
        key="tall-rows",
        body=rf"A {formula}\\B {formula}\\C ordinary text\\D ordinary text",
        width=240.0,
        height=140.0,
        font_size=9.0,
        lead=11.656,
        ascent_top=0.5,
    )
    if batch:
        renderer = BatchStampRenderer(_CAPABILITY)
        companion = StampRequest(
            key="companion", body="Companion", width=240, height=140, font_size=9
        )
        result = renderer.render_many(
            [(request, tmp_path), (companion, tmp_path)]
        )[request.key]
        assert renderer.batch_count == 1
    else:
        result = _renderer().render_one(request, tmp_path)
    assert result.ok, result.reason
    assert result.scale == pytest.approx(1.0)
    assert result.compile_attempts == 1

    with pymupdf.open(result.pdf_path) as doc:
        chars = [
            char
            for block in doc[0].get_text("rawdict")["blocks"]
            for line in block.get("lines", [])
            for span in line["spans"]
            for char in span["chars"]
        ]
        markers = {
            char["c"]: index
            for index, char in enumerate(chars)
            if char["c"] in "ABCD"
        }
        assert set(markers) == set("ABCD")
        # Font envelopes overestimate nested math ink; measure the actual raster,
        # including fraction rules, at 4 pixels per bp instead.
        pix = doc[0].get_pixmap(matrix=pymupdf.Matrix(4, 4), colorspace=pymupdf.csGRAY)
        pixels = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width)
        occupied = np.flatnonzero((pixels < 128).any(axis=1))
        rows = np.split(occupied, np.where(np.diff(occupied) > 1)[0] + 1)
        assert len(rows) == 4, "Formula rows must have separate ink bands"
        clearance = (rows[1][0] - rows[0][-1] - 1) / 4
        assert clearance >= 0.75, f"Adjacent formula rows overlap: {clearance=:.3f}bp"
        # Normal rows retain the requested baseline pitch, not a global increase.
        baseline_c = chars[markers["C"]]["origin"][1]
        baseline_d = chars[markers["D"]]["origin"][1]
        assert baseline_d - baseline_c == pytest.approx(request.lead, abs=0.02)


@requires_latex
def test_compiled_paragraph_uses_product_fonts_and_indent(tmp_path):
    renderer = _renderer()
    body = (
        "近年来，长程任务智能体（long-horizon agent）的应用迅速扩展，"
        "使得超长上下文处理日益成为关键的模型工作负载。"
    )
    request = StampRequest(
        key="p3-fonts",
        body=body,
        width=320.0,
        height=60.0,
        font_size=9.0,
        expected_text=body,
        lead=11.5,
        first_line_dx=6.0,
        ascent_top=0.5,
        serif=True,
    )

    result = renderer.render_one(request, tmp_path)

    assert result.ok, result.reason
    assert result.lead == pytest.approx(11.5)
    doc = pymupdf.open(result.pdf_path)
    try:
        fonts = {font[3].split("+")[-1] for font in doc[0].get_fonts()}
        names = " ".join(fonts)
        if _CAPABILITY.cjk_serif_fonts:
            assert "SourceHanSerifCN" in names
        if _CAPABILITY.latin_serif_fonts:
            assert "NotoSerif" in names
        # 首行缩进 6bp：首行左缘明显大于后续行。
        rows: dict[float, float] = {}
        for word in doc[0].get_text("words"):
            rows[round(word[1])] = min(rows.get(round(word[1]), 1e9), word[0])
        ordered = [rows[key] for key in sorted(rows)]
        assert ordered[0] == pytest.approx(6.0, abs=0.5)
        assert min(ordered[1:]) == pytest.approx(0.0, abs=0.5)
        # 首行墨迹不从 box 顶溢出。
        ink = pymupdf.Rect()
        for block in doc[0].get_text("blocks"):
            ink |= pymupdf.Rect(block[:4])
        assert ink.y0 >= -0.5
    finally:
        doc.close()


@requires_latex
def test_compiled_paragraph_tight_lead_still_fits(tmp_path):
    """源行距比默认 1.5× 小：行距推导后仍能放下（不缩字号）。"""
    renderer = _renderer()
    body = "这是一段用于验证源行距推导的中文正文内容，长度适中以便在两三行内排完。"
    request = StampRequest(
        key="p3-lead",
        body=body,
        width=320.0,
        height=60.0,
        font_size=10.0,
        expected_text=body,
        lead=11.5,  # 1.15×10：比默认 15 紧
        ascent_top=0.2,
    )

    result = renderer.render_one(request, tmp_path)

    assert result.ok, result.reason
    assert result.scale == pytest.approx(1.0)


@requires_latex
def test_compiled_hanging_indent_keeps_first_line_flush(tmp_path):
    """悬挂缩进：首行顶格、其余行缩进 |dx|（``\\hangindent`` 必须在正文里）。"""
    renderer = _renderer()
    body = (
        "这是一个用于验证悬挂缩进的段落，首行顶格，其余行缩进五个点，"
        "观察每行左缘位置是否与源行一致。"
    )
    request = StampRequest(
        key="p3-hang",
        body=body,
        width=200.0,
        height=60.0,
        font_size=9.0,
        expected_text=body,
        lead=12.0,
        first_line_dx=-5.0,
        ascent_top=0.2,
    )

    result = renderer.render_one(request, tmp_path)

    assert result.ok, result.reason
    doc = pymupdf.open(result.pdf_path)
    try:
        lines = [
            line["bbox"][0]
            for block in doc[0].get_text("dict")["blocks"]
            for line in block.get("lines", [])
        ]
        assert len(lines) >= 2
        assert lines[0] == pytest.approx(0.0, abs=0.5)
        assert lines[1] == pytest.approx(5.0, abs=0.5)
    finally:
        doc.close()
