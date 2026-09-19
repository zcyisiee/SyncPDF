"""(P6) 编译后按译文版面扩框：纯几何、检测器映射与排版编排。

不加载真实模型：检测器用注入的 predictor / stub 替换，编排用合成 PDF。
"""

from __future__ import annotations

import numpy as np
import pymupdf
import pytest
from babeldoc.docvision.paddle_layout_regions import PaddleLayoutRegions
from babeldoc.docvision.paddle_layout_regions import Region
from babeldoc.tools.agent import layout_refine


class _StubDetector:
    """只提供 ``available`` / ``detect_page`` 的检测器替身。"""

    available = True

    def __init__(self, regions):
        self.regions = list(regions)
        self.pages: list[int] = []

    def detect_page(self, page):
        self.pages.append(page.number)
        return list(self.regions)


def _page_pdf(tmp_path, width=300, height=200):
    path = tmp_path / "page.pdf"
    with pymupdf.open() as doc:
        doc.new_page(width=width, height=height)
        doc.save(path)
    return path


def _page_with_heading(tmp_path):
    """一页：正文段 + 它正上方一条**短**小标题（页 400x300，返回路径）。"""
    path = tmp_path / "heading.pdf"
    with pymupdf.open() as doc:
        page = doc.new_page(width=400, height=300)
        page.insert_text((25, 190), "正文译文一行", fontsize=10)
        page.insert_text((25, 160), "4 结论", fontsize=14)
        doc.save(path)
    return path


# --------------------------------------------------------------------------- #
# 纯几何
# --------------------------------------------------------------------------- #
class TestPlanExpansion:
    #: 框 (20,100)-(200,130)，自身墨迹盒在框内。
    BOX = (20.0, 100.0, 200.0, 130.0)
    SELF = (20.0, 102.0, 200.0, 128.0)

    def test_expands_to_nearest_obstacle_below(self):
        obstacles = [self.SELF, (20.0, 40.0, 200.0, 70.0)]
        expanded, reason = layout_refine.plan_expansion(
            self.BOX, obstacles, page_bottom=0.0
        )
        assert reason is None
        assert expanded == (20.0, 71.0, 200.0, 130.0)

    def test_expands_to_page_bottom_without_obstacle(self):
        expanded, reason = layout_refine.plan_expansion(
            self.BOX, [self.SELF], page_bottom=0.0
        )
        assert reason is None
        assert expanded == (20.0, 0.0, 200.0, 130.0)

    def test_ignores_other_column(self):
        """另一栏的区域（横向不重叠）不构成下方障碍。"""
        obstacles = [self.SELF, (400.0, 40.0, 600.0, 70.0)]
        expanded, _ = layout_refine.plan_expansion(
            self.BOX, obstacles, page_bottom=0.0
        )
        assert expanded == (20.0, 0.0, 200.0, 130.0)

    def test_refuses_when_foreign_ink_overlaps_box(self):
        """框内已有别人墨迹（竖向交叠的非自身区域）→ 不扩。"""
        obstacles = [self.SELF, (20.0, 90.0, 200.0, 160.0)]
        expanded, reason = layout_refine.plan_expansion(
            self.BOX, obstacles, page_bottom=0.0
        )
        assert expanded is None
        assert reason in (layout_refine.SKIP_NO_SELF, layout_refine.SKIP_UNSAFE)

    def test_refuses_without_identifiable_self_region(self):
        """本段与邻居被检测成一块（区域伸出框外）→ 不扩。"""
        obstacles = [(20.0, 40.0, 200.0, 130.0)]
        expanded, reason = layout_refine.plan_expansion(
            self.BOX, obstacles, page_bottom=0.0
        )
        assert expanded is None
        assert reason == layout_refine.SKIP_NO_SELF

    def test_no_room_below(self):
        obstacles = [self.SELF, (20.0, 96.0, 200.0, 99.0)]
        expanded, reason = layout_refine.plan_expansion(
            self.BOX, obstacles, page_bottom=0.0
        )
        assert expanded is None
        assert reason == layout_refine.SKIP_NO_ROOM

    def test_gap_keeps_clearance_above_obstacle(self):
        obstacles = [self.SELF, (20.0, 40.0, 200.0, 70.0)]
        expanded, _ = layout_refine.plan_expansion(self.BOX, obstacles, page_bottom=0.0)
        assert expanded[1] - 70.0 == pytest.approx(layout_refine.EXPAND_GAP_PT)


