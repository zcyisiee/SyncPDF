"""「扩容优先」排版的单元测试。

覆盖 ``Typesetting._expanded_box``（满额扩容计算）与
``_find_optimal_scale_and_layout``（扩容在缩字之前触发）的关键行为：

* 纵向拉满到最近障碍，并与上下相邻区域保持阈值间隔；
* 页边封边：不允许扩出页面；且只有 bbox 已贴近某页边（"位于顶部/底部"）
  时该页边才是合法边界，远离页边、该方向无障碍时不扩（不进页边距）；
* 宽度不足只走纵向扩容，横向仅加不明显的 tolerance，且被左右邻居 clamp；
* 两轴交叠的障碍（如整页水印框）不构成扩容边界；
* 排版阶梯的顺序：先在原始 box 尝试初始字号，失败后满额扩容并回到
  初始字号重试，仍失败才按阶梯缩字。
"""

from __future__ import annotations

import types as py_types

import pytest
from babeldoc.format.pdf.document_il import Box
from babeldoc.format.pdf.document_il import il_version_1
from babeldoc.format.pdf.document_il.midend.typesetting import (
    EXPAND_HORIZONTAL_TOLERANCE,
)
from babeldoc.format.pdf.document_il.midend.typesetting import EXPAND_VERTICAL_GAP
from babeldoc.format.pdf.document_il.midend.typesetting import Typesetting


def make_typesetting() -> Typesetting:
    """跳过 FontMapper/真实 config 构造，只补 _find/_expanded_box 用到的属性。"""
    ts = object.__new__(Typesetting)
    ts.translation_config = py_types.SimpleNamespace(
        paragraph_layout_overrides=None,
        line_skip_override=None,
    )
    ts.is_cjk = True
    return ts


def make_page(
    paragraphs: list[il_version_1.PdfParagraph] | None = None,
    characters: list[il_version_1.PdfCharacter] | None = None,
    figures: list[il_version_1.PdfFigure] | None = None,
    crop: tuple[float, float, float, float] = (0, 0, 612, 792),
) -> il_version_1.Page:
    return il_version_1.Page(
        cropbox=il_version_1.Cropbox(box=Box(*crop)),
        pdf_paragraph=paragraphs or [],
        pdf_character=characters or [],
        pdf_figure=figures or [],
    )


def make_paragraph(box: Box, debug_id: str = "P01-001") -> il_version_1.PdfParagraph:
    return il_version_1.PdfParagraph(debug_id=debug_id, box=box)


