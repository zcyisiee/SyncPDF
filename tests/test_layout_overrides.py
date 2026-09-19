"""排版覆盖通道：schema / patch 合并 / IR 应用 / Typesetting 挂钩默认值。"""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from babeldoc.format.pdf.document_il import il_version_1
from babeldoc.format.pdf.document_il.midend.typesetting import Typesetting
from babeldoc.tools.agent import layout_overrides as lo


# --------------------------------------------------------------------------- #
# fixture：迷你 IR（2 页 × 各 1 段，段落与 composition 共享同一 style 对象）
# --------------------------------------------------------------------------- #
def _style(size: float) -> il_version_1.PdfStyle:
    return il_version_1.PdfStyle(
        font_id="F1", font_size=size, graphic_state=il_version_1.GraphicState()
    )


def _paragraph(pid: str, text: str, box, size: float = 10.0):
    style = _style(size)
    return il_version_1.PdfParagraph(
        box=box,
        pdf_style=style,
        pdf_paragraph_composition=[
            il_version_1.PdfParagraphComposition(
                pdf_same_style_unicode_characters=il_version_1.PdfSameStyleUnicodeCharacters(
                    unicode=text, pdf_style=style
                )
            )
        ],
        unicode=text,
        debug_id=pid,
        layout_label="text",
        first_line_indent=False,
    )


def _make_doc():
    pages = []
    for page_number in range(2):
        paragraph = _paragraph(
            f"P{page_number + 1:02d}-001",
            f"第{page_number + 1}页正文",
            il_version_1.Box(x=10, y=600, x2=200, y2=700),
        )
        pages.append(
            il_version_1.Page(
                page_number=page_number,
                pdf_paragraph=[paragraph],
                cropbox=il_version_1.Cropbox(
                    box=il_version_1.Box(x=0, y=0, x2=612, y2=792)
                ),
            )
        )
    return SimpleNamespace(page=pages)


# --------------------------------------------------------------------------- #
# schema / patch
# --------------------------------------------------------------------------- #
def test_validate_accepts_full_patch():
    patch = {
        "paragraphs": {
            "P05-012": {
                "scale_cap": 0.92,
                "font_scale": 0.95,
                "line_skip": 1.35,
                "box_scale": 1.05,
                "box": [44.0, 500.0, 300.0, 620.0],
                "bold": True,
                "italic": False,
                "serif": True,
                "force_break_after_text": ["（1）", "（2）"],
                "force_break_after_offset": [23, 47],
            }
        },
        "pages": {"5": {"font_scale": 0.98}},
    }
    assert lo.validate(lo.normalize(patch)) == []


@pytest.mark.parametrize(
    "patch, fragment",
    [
        ({"paragraphs": {"P1-1": {"scale_cap": 9.0}}}, "超出范围"),
        ({"paragraphs": {"P1-1": {"font_scale": "0.9"}}}, "必须是数字"),
        ({"paragraphs": {"P1-1": {"box": [1, 2, 3]}}}, "四元数组"),
        ({"paragraphs": {"P1-1": {"box": [1, 2, 1, 9]}}}, "x2 > x"),
        ({"paragraphs": {"P1-1": {"box": [1, 9, 5, 2]}}}, "y2 > y"),
        ({"paragraphs": {"P1-1": {"bold": "yes"}}}, "布尔值"),
        ({"paragraphs": {"P1-1": {"italic": 1}}}, "布尔值"),
        ({"paragraphs": {"P1-1": {"serif": "true"}}}, "布尔值"),
        ({"paragraphs": {"P1-1": {"force_break_after_offset": [-1]}}}, "非负整数"),
        ({"paragraphs": {"P1-1": {"typo_key": 1}}}, "未知字段"),
        ({"pages": {"first": {"font_scale": 0.9}}}, "页码"),
    ],
)
def test_validate_rejects_bad_patch(patch, fragment):
    errors = lo.validate(lo.normalize(patch))
    assert any(fragment in error for error in errors), errors