class TestPlanUpwardExpansion:
    #: 同 TestPlanExpansion 的框，只是扩的方向朝上。
    BOX = TestPlanExpansion.BOX
    SELF = TestPlanExpansion.SELF
    PAGE_TOP = 300.0

    def _up(self, obstacles, **kwargs):
        return layout_refine.plan_expansion(
            self.BOX,
            obstacles,
            direction=layout_refine.DIRECTION_UP,
            page_top=self.PAGE_TOP,
            **kwargs,
        )

    def test_expands_to_nearest_obstacle_above(self):
        obstacles = [self.SELF, (20.0, 160.0, 200.0, 190.0)]
        expanded, reason = self._up(obstacles)
        assert reason is None
        assert expanded == (20.0, 100.0, 200.0, 159.0)

    def test_expands_to_page_top_without_obstacle(self):
        expanded, reason = self._up([self.SELF])
        assert reason is None
        assert expanded == (20.0, 100.0, 200.0, 300.0)

    def test_ignores_other_column(self):
        """另一栏的区域（横向不重叠）不构成上方障碍。"""
        expanded, _ = self._up([self.SELF, (400.0, 160.0, 600.0, 190.0)])
        assert expanded == (20.0, 100.0, 200.0, 300.0)

    def test_refuses_when_foreign_ink_overlaps_box(self):
        expanded, reason = self._up([self.SELF, (20.0, 90.0, 200.0, 160.0)])
        assert expanded is None
        assert reason in (layout_refine.SKIP_NO_SELF, layout_refine.SKIP_UNSAFE)

    def test_no_room_above(self):
        obstacles = [self.SELF, (20.0, 132.0, 200.0, 140.0)]
        expanded, reason = self._up(obstacles)
        assert expanded is None
        assert reason == layout_refine.SKIP_NO_ROOM

    def test_gap_keeps_clearance_below_obstacle(self):
        obstacles = [self.SELF, (20.0, 160.0, 200.0, 190.0)]
        expanded, _ = self._up(obstacles)
        assert 160.0 - expanded[3] == pytest.approx(layout_refine.EXPAND_GAP_PT)

    def test_requires_page_top(self):
        with pytest.raises(ValueError, match="page_top"):
            layout_refine.plan_expansion(
                self.BOX, [self.SELF], direction=layout_refine.DIRECTION_UP
            )

    def test_rejects_unknown_direction(self):
        with pytest.raises(ValueError, match="方向"):
            layout_refine.plan_expansion(self.BOX, [self.SELF], direction="sideways")

    def test_wrapper_matches_plan_expansion(self):
        obstacles = [self.SELF, (20.0, 160.0, 200.0, 190.0)]
        assert layout_refine.plan_upward_expansion(
            self.BOX, obstacles, page_top=self.PAGE_TOP
        ) == (20.0, 100.0, 200.0, 159.0)

    def test_short_region_above_counts_as_obstacle(self):
        """同栏短区域（小标题）也是障碍：横向重叠按**窄边**算比例。

        实测回归：45pt 宽的小标题压在 239pt 宽的段落上只有 18% 重叠，按框宽算会被
        当成另一栏，向上扩就直接跨过标题。
        """
        heading = (20.0, 150.0, 65.0, 165.0)
        expanded, reason = self._up([self.SELF, heading])
        assert reason is None
        assert expanded == (20.0, 100.0, 200.0, 149.0)

    def test_short_region_below_counts_as_obstacle(self):
        caption = (20.0, 60.0, 65.0, 75.0)
        expanded, reason = layout_refine.plan_expansion(
            self.BOX, [self.SELF, caption], page_bottom=0.0
        )
        assert reason is None
        assert expanded == (20.0, 76.0, 200.0, 130.0)