class TestExpandedBox:
    @pytest.mark.parametrize("with_figure", [False, True])
    def test_p02_011_top_whitespace_does_not_pull_text_to_page_top(self, with_figure):
        target = Box(306.865, 369.345, 348.155, 376.813)
        page = make_page(
            paragraphs=[make_paragraph(target, "P02-011")],
            characters=[
                il_version_1.PdfCharacter(
                    char_unicode=" ", box=Box(x, 752.428, x + 2, 758.804)
                )
                for x in range(308, 348, 2)
            ],
        )
        if with_figure:
            page.page_layout = [
                il_version_1.PageLayout(
                    class_name="figure", box=Box(308, 410, 553, 740)
                )
            ]
        assert not page.pdf_figure
        expanded = Typesetting._expanded_box(make_typesetting(), target, page)
        assert expanded is not None
        assert expanded.y2 == (410 - EXPAND_VERTICAL_GAP if with_figure else target.y2)
        assert expanded.y == target.y

    @pytest.mark.parametrize("text", [None, "", " ", "\t\n", "\u3000"])
    def test_empty_or_whitespace_characters_are_not_obstacles(self, text):
        target = Box(100, 600, 300, 650)
        page = make_page(characters=[
            il_version_1.PdfCharacter(char_unicode=text, box=Box(100, 700, 300, 710)),
            il_version_1.PdfCharacter(char_unicode="墨", box=None),
        ])
        expanded = Typesetting._expanded_box(make_typesetting(), target, page)
        assert expanded.y2 == target.y2

    def test_non_whitespace_orphan_remains_an_obstacle(self):
        target = Box(100, 600, 300, 650)
        page = make_page(characters=[
            il_version_1.PdfCharacter(char_unicode="墨", box=Box(100, 700, 110, 710))
        ])
        expanded = Typesetting._expanded_box(make_typesetting(), target, page)
        assert expanded.y2 == 700 - EXPAND_VERTICAL_GAP

    @pytest.mark.parametrize("label", ["figure", "table", "formula", "isolate_formula"])
    def test_semantic_regions_block_expansion_without_pdf_figures(self, label):
        target = Box(100, 600, 300, 650)
        page = make_page()
        page.page_layout = [il_version_1.PageLayout(
            class_name=label, box=Box(100, 700, 300, 780)
        )]
        expanded = Typesetting._expanded_box(make_typesetting(), target, page)
        assert expanded.y2 == 700 - EXPAND_VERTICAL_GAP

    @pytest.mark.parametrize("label", [
        "figure_caption", "figure_text", "table_caption", "table_text",
        "table_footnote", "text", "plain text", None,
    ])
    def test_text_regions_do_not_add_whole_region_obstacles(self, label):
        target = Box(100, 600, 300, 650)
        page = make_page()
        page.page_layout = [il_version_1.PageLayout(
            class_name=label, box=Box(100, 700, 300, 780)
        )]
        expanded = Typesetting._expanded_box(make_typesetting(), target, page)
        assert expanded.y2 == target.y2

    def test_missing_region_attributes_are_ignored(self):
        target = Box(100, 600, 300, 650)
        page = make_page()
        page.page_layout = [
            py_types.SimpleNamespace(box=Box(100, 700, 300, 780)),
            py_types.SimpleNamespace(class_name="figure"),
            il_version_1.PageLayout(class_name="figure", box=None),
        ]
        expanded = Typesetting._expanded_box(make_typesetting(), target, page)
        assert expanded.y2 == target.y2

    def test_legacy_and_duplicate_figure_sources_give_same_boundary(self):
        target = Box(100, 600, 300, 650)
        obstacle = Box(100, 700, 300, 780)
        page = make_page(figures=[il_version_1.PdfFigure(box=obstacle)])
        legacy = Typesetting._expanded_box(make_typesetting(), target, page)
        assert legacy.y2 == 700 - EXPAND_VERTICAL_GAP
        page.page_layout = [
            il_version_1.PageLayout(class_name="figure", box=Box(100, 700, 300, 780))
        ]
        assert Typesetting._expanded_box(make_typesetting(), target, page) == legacy

    def test_pulls_to_neighbors_with_gap(self):
        # 目标框 (100, 620, 300, 650)；上方邻居底边 700、下方邻居顶边 600。
        target = Box(x=100, y=620, x2=300, y2=650)
        page = make_page(
            paragraphs=[
                make_paragraph(target),
                make_paragraph(Box(x=100, y=700, x2=300, y2=780), "P01-002"),
                make_paragraph(Box(x=100, y=500, x2=300, y2=600), "P01-003"),
            ]
        )
        expanded = Typesetting._expanded_box(make_typesetting(), target, page)
        assert expanded is not None
        assert expanded.y2 == 700 - EXPAND_VERTICAL_GAP
        assert expanded.y == 600 + EXPAND_VERTICAL_GAP
        assert expanded.x == 100 - EXPAND_HORIZONTAL_TOLERANCE
        assert expanded.x2 == 300 + EXPAND_HORIZONTAL_TOLERANCE

    def test_page_top_box_only_expands_down_to_obstacles(self):
        # 位于页顶的 bbox：上方无可扩空间（不允许扩出页面）；下方离页底
        # 很远且无障碍 → 页底不是合法边界，向下也不扩。
        target = Box(x=100, y=700, x2=300, y2=792)
        page = make_page(paragraphs=[make_paragraph(target)])
        expanded = Typesetting._expanded_box(make_typesetting(), target, page)
        assert expanded is not None
        assert expanded.y2 == 792
        assert expanded.y == 700
        assert expanded.x == 100 - EXPAND_HORIZONTAL_TOLERANCE

    def test_page_bottom_box_only_expands_up_to_obstacles(self):
        # 位于页底的 bbox：下界封在页底（不允许像旧逻辑那样扩出页面底部）；
        # 上方离页顶很远且无障碍 → 不向上扩。
        target = Box(x=100, y=0, x2=300, y2=60)
        page = make_page(paragraphs=[make_paragraph(target)])
        expanded = Typesetting._expanded_box(make_typesetting(), target, page)
        assert expanded is not None
        assert expanded.y == 0
        assert expanded.y2 == 60
        assert expanded.x2 == 300 + EXPAND_HORIZONTAL_TOLERANCE

    def test_far_from_page_edge_no_obstacle_no_vertical_expansion(self):
        # 远离上下页边且无障碍：两个页边都不是合法边界，纵向零扩容
        # （只剩横向 tolerance），防止段落被拉进页边距。
        target = Box(x=100, y=600, x2=300, y2=650)  # 距页顶 142pt、页底 600pt
        page = make_page(paragraphs=[make_paragraph(target)])
        expanded = Typesetting._expanded_box(make_typesetting(), target, page)
        assert expanded is not None
        assert expanded.y == 600
        assert expanded.y2 == 650
        assert expanded.x == 100 - EXPAND_HORIZONTAL_TOLERANCE
        assert expanded.x2 == 300 + EXPAND_HORIZONTAL_TOLERANCE

    def test_near_page_edge_expands_to_edge(self):
        # 已贴近页顶（≤ EXPAND_PAGE_EDGE_MAX_DISTANCE）：页顶是合法边界，
        # 向上扩到页边减阈值间隔。
        y2 = 792 - 10
        target = Box(x=100, y=700, x2=300, y2=y2)
        page = make_page(paragraphs=[make_paragraph(target)])
        expanded = Typesetting._expanded_box(make_typesetting(), target, page)
        assert expanded is not None
        assert expanded.y2 == 792 - EXPAND_VERTICAL_GAP

    def test_horizontal_tolerance_clamped_by_side_neighbors(self):
        # 左右邻居只 clamp 横向 tolerance，不影响纵向扩容。
        target = Box(x=100, y=600, x2=300, y2=650)
        page = make_page(
            paragraphs=[
                make_paragraph(target),
                make_paragraph(Box(x=310, y=600, x2=500, y2=650), "right"),
                make_paragraph(Box(x=20, y=600, x2=99, y2=650), "left"),
                make_paragraph(Box(x=100, y=700, x2=300, y2=780), "above"),
            ]
        )
        expanded = Typesetting._expanded_box(make_typesetting(), target, page)
        assert expanded is not None
        # 右邻居在 310（> 300+tolerance）→ tolerance 全额；左邻居 99 → 只能到 99。
        assert expanded.x2 == 300 + EXPAND_HORIZONTAL_TOLERANCE
        assert expanded.x == 99
        assert expanded.y2 == 700 - EXPAND_VERTICAL_GAP

    def test_intersecting_obstacle_is_ignored(self):
        # 两轴都交叠的障碍（如整页水印框）不应 clamp 任何方向。
        target = Box(x=100, y=600, x2=300, y2=650)
        page = make_page(
            paragraphs=[
                make_paragraph(target),
                make_paragraph(Box(x=0, y=0, x2=612, y2=792), "watermark"),
                make_paragraph(Box(x=100, y=700, x2=300, y2=780), "above"),
            ]
        )
        expanded = Typesetting._expanded_box(make_typesetting(), target, page)
        assert expanded is not None
        assert expanded.y2 == 700 - EXPAND_VERTICAL_GAP
        assert expanded.x == 100 - EXPAND_HORIZONTAL_TOLERANCE
        assert expanded.x2 == 300 + EXPAND_HORIZONTAL_TOLERANCE

    def test_no_room_returns_none(self):
        # 四周都贴着邻居（间隔已等于阈值）时无可扩空间，返回 None。
        target = Box(x=100, y=620, x2=300, y2=650)
        page = make_page(
            paragraphs=[
                make_paragraph(target),
                make_paragraph(Box(x=100, y=652, x2=300, y2=700), "above"),
                make_paragraph(Box(x=100, y=500, x2=300, y2=618), "below"),
                make_paragraph(Box(x=20, y=620, x2=100, y2=650), "left-touch"),
                make_paragraph(Box(x=300, y=620, x2=500, y2=650), "right-touch"),
            ]
        )
        assert Typesetting._expanded_box(make_typesetting(), target, page) is None

    def test_expansion_never_shrinks_any_side(self):
        # 源框横向略超页边、纵向贴近页顶：扩容在可扩方向（向上到页边）
        # 生效，但越出页边的方向不允许把任何一边往内推。
        target = Box(x=-5, y=700, x2=615, y2=785)
        page = make_page(paragraphs=[make_paragraph(target)])
        expanded = Typesetting._expanded_box(make_typesetting(), target, page)
        assert expanded is not None
        assert expanded.x == -5
        assert expanded.y == 700
        assert expanded.x2 == 615
        assert expanded.y2 == 792 - EXPAND_VERTICAL_GAP