def test_validate_allows_bool_none_deletion_and_style_flag():
    # allow_none=True 的 patch 里 null = 删除该样式覆盖。
    patch = {"paragraphs": {"P1-1": {"bold": None}}}
    assert lo.validate(lo.normalize(patch), allow_none=True) == []

    overrides = lo.normalize(
        {"paragraphs": {"P1-1": {"bold": True, "serif": False}}}
    )
    assert lo.style_flag(overrides, "P1-1", "bold") is True
    assert lo.style_flag(overrides, "P1-1", "serif") is False
    assert lo.style_flag(overrides, "P1-1", "italic") is None
    assert lo.style_flag(overrides, "P2-1", "bold") is None
    with pytest.raises(ValueError):
        lo.style_flag(overrides, "P1-1", "typo")


def test_patch_merge_diff_and_delete(tmp_path):
    first = lo.apply_patch(
        tmp_path,
        {"paragraphs": {"P01-001": {"scale_cap": 0.9, "line_skip": 1.4}}},
        reason="第一次",
    )
    assert first["ok"] is True
    assert first["diff"]["added"] == ["paragraphs.P01-001"]

    second = lo.apply_patch(
        tmp_path,
        {"paragraphs": {"P01-001": {"scale_cap": 0.8, "line_skip": None}}},
        reason="第二次",
    )
    data = lo.load_overrides(tmp_path)
    assert data["paragraphs"]["P01-001"] == {"scale_cap": 0.8}
    assert second["diff"]["changed"]["paragraphs.P01-001"] == {
        "scale_cap": [0.9, 0.8],
        "line_skip": [1.4, None],  # null = 删除该字段
    }
    assert len(data["history"]) == 2

    lo.apply_patch(tmp_path, {"paragraphs": {"P01-001": None}})
    assert lo.load_overrides(tmp_path)["paragraphs"] == {}