class TestPlanWidenExpansion:
    #: 同 TestPlanExpansion 的框，只是扩的方向朝右/左（页宽 400）。
    BOX = TestPlanExpansion.BOX
    SELF = TestPlanExpansion.SELF
    PAGE_RIGHT = 400.0

    def _widen(self, regions, **kwargs):
        return layout_refine.plan_widen_expansion(
            self.BOX, regions, page_right=self.PAGE_RIGHT, **kwargs
        )

    def test_widens_right_to_neighbor_left_edge_minus_gap(self):
        """右邻栏有文字（纵向交叠）→ 收边到它左沿减 EXPAND_GAP_PT。"""
        regions = [self.SELF, (220.0, 100.0, 380.0, 130.0)]
        expanded, reason = self._widen(regions)
        assert reason is None
        assert expanded == (20.0, 100.0, 219.0, 130.0)

    def test_widens_right_to_page_edge_when_free(self):
        expanded, reason = self._widen([self.SELF])
        assert reason is None
        assert expanded == (20.0, 100.0, 400.0, 130.0)

    def test_ink_limits_right_widening(self):
        expanded, reason = self._widen([self.SELF], ink=[(240.0, 105.0, 300.0, 125.0)])
        assert reason is None
        assert expanded == (20.0, 100.0, 239.0, 130.0)

    def test_refuses_when_rect_overlaps_box_horizontally(self):
        """纵向交叠且与框横向交叠 → 框内已有别人墨迹。"""
        expanded, reason = self._widen([self.SELF, (150.0, 110.0, 260.0, 120.0)])
        assert expanded is None
        assert reason == layout_refine.SKIP_UNSAFE

    def test_slight_vertical_overlap_neighbor_is_unsafe(self):
        """邻栏文字只与框纵向轻微交叠、横向重叠远低于同栏比例 → 仍不安全。

        纵向扩框可以用 ``X_OVERLAP_RATIO`` 忽略轻微交叠的邻栏；横向扩框扩过去
        就是物理碰撞，必须按「任何纵向交叠」处理。
        """
        neighbor = (190.0, 110.0, 380.0, 125.0)  # 与框只重叠 10pt（约 5%）
        expanded, reason = self._widen([self.SELF, neighbor])
        assert expanded is None
        assert reason == layout_refine.SKIP_UNSAFE

    def test_no_room_right(self):
        regions = [self.SELF, (204.0, 100.0, 380.0, 130.0)]
        expanded, reason = self._widen(regions)
        assert expanded is None
        assert reason == layout_refine.SKIP_NO_ROOM

    def test_widens_left_symmetrically(self):
        """向左扩：收边到左邻右沿加 GAP，对称于向右。"""
        regions = [self.SELF, (5.0, 100.0, 12.0, 130.0)]
        expanded, reason = self._widen(regions, direction=layout_refine.DIRECTION_LEFT)
        assert reason is None
        assert expanded == (13.0, 100.0, 200.0, 130.0)

    def test_far_rect_without_vertical_overlap_is_ignored(self):
        """纵向不交叠的远处矩形（物理上碰不到）不阻碍横向扩。"""
        regions = [self.SELF, (220.0, 200.0, 380.0, 260.0)]
        expanded, reason = self._widen(regions)
        assert reason is None
        assert expanded == (20.0, 100.0, 400.0, 130.0)

    def test_refuses_when_self_region_spans_columns(self):
        """自域在横向伸出框外（本段 + 邻栏被合并成一块）→ 放弃。"""
        expanded, reason = self._widen([(20.0, 102.0, 380.0, 128.0)])
        assert expanded is None
        assert reason == layout_refine.SKIP_NO_SELF

    def test_rejects_unknown_direction(self):
        with pytest.raises(ValueError, match="方向"):
            layout_refine.plan_widen_expansion(
                self.BOX, [self.SELF], page_right=400.0, direction="up"
            )