class TestExpandBeforeShrink:
    @staticmethod
    def _run_ladder(ts, paragraph, page, fits):
        """stub 掉真实布局：``fits(box, scale)`` 决定是否放得下，记录每次尝试。"""
        attempts: list[tuple[float, float, float]] = []

        def fake_layout(_units, box, scale, _line_skip, _para, _use_english):
            attempts.append((round(box.y, 2), round(box.y2, 2), round(scale, 2)))
            return [], fits(box, scale)

        ts._layout_typesetting_units = fake_layout
        scale, _ = ts._find_optimal_scale_and_layout(
            paragraph, page, [], initial_scale=1.0, apply_layout=True
        )
        return scale, attempts

    def test_expands_then_retries_at_initial_scale(self):
        # 原始高度 50 放不下、扩到 92 放得下：扩容后回到初始字号重试，
        # 最终不缩字，且 paragraph.box 被更新为扩容后的框（上下同时拉满、
        # 与邻居保持阈值间隔）。
        ts = make_typesetting()
        paragraph = make_paragraph(Box(x=100, y=600, x2=300, y2=650))
        page = make_page(
            paragraphs=[
                paragraph,
                make_paragraph(Box(x=100, y=700, x2=300, y2=780), "above"),
                make_paragraph(Box(x=100, y=500, x2=300, y2=580), "below"),
            ]
        )
        scale, attempts = self._run_ladder(
            ts, paragraph, page, fits=lambda box, _s: (box.y2 - box.y) >= 90
        )
        assert scale == 1.0
        assert attempts == [(600, 650, 1.0), (582, 698, 1.0)]
        assert paragraph.box.y == 580 + EXPAND_VERTICAL_GAP
        assert paragraph.box.y2 == 700 - EXPAND_VERTICAL_GAP
        assert paragraph.box.x == 100 - EXPAND_HORIZONTAL_TOLERANCE

    def test_shrinks_only_after_expansion_exhausted(self):
        # 扩容也救不了（fits 只认 scale <= 0.7）：先扩容（远离页边、无障碍
        # → 仅横向 tolerance，纵向不变）并在初始字号重试一次，再进入缩字
        # 阶梯。
        ts = make_typesetting()
        paragraph = make_paragraph(Box(x=100, y=600, x2=300, y2=650))
        page = make_page(paragraphs=[paragraph])
        scale, attempts = self._run_ladder(
            ts, paragraph, page, fits=lambda _box, s: round(s, 2) <= 0.7
        )
        assert round(scale, 2) == 0.7
        # 第二次尝试：同一 scale=1.0、box 纵向不变（只加了横向 tolerance）。
        assert attempts[0] == (600, 650, 1.0)
        assert attempts[1] == (600, 650, 1.0)
        assert [a[2] for a in attempts[2:]] == [
            round(1.0 - 0.05 * i, 2) for i in range(1, 7)
        ]

    def test_no_room_ladder_on_original_box(self):
        # 四周贴死无扩容空间：直接在原始 box 上走缩字阶梯，box 不变。
        ts = make_typesetting()
        paragraph = make_paragraph(Box(x=100, y=620, x2=300, y2=650))
        page = make_page(
            paragraphs=[
                paragraph,
                make_paragraph(Box(x=100, y=652, x2=300, y2=700), "above"),
                make_paragraph(Box(x=100, y=500, x2=300, y2=618), "below"),
                make_paragraph(Box(x=20, y=620, x2=100, y2=650), "left"),
                make_paragraph(Box(x=300, y=620, x2=500, y2=650), "right"),
            ]
        )
        scale, attempts = self._run_ladder(
            ts, paragraph, page, fits=lambda _box, s: round(s, 2) <= 0.8
        )
        assert round(scale, 2) == 0.8
        assert all(a[0] == 620 and a[1] == 650 for a in attempts)
        assert paragraph.box.y == 620 and paragraph.box.y2 == 650
