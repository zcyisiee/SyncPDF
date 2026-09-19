"""编译后按译文版面扩框。

背景：LaTeX bbox 渲染器在原框里放不下译文时会按阶梯缩字号（``renderer.py``），
排版随之变丑；而邻居的译文往往比源文短，框内空出来的地方本可以用。

既有 ``overlay._expand_vertical_failures`` 只在**源**版面里找净空（读
``source_geometry.space_below_pt``、在源 PDF 上判定），看不到译文实际占用了
多少。本模块补上这一步：对**已编译的译文页**用 PP-DocLayoutV3 检测实际墨迹
区域，算出目标段下方还能扩多少。

设计边界（YAGNI）：

- **两个方向都扩，但向下优先**：先给同一页所有目标做向下扩，没有净空的再向上；
  障碍集里带着刚下扩过的框，所以相邻两段不会抢同一段净空。顺序按坐标定（
  ``_downward_order`` / ``_upward_order``），同一输入的结果可复现。
- **向上扩要求首行量测钉在原框口径上**：``\topskip`` = 首行字顶 − 框顶。若它跟着
  新框顶一起涨，多出来的高度就被首行 skip 吃掉，可用行数一点不变——向上扩等于
  白扩。一次性编译那条路径的口径在 ``overlay._select_candidates``（量测 clip 与
  ``_rows_geometry`` 都用原框）；serve 路径的首行几何来自持久化的源行量测，
  本来就是原框口径。
- **障碍 = 检测区域（粗）+ pymupdf 精确墨迹（兜底）**，不做语义判定：某条带上有没有
  东西才是扩框唯一需要的信息。同栏判定按「窄边」算比例——实测一个 45pt 宽的小标题
  压在 239pt 宽的段落上只有 18% 横向重叠，按框宽算会被当成另一栏，向上扩就直接
  跨过标题（区域检测本身也可能漏检，所以再加上精确墨迹托底）。
- 检测器不可用（模型/依赖缺失）时返回空结果，调用方保持原行为。
- **横向扩框与跨页整框迁移目前只是纯几何规划**（``plan_widen_expansion`` /
  ``plan_next_page_float``）：同栏上下都没有净空时，前者把框向左/右延伸进相邻
  空闲带，后者在下一页同 x 范围自上而下找第一个能容纳所需高度的空闲区间（顶
  对齐）。横向扩的障碍按「任何纵向交叠」判定，不能用 ``X_OVERLAP_RATIO`` 那套
  同栏比例——邻栏文字只要与框纵向轻微交叠，横向扩过去就是物理碰撞。两者尚未
  接入编译编排（block_compile/渲染集成是后续任务）。
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from babeldoc.docvision.paddle_layout_regions import PaddleLayoutRegions
from babeldoc.docvision.paddle_layout_regions import Region

logger = logging.getLogger(__name__)

#: 缩字判定：``stamp.scale``（字号 / 源字号）低于它即认为被缩过。
SHRINK_RATIO = 0.995
#: 最小可扩高度（pt）：低于它不值得为一次重编译/重排版付代价。
MIN_GAIN_PT = 4.0
#: 扩到邻居墨迹上沿时保留的净空（pt）。
EXPAND_GAP_PT = 1.0
#: 横向重叠下限：重叠小于「窄边」该比例的区域算另一栏，不构成上下障碍。
#: 用窄边（不是框宽）是因为同栏的短区域很常见：实测一个 45pt 宽的小标题压在
#: 239pt 宽的段落上只有 18% 重叠，按框宽算会被当成「另一栏」而漏掉，向上扩就直接
#: 跨过标题。
X_OVERLAP_RATIO = 0.25
#: 竖向「相接」容差（pt）：吸收段框与自身墨迹盒的舍入差。
TOUCH_TOLERANCE = 2.0
#: 认为「本段因高度不足失败」的编译原因（尺寸问题，不是内容错误）。
_OVERFLOW_REASONS = ("vertical-overflow", "overfull-vbox", "text-clipped")

Box = tuple[float, float, float, float]
#: 未扩原因：``no-self-region`` / ``unsafe-overlap`` / ``no-room``。
SKIP_NO_SELF = "unsafe-no-self-region"
SKIP_UNSAFE = "unsafe-overlap"
SKIP_NO_ROOM = "no-room"

#: 扩框方向：``down`` 向下（先试）、``up`` 向上（向下无净空时才试）。
DIRECTION_DOWN = "down"
DIRECTION_UP = "up"
#: 横向扩框方向：``right`` 向右、``left`` 向左（跨栏延伸进相邻空闲带）。
DIRECTION_RIGHT = "right"
DIRECTION_LEFT = "left"


def refine_enabled() -> bool:
    """扩框总开关：``BDT_LATEX_REFINE=0/false/no/off`` 关闭（测试与排查用）。"""
    raw = os.environ.get("BDT_LATEX_REFINE")
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


@dataclass(frozen=True)
class RefineResult:
    """一次排版扩框计划的结果（只读证据）。"""

    #: debug_id → 扩后的 IL box ``[x, y, x2, y2]``。
    overrides: dict[str, list[float]]
    #: 被缩字/溢出的候选段数。
    targets: int
    #: 未扩原因计数。
    skipped: dict[str, int]


# --------------------------------------------------------------------------- #
# 纯几何
# --------------------------------------------------------------------------- #
def plan_expansion(
    box,
    regions,
    *,
    ink=(),
    direction: str = DIRECTION_DOWN,
    page_bottom: float = 0.0,
    page_top: float | None = None,
) -> tuple[Box | None, str | None]:
    """把 ``box`` 沿 ``direction`` 扩到最近障碍。→ ``(新框 | None, 未扩原因)``。

    坐标是 IL 约定（y 向上）：``box = (x, y_bottom, x2, y_top)``。两个方向规则对称：

    1. 横向重叠不足的区域（另一栏）忽略，阈值按窄边算（见 ``X_OVERLAP_RATIO``）；
    2. 与框交集面积最大的区域视为**本段自身墨迹**并跳过；它必须基本落在框内，
       否则说明检测把本段和邻居合并成一块，无法区分自身墨迹 → 放弃；
    3. 其余与框竖向交叠的区域 → 框内已有别人墨迹，扩框不安全 → 放弃；
    4. 同方向的区域把扩张边界收在它的近侧边缘（向下取它的上沿、向上取它的下沿），
       再加 ``EXPAND_GAP_PT`` 净空；没有障碍时一直扩到页边。

    ``ink`` 是精确墨迹矩形（``page_ink_rects``，已排除本段自己的墨迹），与区域走同
    一条规则：区域检测是启发式的（实测漏过一个小标题），这一步保证扩出去的那条带子
    里没有别的文字/图形。

    向上扩只在调用方把首行量测钉在原框口径上时才真的让出高度，见模块 docstring。
    """
    if direction not in (DIRECTION_DOWN, DIRECTION_UP):
        raise ValueError(f"未知扩框方向：{direction!r}")
    if direction == DIRECTION_UP and page_top is None:
        raise ValueError("向上扩框需要 page_top")

    x, y, x2, y2 = (float(value) for value in box)
    if x2 - x <= 0 or y2 <= y:
        return None, SKIP_NO_ROOM

    self_region = _self_region(box, regions)
    if self_region is None:
        return None, SKIP_NO_SELF
    rx, ry, rx2, ry2 = self_region
    if not (ry >= y - TOUCH_TOLERANCE and ry2 <= y2 + TOUCH_TOLERANCE):
        # 与自己交集最大的区域都伸出框外：多半是「本段 + 邻居」被合并成一块。
        return None, SKIP_NO_SELF

    downward = direction == DIRECTION_DOWN
    limit = float(page_bottom) if downward else float(page_top)
    # 区域与精确墨迹同一条规则：竖向交叠 → 不安全；同侧 → 收紧边界。
    for values in [*regions, *ink]:
        rect = tuple(float(value) for value in values)
        if rect == self_region or not _same_column(box, rect):
            continue
        ry, ry2 = rect[1], rect[3]
        if ry < y2 - TOUCH_TOLERANCE and ry2 > y + TOUCH_TOLERANCE:
            return None, SKIP_UNSAFE
        if downward:
            if ry2 <= y + TOUCH_TOLERANCE:
                limit = max(limit, ry2)
        elif ry >= y2 - TOUCH_TOLERANCE:
            limit = min(limit, ry)

    if downward:
        edge = float(page_bottom)
        new_y = limit + EXPAND_GAP_PT if limit > edge else edge
        if y - new_y < MIN_GAIN_PT:
            return None, SKIP_NO_ROOM
        return (x, new_y, x2, y2), None

    edge = float(page_top)
    new_top = limit - EXPAND_GAP_PT if limit < edge else edge
    if new_top - y2 < MIN_GAIN_PT:
        return None, SKIP_NO_ROOM
    return (x, y, x2, new_top), None


def _same_column(box, region) -> bool:
    """区域与框是否同栏：横向重叠达「窄边」的 ``X_OVERLAP_RATIO``。"""
    x, x2 = float(box[0]), float(box[2])
    rx, rx2 = float(region[0]), float(region[2])
    narrower = min(x2 - x, rx2 - rx)
    if narrower <= 0:
        return False
    return min(x2, rx2) - max(x, rx) >= max(1.0, X_OVERLAP_RATIO * narrower)


def _self_region(box, regions) -> Box | None:
    """与框交集面积最大的同栏区域（视为本段自身墨迹）。"""
    x, y, x2, y2 = (float(value) for value in box)
    best: Box | None = None
    best_area = 0.0
    for region in regions:
        values = tuple(float(value) for value in region)
        if not _same_column(box, values):
            continue
        rx, ry, rx2, ry2 = values
        inter_height = min(y2, ry2) - max(y, ry)
        if inter_height <= 0:
            continue
        area = (min(x2, rx2) - max(x, rx)) * inter_height
        if area > best_area:
            best_area = area
            best = (rx, ry, rx2, ry2)
    return best


def plan_widen_expansion(
    box,
    regions,
    *,
    ink=(),
    direction: str = DIRECTION_RIGHT,
    page_left: float = 0.0,
    page_right: float,
) -> tuple[Box | None, str | None]:
    """把 ``box`` 向左/右延伸进相邻空闲带。→ ``(新框 | None, 未扩原因)``。

    IL 坐标（y 向上）：``box = (x, y_bottom, x2, y_top)``，y 范围保持不变。
    ``direction`` 取 :data:`DIRECTION_RIGHT` / :data:`DIRECTION_LEFT`，规则对称：

    1. 自身墨迹区域与 :func:`plan_expansion` 同一套判定（交集最大的同栏区域）；
       它必须基本落在框的 x 范围内，否则多半是「本段 + 邻栏」被检测合并成一块，
       无法区分自身墨迹 → 放弃；
    2. 障碍（regions 与 ink 同规则）是**与框纵向交叠**的矩形。注意这里不能用
       ``X_OVERLAP_RATIO`` 那套 25% 重叠比例判「同栏」：纵向扩框里轻微交叠的
       邻栏可以忽略，横向扩框里邻栏文字只要与框纵向轻微交叠，扩过去就是物理
       碰撞，必须按「任何纵向交叠」处理；纵向不交叠的矩形物理上碰不到，忽略；
    3. 纵向交叠且与框横向交叠 → 框内已有别人墨迹 → 放弃（``SKIP_UNSAFE``）；
    4. 完全在扩框一侧的矩形把边界收在它的近侧边缘，再加 ``EXPAND_GAP_PT``
       净空；没有障碍时扩到页边；增量不足 ``MIN_GAIN_PT`` → ``SKIP_NO_ROOM``。
    """
    if direction not in (DIRECTION_RIGHT, DIRECTION_LEFT):
        raise ValueError(f"未知扩框方向：{direction!r}")

    x, y, x2, y2 = (float(value) for value in box)
    if x2 - x <= 0 or y2 <= y:
        return None, SKIP_NO_ROOM

    self_region = _self_region(box, regions)
    if self_region is None:
        return None, SKIP_NO_SELF
    if not (
        self_region[0] >= x - TOUCH_TOLERANCE
        and self_region[2] <= x2 + TOUCH_TOLERANCE
    ):
        # 与自己交集最大的区域在横向伸出框外：多半是「本段 + 邻栏」被合并成一块。
        return None, SKIP_NO_SELF

    rightward = direction == DIRECTION_RIGHT
    limit = float(page_right) if rightward else float(page_left)
    # 区域与精确墨迹同一条规则：纵向交叠 → 不安全或收边界；纵向不交叠 → 忽略。
    for values in [*regions, *ink]:
        rect = tuple(float(value) for value in values)
        if rect == self_region:
            continue
        ry, ry2 = rect[1], rect[3]
        if not (ry < y2 - TOUCH_TOLERANCE and ry2 > y + TOUCH_TOLERANCE):
            # 纵向位置不同，物理上碰不到（对侧栏、页眉页脚同理）。
            continue
        rx, rx2 = rect[0], rect[2]
        if rightward:
            if rx >= x2 - TOUCH_TOLERANCE:
                limit = min(limit, rx)
            elif rx2 > x + TOUCH_TOLERANCE:
                return None, SKIP_UNSAFE
        elif rx2 <= x + TOUCH_TOLERANCE:
            limit = max(limit, rx2)
        elif rx < x2 - TOUCH_TOLERANCE:
            return None, SKIP_UNSAFE

    if rightward:
        edge = float(page_right)
        new_x2 = limit - EXPAND_GAP_PT if limit < edge else edge
        if new_x2 - x2 < MIN_GAIN_PT:
            return None, SKIP_NO_ROOM
        return (x, y, new_x2, y2), None

    edge = float(page_left)
    new_x = limit + EXPAND_GAP_PT if limit > edge else edge
    if x - new_x < MIN_GAIN_PT:
        return None, SKIP_NO_ROOM
    return (new_x, y, x2, y2), None


def plan_next_page_float_core(box, obstacles, *, page_height, required_height) -> Box | None:
    """跨页整框迁移的纯几何核心：在下一页同 x 范围找最靠上的落点。

    ``obstacles`` 是与 ``box`` 的 x 范围有横向交叠的 IL 矩形（``_intersects``
    的 x 投影，带 ``TOUCH_TOLERANCE`` 粘连）。``[0, page_height]`` 按它们的
    y 区间切成空闲区间，**自上而下**找第一个高度 ≥ ``required_height`` 的区间
    ``[a, b]``（b 是区间上沿），返回顶对齐的 ``(x, b - required_height, x2, b)``；
    放不下（或 ``required_height <= 0``）→ None。
    """
    if required_height <= 0:
        return None
    x, _y, x2, _y2 = (float(value) for value in box)
    lo, hi = x - TOUCH_TOLERANCE, x2 + TOUCH_TOLERANCE
    page_height = float(page_height)
    spans: list[list[float]] = []
    for values in obstacles:
        rx, ry, rx2, ry2 = (float(value) for value in values)
        if min(rx2, hi) > max(rx, lo):
            bottom, top = max(0.0, ry), min(page_height, ry2)
            if top > bottom:
                spans.append([bottom, top])
    spans.sort()
    cursor = page_height
    for bottom, top in reversed(spans):  # 自上而下：先看最高障碍上方的净空
        if cursor - top >= required_height:
            return (x, cursor - required_height, x2, cursor)
        cursor = min(cursor, bottom)
    if cursor >= required_height:  # 最低障碍下方的整段净空（或无障碍）
        return (x, cursor - required_height, x2, cursor)
    return None


def plan_downward_expansion(box, regions, *, page_bottom: float) -> Box | None:
    """:func:`plan_expansion` 向下扩的薄封装（只关心结果时用）。"""
    return plan_expansion(box, regions, page_bottom=page_bottom)[0]


def plan_upward_expansion(box, regions, *, page_top: float) -> Box | None:
    """:func:`plan_expansion` 向上扩的薄封装；``page_top`` 按页高（IL 坐标）给。"""
    return plan_expansion(box, regions, direction=DIRECTION_UP, page_top=page_top)[0]


# --------------------------------------------------------------------------- #
# 编排
# --------------------------------------------------------------------------- #
def page_ink_rects(page) -> list[Box]:
    """译文页上的**精确墨迹矩形**（文字 span / 矢量 / 图片），IL 坐标。

    区域检测是启发式的，实测漏过一个 14pt 小标题（区域检测把同栏短区域当成了
    「另一栏」或根本没检出）。这一步不猜语义，只问「这条带子里有没有东西」，
    用来给扩框边界托底；失败当「无墨迹」处理（保持原来的区域判定）。
    """
    import pymupdf

    height = float(page.rect.height)
    boxes: list[Box] = []

    def add(rect) -> None:
        if rect is None:
            return
        rect = pymupdf.Rect(rect)
        if rect.is_empty or rect.is_infinite:
            return
        boxes.append((rect.x0, height - rect.y1, rect.x1, height - rect.y0))

    try:
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line.get("spans", []):
                    if str(span.get("text", "")).strip():
                        add(span.get("bbox"))
        for item in page.get_drawings():
            add(item.get("rect"))
        for info in page.get_image_info():
            add(info.get("bbox"))
    except Exception:  # noqa: BLE001 - 拿不到墨迹只是退回纯区域判定
        logger.warning("提取页面墨迹失败，只用区域判定扩框", exc_info=True)
    return boxes


def foreign_ink(ink, box) -> list[Box]:
    """从页面墨迹里去掉「本段自己」（与 ``box`` 相交的部分，带粘接容差）。"""
    x, y, x2, y2 = (float(value) for value in box)
    self_rect = (x - TOUCH_TOLERANCE, y - TOUCH_TOLERANCE, x2 + TOUCH_TOLERANCE, y2 + TOUCH_TOLERANCE)
    keep: list[Box] = []
    for values in ink:
        rect = tuple(float(value) for value in values)
        if _intersects(rect, self_rect):
            continue
        keep.append(rect)
    return keep


def _intersects(first: Box, second) -> bool:
    return (
        min(first[2], second[2]) > max(first[0], second[0])
        and min(first[3], second[3]) > max(first[1], second[1])
    )


def _downward_order(entry: tuple) -> tuple:
    """向下扩的处理顺序：自上而下（上方段先吃段间净空），再按左缘与 id 定序。"""
    debug_id, box = entry[0], entry[1]
    return (-float(box[3]), float(box[0]), debug_id)


def _upward_order(entry: tuple) -> tuple:
    """向上扩的处理顺序：自下而上（下方段先吃段间净空）。"""
    debug_id, box = entry[0], entry[1]
    return (float(box[1]), float(box[0]), debug_id)


def _rounded(box) -> list[float]:
    return [round(float(value), 3) for value in box]


def expansion_reason(*, scale, ok: bool, reason) -> str | None:
    """贴片是否需要扩框：被缩字号或因高度不足失败。→ 原因 / None。"""
    if isinstance(scale, int | float) and scale < SHRINK_RATIO:
        return "font-shrink"
    if not ok and any(token in str(reason or "") for token in _OVERFLOW_REASONS):
        return "overflow"
    return None


def shrink_targets(report: dict | None) -> dict[str, str]:
    """从 ``latex_bbox_report.json`` 的 decisions 里挑出需要扩框的段。

    只有「真的被缩了字号」或「因高度不足编译失败」的段才值得重排；对未缩字段
    扩框只会改变版式，收益为零。
    """
    targets: dict[str, str] = {}
    for decision in (report or {}).get("decisions") or []:
        debug_id = decision.get("debug_id")
        if not debug_id:
            continue
        reason = expansion_reason(
            scale=decision.get("font_scale"),
            ok=decision.get("reason") == "applied",
            reason=decision.get("reason"),
        )
        if reason:
            targets[debug_id] = reason
    return targets


def plan_build_refinement(
    *,
    report: dict | None,
    geometry: dict | None,
    pdf_path,
    detector: PaddleLayoutRegions,
) -> RefineResult:
    """一次性编译的第二遍：按译文 PDF 的版面算出 ``{debug_id: box}``。

    逐页处理，每页分两阶段：先给所有目标向下扩，再把仍没净空的向上扩（障碍集
    里含刚下扩的框）。已扩的框都进障碍集，后面的段不会抢同一段净空。
    """
    import pymupdf

    targets = shrink_targets(report)
    skipped: dict[str, int] = {}
    if (report or {}).get("mode") == "repair":
        # repair 模式走旧 redaction 路径，扩框没有预设跳过字符兜底。
        return RefineResult(overrides={}, targets=0, skipped=skipped)
    if not targets or not detector.available:
        return RefineResult(overrides={}, targets=len(targets), skipped=skipped)
    by_id = {
        paragraph.get("id"): paragraph
        for paragraph in (geometry or {}).get("paragraphs") or []
    }
    selected: dict[int, list[tuple[str, Box]]] = {}
    for debug_id in targets:
        paragraph = by_id.get(debug_id) or {}
        box = paragraph.get("src_box") or paragraph.get("layout_box")
        page_no = paragraph.get("page")
        if not box or not page_no:
            _bump(skipped, "missing-geometry")
            continue
        selected.setdefault(int(page_no), []).append((debug_id, tuple(box)))

    overrides: dict[str, list[float]] = {}
    with pymupdf.open(pdf_path) as pdf:
        for page_no, entries in sorted(selected.items()):
            index = page_no - 1
            if index < 0 or index >= pdf.page_count:
                skipped["page-out-of-range"] = skipped.get("page-out-of-range", 0) + len(
                    entries
                )
                continue
            regions = _regions_il(pdf[index], detector)
            if not regions:
                skipped["no-regions"] = skipped.get("no-regions", 0) + len(entries)
                continue
            obstacles = [region.box for region in regions]
            ink = page_ink_rects(pdf[index])
            pending_up: list[tuple[str, Box, str | None]] = []
            for debug_id, box in sorted(entries, key=_downward_order):
                expanded, reason = plan_expansion(
                    box, obstacles, ink=foreign_ink(ink, box)
                )
                if expanded is None:
                    pending_up.append((debug_id, box, reason))
                    continue
                overrides[debug_id] = _rounded(expanded)
                obstacles.append(expanded)
            page_top = float(pdf[index].rect.height)
            for debug_id, box, reason in sorted(pending_up, key=_upward_order):
                expanded, up_reason = plan_expansion(
                    box,
                    obstacles,
                    ink=foreign_ink(ink, box),
                    direction=DIRECTION_UP,
                    page_top=page_top,
                )
                if expanded is None:
                    # 两个方向都没净空：记向下那次的理由（更接近既有报告口径）。
                    _bump(skipped, reason or up_reason)
                    continue
                overrides[debug_id] = _rounded(expanded)
                obstacles.append(expanded)
    return RefineResult(overrides=overrides, targets=len(targets), skipped=skipped)


def plan_page_expansion(
    page,
    box,
    detector: PaddleLayoutRegions,
    *,
    page_bottom: float = 0.0,
    page_top: float | None = None,
) -> Box | None:
    """serve 局部编译用：对一张（已合成）页面算单段扩框（先向下，再向上）。

    两个方向共用同一次检测；``page_top`` 缺省取页高（IL 坐标）。
    """
    if not detector.available:
        return None
    regions = _regions_il(page, detector)
    if not regions:
        return None
    boxes = [region.box for region in regions]
    ink = page_ink_rects(page)
    expanded, _ = plan_expansion(
        box, boxes, ink=foreign_ink(ink, box), page_bottom=page_bottom
    )
    if expanded is not None:
        return expanded
    top = float(page_top) if page_top is not None else float(page.rect.height)
    return plan_expansion(
        box,
        boxes,
        ink=foreign_ink(ink, box),
        direction=DIRECTION_UP,
        page_top=top,
    )[0]


def plan_next_page_float(
    next_page,
    box,
    detector: PaddleLayoutRegions,
    *,
    required_height: float,
    page_height: float | None = None,
) -> Box | None:
    """下一页整框迁移的薄封装：区域 + 精确墨迹并集作障碍，调 core。

    检测失败或无区域 → None（调用方保持原行为）；``page_height`` 缺省取
    ``next_page.rect.height``（IL 坐标即页高）。
    """
    regions = _regions_il(next_page, detector)
    if not regions:
        return None
    obstacles = [region.box for region in regions]
    obstacles.extend(page_ink_rects(next_page))
    height = (
        float(page_height) if page_height is not None else float(next_page.rect.height)
    )
    return plan_next_page_float_core(
        box, obstacles, page_height=height, required_height=required_height
    )


def plan_widen_page_expansion(
    page,
    box,
    detector: PaddleLayoutRegions,
    *,
    direction: str,
    page_left: float = 0.0,
    page_right: float | None = None,
) -> Box | None:
    """serve 局部编译用：对一张（已合成）页面把 ``box`` 向左/右横向扩框。

    与 :func:`plan_page_expansion` 同一套数据（PP-DocLayoutV3 区域 + 精确墨迹，
    已排除本段自身）；检测不可用或没有区域 → None。
    """
    if not detector.available:
        return None
    regions = _regions_il(page, detector)
    if not regions:
        return None
    boxes = [region.box for region in regions]
    ink = foreign_ink(page_ink_rects(page), box)
    right = float(page.rect.width) if page_right is None else float(page_right)
    return plan_widen_expansion(
        box, boxes, ink=ink, direction=direction, page_left=page_left, page_right=right
    )[0]


def _regions_il(page, detector: PaddleLayoutRegions) -> list[Region]:
    """检测一页；失败按「无区域」处理（调用方保持原行为）。"""
    try:
        return detector.detect_page(page)
    except Exception:  # noqa: BLE001 - 检测失败只是不扩框
        logger.warning("版面检测失败，跳过本页扩框", exc_info=True)
        return []


def _bump(counter: dict[str, int], key: str | None) -> None:
    if key:
        counter[key] = counter.get(key, 0) + 1