class TestPlanNextPageFloatCore:
    #: 只用 x 范围 20..200；下一页页高 300。
    BOX = (20.0, 100.0, 200.0, 130.0)
    PAGE_HEIGHT = 300.0

    def _float(self, obstacles, required_height):
        return layout_refine.plan_next_page_float_core(
            self.BOX,
            obstacles,
            page_height=self.PAGE_HEIGHT,
            required_height=required_height,
        )

    def test_floats_below_top_heading_top_aligned(self):
        """顶端被标题占据 → 落到标题下方、顶对齐标题下沿。"""
        heading = (20.0, 250.0, 200.0, 300.0)
        floated = self._float([heading], required_height=40.0)
        assert floated == (20.0, 210.0, 200.0, 250.0)

    def test_picks_topmost_interval_that_fits(self):
        """多个能容纳的区间 → 取最靠上的那个（顶对齐它的上沿）。"""
        heading = (20.0, 250.0, 200.0, 300.0)
        figure = (20.0, 100.0, 200.0, 140.0)
        floated = self._float([heading, figure], required_height=30.0)
        assert floated == (20.0, 220.0, 200.0, 250.0)

    def test_returns_none_when_no_interval_fits(self):
        obstacles = [(20.0, 250.0, 200.0, 300.0), (20.0, 0.0, 200.0, 180.0)]
        assert self._float(obstacles, required_height=80.0) is None

    def test_no_obstacles_floats_to_page_top(self):
        """无障碍（异栏矩形不算）→ 贴页顶。"""
        off_column = (250.0, 0.0, 450.0, 300.0)
        assert self._float([], required_height=50.0) == (20.0, 250.0, 200.0, 300.0)
        assert self._float([off_column], 50.0) == (20.0, 250.0, 200.0, 300.0)

    def test_sticky_tolerance_on_x_projection(self):
        """x 投影带 TOUCH_TOLERANCE 粘连：贴着的矩形算障碍，稍远的不算。"""
        touching = (201.5, 0.0, 300.0, 300.0)
        apart = (203.0, 0.0, 300.0, 300.0)
        assert self._float([touching], 50.0) is None
        assert self._float([apart], 50.0) == (20.0, 250.0, 200.0, 300.0)

    def test_non_positive_required_height_returns_none(self):
        assert self._float([], required_height=0.0) is None
        assert self._float([], required_height=-3.0) is None


class TestPageInkRects:
    BOX = TestPlanExpansion.BOX
    SELF = TestPlanExpansion.SELF

    def test_extracts_text_ink_in_il_coords(self, tmp_path):
        with pymupdf.open(_page_with_heading(tmp_path)) as pdf:
            ink = layout_refine.page_ink_rects(pdf[0])
        assert len(ink) == 2
        body, heading = sorted(ink, key=lambda box: box[1])
        # 页高 300、字号 10、baseline 190 → 墨迹约在 IL y 107..120。
        assert 100.0 < body[1] < 120.0 and body[3] > body[1]
        # 标题字号更大、位置更高 → IL y 更大。
        assert heading[1] > body[3]
        assert (heading[3] - heading[1]) < (heading[2] - heading[0])

    def test_foreign_ink_drops_self(self, tmp_path):
        with pymupdf.open(_page_with_heading(tmp_path)) as pdf:
            ink = layout_refine.page_ink_rects(pdf[0])
        foreign = layout_refine.foreign_ink(ink, self.BOX)
        # 只有标题是「别人的墨迹」，正文段自己的墨迹被排除。
        assert len(foreign) == 1
        assert foreign[0][1] > self.BOX[3]

    def test_broken_page_is_treated_as_no_ink(self):
        class _Broken:
            rect = pymupdf.Rect(0, 0, 100, 100)

            def get_text(self, _kind):
                raise RuntimeError("boom")

        assert layout_refine.page_ink_rects(_Broken()) == []

    def test_ink_limits_upward_expansion(self):
        """区域检测漏了标题时，精确墨迹仍把边界拦在标题下沿。"""
        ink = [
            (20.0, 140.0, 65.0, 150.0),  # 标题墨迹
            (20.0, 110.0, 200.0, 120.0),  # 本段自己的墨迹
        ]
        expanded, reason = layout_refine.plan_expansion(
            self.BOX,
            [self.SELF],
            ink=layout_refine.foreign_ink(ink, self.BOX),
            direction=layout_refine.DIRECTION_UP,
            page_top=300.0,
        )
        assert reason is None
        assert expanded == (20.0, 100.0, 200.0, 139.0)

    def test_ink_limits_downward_expansion(self):
        ink = [
            (20.0, 80.0, 200.0, 90.0),  # 下方的别人墨迹
            (20.0, 110.0, 200.0, 120.0),  # 本段自己的
        ]
        expanded, reason = layout_refine.plan_expansion(
            self.BOX,
            [self.SELF],
            ink=layout_refine.foreign_ink(ink, self.BOX),
        )
        assert reason is None
        assert expanded == (20.0, 91.0, 200.0, 130.0)