def test_load_overrides_missing_and_broken(tmp_path):
    assert lo.load_overrides(tmp_path) == lo.empty_overrides()
    path = lo.overrides_path(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not json", encoding="utf-8")
    data = lo.load_overrides(tmp_path)
    assert data["paragraphs"] == {} and "raw_error" in data


# --------------------------------------------------------------------------- #
# IR 应用
# --------------------------------------------------------------------------- #
def test_font_scale_paragraph_times_page():
    doc = _make_doc()
    overrides = lo.normalize(
        {
            "paragraphs": {"P01-001": {"font_scale": 0.5}, "P02-001": {"font_scale": 0.5}},
            "pages": {"1": {"font_scale": 0.8}},
        }
    )
    stats = lo.apply_to_ir(doc, overrides)
    assert stats["font_scaled"] == 2

    first = doc.page[0].pdf_paragraph[0]
    second = doc.page[1].pdf_paragraph[0]
    # 页 1：0.8（页级）× 0.5（段落级）；页 2：只有段落级 0.5
    assert first.pdf_style.font_size == pytest.approx(10.0 * 0.8 * 0.5)
    assert second.pdf_style.font_size == pytest.approx(10.0 * 0.5)
    # composition 与段落共享 style 对象时不能重复乘算
    comp = first.pdf_paragraph_composition[0].pdf_same_style_unicode_characters
    assert comp.pdf_style.font_size == pytest.approx(10.0 * 0.8 * 0.5)
    # 原段落未被交叉污染
    assert doc.page[1].pdf_paragraph[0].pdf_style.font_size == pytest.approx(5.0)


def test_no_overrides_is_noop():
    doc = _make_doc()
    before = [(p.pdf_style.font_size, copy.deepcopy(p.box)) for p in _iter_paras(doc)]
    stats = lo.apply_to_ir(doc, lo.empty_overrides())
    assert stats["paragraphs_touched"] == 0
    after = [(p.pdf_style.font_size, p.box) for p in _iter_paras(doc)]
    assert before[0][0] == after[0][0]
    assert (before[0][1].x, before[0][1].y2) == (after[0][1].x, after[0][1].y2)


def test_box_scale_anchors_top_left():
    doc = _make_doc()
    lo.apply_to_ir(doc, lo.normalize({"paragraphs": {"P01-001": {"box_scale": 2.0}}}))
    box = doc.page[0].pdf_paragraph[0].box
    # 原 box 10,600,200,700；锚定左上角 (x=10, y2=700) → 右/下各扩一倍
    assert (box.x, box.y2) == (10, 700)
    assert box.x2 == pytest.approx(390)  # 10 + (200-10)*2
    assert box.y == pytest.approx(500)  # 700 - (700-600)*2


def test_box_clamped_to_cropbox():
    doc = _make_doc()
    stats = lo.apply_to_ir(
        doc, lo.normalize({"paragraphs": {"P01-001": {"box_scale": 10.0}}})
    )
    box = doc.page[0].pdf_paragraph[0].box
    assert (box.x2, box.y) == (612, 0)
    assert stats["clamped"] == 1 and stats["warnings"]


def test_unmatched_ids_reported():
    doc = _make_doc()
    stats = lo.apply_to_ir(doc, lo.normalize({"paragraphs": {"P09-009": {"scale_cap": 0.5}}}))
    assert stats["unmatched_ids"] == ["P09-009"]


def _iter_paras(doc):
    for page in doc.page:
        yield from page.pdf_paragraph


# --------------------------------------------------------------------------- #
# Typesetting 挂钩（用 __new__ 绕开 FontMapper 加载）
# --------------------------------------------------------------------------- #
def _fake_typesetting(overrides: dict | None = None, line_skip=None, is_cjk=True):
    ts = Typesetting.__new__(Typesetting)
    ts.is_cjk = is_cjk
    ts.translation_config = SimpleNamespace(
        paragraph_layout_overrides=(
            lo.to_config_hook(lo.normalize(overrides)) if overrides else {}
        ),
        line_skip_override=line_skip,
        layout_warnings=[],
    )
    return ts


def test_scale_cap_only_lowers():
    ts = _fake_typesetting({"paragraphs": {"P01-001": {"scale_cap": 0.9}}})
    high = _paragraph("P01-001", "x", il_version_1.Box(x=0, y=0, x2=10, y2=10))
    high.optimal_scale = 1.0
    low = _paragraph("P02-001", "x", il_version_1.Box(x=0, y=0, x2=10, y2=10))
    low.optimal_scale = 0.5
    ts._apply_scale_caps([high, low])
    assert high.optimal_scale == 0.9
    assert low.optimal_scale == 0.5  # cap 不放大


def test_scale_cap_skipped_without_overrides():
    ts = _fake_typesetting()
    para = _paragraph("P01-001", "x", il_version_1.Box(x=0, y=0, x2=10, y2=10))
    para.optimal_scale = 1.0
    ts._apply_scale_caps([para])
    assert para.optimal_scale == 1.0


def test_line_skip_precedence():
    default_cjk = 1.50
    default_latin = 1.3
    para = _paragraph("P01-001", "x", il_version_1.Box(x=0, y=0, x2=10, y2=10))

    assert _fake_typesetting(is_cjk=True)._line_skip_for(para) == default_cjk
    assert _fake_typesetting(is_cjk=False)._line_skip_for(para) == default_latin
    assert (
        _fake_typesetting(is_cjk=True, line_skip=1.2)._line_skip_for(para) == 1.2
    )
    ts = _fake_typesetting(
        {"paragraphs": {"P01-001": {"line_skip": 1.8}}}, line_skip=1.2
    )
    assert ts._line_skip_for(para) == 1.8  # 段落覆盖优先于文档级
    other = _paragraph("P02-001", "x", il_version_1.Box(x=0, y=0, x2=10, y2=10))
    assert ts._line_skip_for(other) == 1.2


def test_config_defaults_do_not_change_behaviour(tmp_path):
    """回归硬约束：构建 TranslationConfig 后新字段默认为空/None（无覆盖 = 零行为变化）。"""
    from babeldoc.format.pdf.parse_shared import build_parse_only_config

    config = build_parse_only_config(
        tmp_path / "dummy.pdf", working_dir=tmp_path / "wd"
    )
    assert config.paragraph_layout_overrides == {}
    assert config.page_font_scale == {}
    assert config.line_skip_override is None
    assert config.layout_warnings == []
