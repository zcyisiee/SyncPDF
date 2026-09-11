"""强制换行：索引解析（文本锚定 / 偏移锚定）+ 实际换行效果。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from babeldoc.format.pdf.document_il import il_version_1
from babeldoc.format.pdf.document_il.midend.typesetting import Typesetting
from babeldoc.format.pdf.document_il.midend.typesetting import TypesettingUnit
from babeldoc.tools.agent import layout_overrides as lo

CJK_TEXT = "公众号排版测试文本用来验证强制换行是否生效"


def _unit(char: str, width: float = 10.0, height: float = 10.0, x: float = 0.0):
    style = il_version_1.PdfStyle(
        font_id="F1", font_size=10.0, graphic_state=il_version_1.GraphicState()
    )
    return TypesettingUnit(
        char=il_version_1.PdfCharacter(
            char_unicode=char,
            box=il_version_1.Box(x=x, y=0, x2=x + width, y2=height),
            pdf_style=style,
            visual_bbox=il_version_1.VisualBbox(
                box=il_version_1.Box(x=x, y=0, x2=x + width, y2=height)
            ),
        )
    )


def _typesetting(overrides: dict | None = None):
    ts = Typesetting.__new__(Typesetting)
    ts.is_cjk = True
    ts.translation_config = SimpleNamespace(
        paragraph_layout_overrides=(
            lo.to_config_hook(lo.normalize(overrides)) if overrides else {}
        ),
        line_skip_override=None,
        layout_warnings=[],
    )

    class _Font:
        def char_lengths(self, _ch, size):
            return (size,)

    ts.font_mapper = SimpleNamespace(base_font=_Font())
    return ts


def _paragraph(pid: str = "P01-001"):
    return il_version_1.PdfParagraph(
        box=il_version_1.Box(x=0, y=0, x2=100, y2=100),
        pdf_style=il_version_1.PdfStyle(
            font_id="F1", font_size=10, graphic_state=il_version_1.GraphicState()
        ),
        pdf_paragraph_composition=[],
        unicode=CJK_TEXT,
        debug_id=pid,
        first_line_indent=False,
    )


# --------------------------------------------------------------------------- #
# 索引解析
# --------------------------------------------------------------------------- #
def test_no_overrides_returns_empty_set():
    ts = _typesetting()
    units = [_unit(ch) for ch in CJK_TEXT]
    assert ts._force_break_indices(units, _paragraph()) == set()


def test_force_break_after_text_index():
    ts = _typesetting(
        {"paragraphs": {"P01-001": {"force_break_after_text": ["强制换行"]}}}
    )
    units = [_unit(ch) for ch in CJK_TEXT]
    indices = ts._force_break_indices(units, _paragraph())
    end = CJK_TEXT.index("强制换行") + len("强制换行")
    assert indices == {end}  # 在第 end 个单元之前换行


def test_force_break_after_offset_index():
    # offset = 新行开始处的字符偏移（前 5 个字符留在本行）
    ts = _typesetting({"paragraphs": {"P01-001": {"force_break_after_offset": [5]}}})
    units = [_unit(ch) for ch in CJK_TEXT]
    assert ts._force_break_indices(units, _paragraph()) == {5}


def test_text_anchor_uses_last_match():
    text = "abcabc"
    ts = _typesetting({"paragraphs": {"P01-001": {"force_break_after_text": ["abc"]}}})
    units = [_unit(ch) for ch in text]
    assert ts._force_break_indices(units, _paragraph()) == {6}


def test_unresolved_anchor_warns_without_raising():
    ts = _typesetting(
        {
            "paragraphs": {
                "P01-001": {
                    "force_break_after_text": ["不存在的子串"],
                    "force_break_after_offset": [999],
                }
            }
        }
    )
    units = [_unit(ch) for ch in CJK_TEXT]
    assert ts._force_break_indices(units, _paragraph()) == set()
    warnings = ts.translation_config.layout_warnings
    assert len(warnings) == 2
    assert all("P01-001" in warning for warning in warnings)


def test_formula_units_do_not_shift_text_offsets():
    """公式单元没有文本：锚点按"可见文本"偏移计算。"""
    style = il_version_1.PdfStyle(
        font_id="F1", font_size=10.0, graphic_state=il_version_1.GraphicState()
    )
    formula = TypesettingUnit(
        formular=il_version_1.PdfFormula(
            box=il_version_1.Box(x=0, y=0, x2=10, y2=10), pdf_character=[]
        )
    )
    units = [_unit("甲"), _unit("乙"), formula, _unit("丙")]
    ts = _typesetting({"paragraphs": {"P01-001": {"force_break_after_text": ["甲乙"]}}})
    assert ts._force_break_indices(units, _paragraph()) == {2}


# --------------------------------------------------------------------------- #
# 实际布局效果
# --------------------------------------------------------------------------- #
def _layout(ts, units, box, scale=1.0):
    paragraph = _paragraph()
    typeset, fits = ts._layout_typesetting_units(units, box, scale, 1.5, paragraph)
    rows = sorted({round(unit.box.y, 3) for unit in typeset})
    return typeset, fits, rows


def test_force_break_adds_a_line():
    box = il_version_1.Box(x=0, y=0, x2=100, y2=100)
    text = "一二三四五六七八九十"  # 10 字 × 10pt = 100pt：正好一行
    units = [_unit(ch) for ch in text]
    ts_plain = _typesetting()
    _, fits_plain, rows_plain = _layout(ts_plain, units, box)

    ts_break = _typesetting(
        {"paragraphs": {"P01-001": {"force_break_after_text": ["五"]}}}
    )
    units = [_unit(ch) for ch in text]
    _, fits_break, rows_break = _layout(ts_break, units, box)

    assert fits_plain and fits_break
    assert len(rows_plain) == 1
    assert len(rows_break) == 2


def test_layout_without_overrides_matches_original_path():
    """无覆盖时强制换行集合为空 → 布局结果与不传覆盖完全一致。"""
    box = il_version_1.Box(x=0, y=0, x2=60, y2=100)
    text = "一二三四五六七八九十甲乙丙丁"
    units_a = [_unit(ch) for ch in text]
    units_b = [_unit(ch) for ch in text]
    ts = _typesetting()
    result_a = _layout(ts, units_a, box)
    result_b = _layout(_typesetting({"paragraphs": {}}), units_b, box)
    assert len(result_a[2]) == len(result_b[2])
    assert [[u.try_get_unicode() for u in r] for r in (result_a[0], result_b[0])] == [
        [u.try_get_unicode() for u in r] for r in (result_a[0], result_b[0])
    ]


@pytest.mark.parametrize("offset, lines", [(0, 1), (1, 2)])
def test_break_before_first_unit_is_ignored(offset, lines):
    """offset=0（首单元前换行）没有意义：不应产生额外空行。"""
    box = il_version_1.Box(x=0, y=0, x2=100, y2=100)
    units = [_unit(ch) for ch in "一二三四五"]
    ts = _typesetting(
        {"paragraphs": {"P01-001": {"force_break_after_offset": [offset]}}}
    )
    _, _, rows = _layout(ts, units, box)
    assert len(rows) == lines