class TestPlanBuildInkGuard:
    """区域检测漏检时，第二遍仍不会把段扩到别人的文字上。"""

    REPORT = {
        "mode": "full",
        "decisions": [
            {"debug_id": "P1", "page": 0, "reason": "applied", "font_scale": 0.7}
        ],
    }
    GEOMETRY = {
        "paragraphs": [{"id": "P1", "page": 1, "src_box": [20.0, 100.0, 200.0, 130.0]}]
    }
    #: 只把正文段当区域上报，故意「漏掉」上面的小标题；框下沿再堵一块，
    #: 让向下扩没有净空，逼出向上扩那条支路。
    REGIONS = [
        Region("text", 0.9, (20.0, 102.0, 200.0, 128.0)),
        Region("text", 0.9, (20.0, 96.0, 200.0, 99.0)),
    ]

    def test_upward_expansion_stops_below_undetected_heading(self, tmp_path):
        pdf_path = _page_with_heading(tmp_path)
        result = layout_refine.plan_build_refinement(
            report=self.REPORT,
            geometry=self.GEOMETRY,
            pdf_path=pdf_path,
            detector=_StubDetector(self.REGIONS),
        )
        expanded = result.overrides["P1"]
        box = tuple(self.GEOMETRY["paragraphs"][0]["src_box"])
        with pymupdf.open(pdf_path) as pdf:
            foreign = layout_refine.foreign_ink(
                layout_refine.page_ink_rects(pdf[0]), box
            )
        assert foreign, "页面上应能提到标题墨迹"
        assert expanded[1] == box[1], "向下没净空，只该向上"
        assert expanded[3] > box[3]  # 确实向上扩了
        for rect in foreign:
            assert not layout_refine._intersects(tuple(expanded), rect)


# --------------------------------------------------------------------------- #
# 缩字判定
# --------------------------------------------------------------------------- #
class TestExpansionReason:
    def test_font_shrink(self):
        assert (
            layout_refine.expansion_reason(scale=0.8, ok=True, reason="applied")
            == "font-shrink"
        )

    def test_no_shrink(self):
        assert (
            layout_refine.expansion_reason(scale=1.0, ok=True, reason="applied") is None
        )

    def test_overflow_failure(self):
        assert (
            layout_refine.expansion_reason(
                scale=None, ok=False, reason="compile:s0:vertical-overflow"
            )
            == "overflow"
        )

    def test_other_failure_not_targeted(self):
        assert (
            layout_refine.expansion_reason(
                scale=None, ok=False, reason="compile:TeX-error"
            )
            is None
        )

    def test_shrink_targets_reads_decisions(self):
        report = {
            "mode": "full",
            "decisions": [
                {"debug_id": "P1", "reason": "applied", "font_scale": 0.7},
                {"debug_id": "P2", "reason": "applied", "font_scale": 1.0},
                {"debug_id": "P3", "reason": "compile:s0:overfull-vbox"},
                {"debug_id": "P4", "reason": "single-line"},
            ],
        }
        assert layout_refine.shrink_targets(report) == {
            "P1": "font-shrink",
            "P3": "overflow",
        }


# --------------------------------------------------------------------------- #
# 检测器坐标映射（不碰模型）
# --------------------------------------------------------------------------- #
class TestDetector:
    def test_detect_image_maps_pixels_to_il(self):
        # 页 200x100pt，图 400x200px（非等比会被各自缩放）；框 px [40,20,120,50]。
        predictor = lambda _image: [  # noqa: E731 - 测试内联
            {"label": "text", "score": 0.9, "coordinate": [40, 20, 120, 50]}
        ]
        detector = PaddleLayoutRegions(predictor=predictor)
        regions = detector.detect_image(
            np.zeros((200, 400, 3), dtype=np.uint8), page_width=200.0, page_height=100.0
        )
        assert len(regions) == 1
        region = regions[0]
        assert region.label == "text"
        assert region.box == pytest.approx((20.0, 75.0, 60.0, 90.0))

    def test_detect_image_skips_malformed(self):
        predictor = lambda _image: [  # noqa: E731
            {"label": "text", "score": 0.9, "coordinate": [10, 10, 10, 10]},
            {"label": "text", "score": 0.9},
            {"label": "ok", "score": 0.5, "coordinate": [0, 0, 10, 10]},
        ]
        detector = PaddleLayoutRegions(predictor=predictor)
        regions = detector.detect_image(
            np.zeros((100, 100, 3), dtype=np.uint8), page_width=100.0, page_height=100.0
        )
        assert [region.label for region in regions] == ["ok"]

    def test_available_false_without_model_or_predictor(self, tmp_path):
        detector = PaddleLayoutRegions(model_path=tmp_path / "missing.onnx")
        assert detector.available is False

    def test_available_true_with_predictor(self):
        assert PaddleLayoutRegions(predictor=lambda _image: []).available is True


# --------------------------------------------------------------------------- #
# 一次性编译第二遍的编排
# --------------------------------------------------------------------------- #
class TestPlanBuildRefinement:
    REPORT = {
        "mode": "full",
        "decisions": [
            {"debug_id": "P1", "page": 0, "reason": "applied", "font_scale": 0.7},
            {"debug_id": "P2", "page": 0, "reason": "applied", "font_scale": 1.0},
        ],
    }
    GEOMETRY = {
        "paragraphs": [
            {"id": "P1", "page": 1, "src_box": [20.0, 100.0, 200.0, 130.0]},
            {"id": "P2", "page": 1, "src_box": [20.0, 40.0, 200.0, 70.0]},
        ]
    }

    def test_expands_only_shrunk_paragraph(self, tmp_path):
        regions = [
            Region("text", 0.9, (20.0, 102.0, 200.0, 128.0)),
            Region("text", 0.9, (20.0, 42.0, 200.0, 68.0)),
        ]
        detector = _StubDetector(regions)
        result = layout_refine.plan_build_refinement(
            report=self.REPORT,
            geometry=self.GEOMETRY,
            pdf_path=_page_pdf(tmp_path),
            detector=detector,
        )
        assert result.targets == 1
        assert result.overrides == {"P1": [20.0, 69.0, 200.0, 130.0]}

    def test_greedy_claims_do_not_collide(self, tmp_path):
        """两段都缩字：上段先扩，下段不会抢同一段净空。"""
        report = {
            "mode": "full",
            "decisions": [
                {"debug_id": "P1", "page": 0, "reason": "applied", "font_scale": 0.7},
                {"debug_id": "P2", "page": 0, "reason": "applied", "font_scale": 0.7},
            ],
        }
        regions = [
            Region("text", 0.9, (20.0, 102.0, 200.0, 128.0)),
            Region("text", 0.9, (20.0, 42.0, 200.0, 68.0)),
        ]
        result = layout_refine.plan_build_refinement(
            report=report,
            geometry=self.GEOMETRY,
            pdf_path=_page_pdf(tmp_path),
            detector=_StubDetector(regions),
        )
        assert result.overrides["P1"] == [20.0, 69.0, 200.0, 130.0]
        assert result.overrides["P2"] == [20.0, 0.0, 200.0, 70.0]

    def test_repair_mode_is_untouched(self, tmp_path):
        report = dict(self.REPORT, mode="repair")
        result = layout_refine.plan_build_refinement(
            report=report,
            geometry=self.GEOMETRY,
            pdf_path=_page_pdf(tmp_path),
            detector=_StubDetector([]),
        )
        assert result.overrides == {}
        assert result.targets == 0

    def test_missing_geometry_is_reported(self, tmp_path):
        result = layout_refine.plan_build_refinement(
            report=self.REPORT,
            geometry={"paragraphs": []},
            pdf_path=_page_pdf(tmp_path),
            detector=_StubDetector([]),
        )
        assert result.overrides == {}
        assert result.skipped.get("missing-geometry") == 1

    def test_unavailable_detector_is_noop(self, tmp_path):
        class _Unavailable:
            available = False

            def detect_page(self, _page):  # pragma: no cover - 不该被调用
                raise AssertionError("不可用时不应检测")

        result = layout_refine.plan_build_refinement(
            report=self.REPORT,
            geometry=self.GEOMETRY,
            pdf_path=_page_pdf(tmp_path),
            detector=_Unavailable(),
        )
        assert result.overrides == {}
        assert result.targets == 1


class TestPlanBuildUpwardFallback:
    """向下没净空时改向上；两段不会抢同一段净空。"""

    REPORT = {
        "mode": "full",
        "decisions": [
            {"debug_id": "P1", "page": 0, "reason": "applied", "font_scale": 0.7}
        ],
    }
    GEOMETRY = {
        "paragraphs": [
            {"id": "P1", "page": 1, "src_box": [20.0, 100.0, 200.0, 130.0]},
        ]
    }

    def test_falls_back_to_upward(self, tmp_path):
        regions = [
            Region("text", 0.9, (20.0, 102.0, 200.0, 128.0)),
            # 紧贴框下沿：向下扩不足 MIN_GAIN_PT。
            Region("text", 0.9, (20.0, 96.0, 200.0, 99.0)),
            Region("text", 0.9, (20.0, 170.0, 200.0, 200.0)),
        ]
        result = layout_refine.plan_build_refinement(
            report=self.REPORT,
            geometry=self.GEOMETRY,
            pdf_path=_page_pdf(tmp_path),
            detector=_StubDetector(regions),
        )
        assert result.overrides == {"P1": [20.0, 100.0, 200.0, 169.0]}
        assert result.skipped == {}

    def test_reports_downward_reason_when_both_directions_fail(self, tmp_path):
        regions = [
            Region("text", 0.9, (20.0, 102.0, 200.0, 128.0)),
            Region("text", 0.9, (20.0, 96.0, 200.0, 99.0)),
            Region("text", 0.9, (20.0, 132.0, 200.0, 200.0)),
        ]
        result = layout_refine.plan_build_refinement(
            report=self.REPORT,
            geometry=self.GEOMETRY,
            pdf_path=_page_pdf(tmp_path),
            detector=_StubDetector(regions),
        )
        assert result.overrides == {}
        assert result.skipped == {layout_refine.SKIP_NO_ROOM: 1}

    def test_downward_wins_shared_gap(self, tmp_path):
        """上段向下吃掉段间净空（向下优先）；下段仍按向下扩到页底。"""
        report = {
            "mode": "full",
            "decisions": [
                {"debug_id": "P1", "page": 0, "reason": "applied", "font_scale": 0.7},
                {"debug_id": "P2", "page": 0, "reason": "applied", "font_scale": 0.7},
            ],
        }
        geometry = {
            "paragraphs": [
                {"id": "P1", "page": 1, "src_box": [20.0, 60.0, 200.0, 90.0]},
                {"id": "P2", "page": 1, "src_box": [20.0, 100.0, 200.0, 130.0]},
            ]
        }
        regions = [
            Region("text", 0.9, (20.0, 62.0, 200.0, 88.0)),
            Region("text", 0.9, (20.0, 102.0, 200.0, 128.0)),
        ]
        result = layout_refine.plan_build_refinement(
            report=report,
            geometry=geometry,
            pdf_path=_page_pdf(tmp_path),
            detector=_StubDetector(regions),
        )
        # 上段（P2）先向下扩到下一段墨迹上方 1pt；下段（P1）扩到页底。
        assert result.overrides == {
            "P2": [20.0, 89.0, 200.0, 130.0],
            "P1": [20.0, 0.0, 200.0, 90.0],
        }


class TestPlanPageExpansion:
    def test_uses_page_regions(self, tmp_path):
        regions = [
            Region("text", 0.9, (20.0, 102.0, 200.0, 128.0)),
            Region("text", 0.9, (20.0, 42.0, 200.0, 68.0)),
        ]
        with pymupdf.open(_page_pdf(tmp_path)) as pdf:
            expanded = layout_refine.plan_page_expansion(
                pdf[0], (20.0, 100.0, 200.0, 130.0), _StubDetector(regions)
            )
        assert expanded == (20.0, 69.0, 200.0, 130.0)

    def test_unavailable_detector_returns_none(self, tmp_path):
        class _Unavailable:
            available = False

        with pymupdf.open(_page_pdf(tmp_path)) as pdf:
            expanded = layout_refine.plan_page_expansion(
                pdf[0], (20.0, 100.0, 200.0, 130.0), _Unavailable()
            )
        assert expanded is None

    def test_falls_back_to_upward_and_detects_once(self, tmp_path):
        """向下没净空时改向上；两个方向共用一次检测（页高 200）。"""
        regions = [
            Region("text", 0.9, (20.0, 102.0, 200.0, 128.0)),
            Region("text", 0.9, (20.0, 96.0, 200.0, 99.0)),
        ]
        detector = _StubDetector(regions)
        with pymupdf.open(_page_pdf(tmp_path)) as pdf:
            expanded = layout_refine.plan_page_expansion(
                pdf[0], (20.0, 100.0, 200.0, 130.0), detector
            )
        assert expanded == (20.0, 100.0, 200.0, 200.0)
        assert detector.pages == [0]


class TestPlanNextPageFloat:
    """薄封装：区域 + 精确墨迹并集作障碍；无区域 → None。"""

    BOX = (20.0, 100.0, 200.0, 130.0)

    def test_floats_below_region_on_next_page(self, tmp_path):
        regions = [Region("text", 0.9, (20.0, 250.0, 200.0, 300.0))]
        with pymupdf.open(_page_pdf(tmp_path, width=300, height=300)) as pdf:
            floated = layout_refine.plan_next_page_float(
                pdf[0], self.BOX, _StubDetector(regions), required_height=40.0
            )
        assert floated == (20.0, 210.0, 200.0, 250.0)

    def test_no_regions_returns_none(self, tmp_path):
        with pymupdf.open(_page_pdf(tmp_path)) as pdf:
            floated = layout_refine.plan_next_page_float(
                pdf[0], self.BOX, _StubDetector([]), required_height=40.0
            )
        assert floated is None


# --------------------------------------------------------------------------- #
# 排版工具层的第二遍接线
# --------------------------------------------------------------------------- #
class TestReconstructRefineWiring:
    def _workdir(self, tmp_path):
        workdir = tmp_path / "wd"
        (workdir / "agent").mkdir(parents=True)
        return workdir

    def _fake_reconstruct(self, calls):
        def fake(workdir, **kwargs):
            calls.append(kwargs.get("latex_box_overrides"))
            return {
                "mono_pdf": str(workdir) + "/output/mono.pdf",
                "dual_pdf": None,
                "layout_geometry": str(workdir) + "/agent/layout_geometry.json",
                "latex_bbox_report": str(workdir) + "/latex_bbox_report.json",
            }

        return fake

    def test_runs_second_pass_and_reports_evidence(self, tmp_path, monkeypatch):
        from babeldoc.tools.agent import workflow
        from babeldoc_tools import layout

        workdir = self._workdir(tmp_path)
        calls: list = []
        monkeypatch.setattr(workflow, "reconstruct", self._fake_reconstruct(calls))
        monkeypatch.setattr(
            layout,
            "_refine_tight_boxes",
            lambda *_args, **_kwargs: layout_refine.RefineResult(
                overrides={"P1": [20.0, 60.0, 200.0, 130.0]}, targets=1, skipped={}
            ),
        )

        result = layout.reconstruct_pdf(str(workdir), stats=False)

        assert calls == [None, {"P1": [20.0, 60.0, 200.0, 130.0]}]
        assert result["latex_refine"]["expanded"] == 1
        assert result["latex_refine"]["boxes"] == {"P1": [20.0, 60.0, 200.0, 130.0]}

    def test_single_pass_without_expansions(self, tmp_path, monkeypatch):
        from babeldoc.tools.agent import workflow
        from babeldoc_tools import layout

        workdir = self._workdir(tmp_path)
        calls: list = []
        monkeypatch.setattr(workflow, "reconstruct", self._fake_reconstruct(calls))
        monkeypatch.setattr(
            layout,
            "_refine_tight_boxes",
            lambda *_args, **_kwargs: layout_refine.RefineResult(
                overrides={}, targets=0, skipped={}
            ),
        )

        result = layout.reconstruct_pdf(str(workdir), stats=False)

        assert calls == [None]
        assert result["latex_refine"]["expanded"] == 0
