"""源行几何采集（P3-0）：行盒、首行缩进、baseline pitch 与下方净空。

**必须在 ILTranslator / Typesetting 之前采集**（根因 5）：译文回填会把
composition 换成纯文本 run（``pdf_same_style_unicode_characters``，不再有
``pdf_line``），字符 box 也会被 Typesetting 改写，源行几何随之丢失。

两个入口：

- :func:`capture_source_line_geometry`：extract / high_level 路径，直接读 IL 里
  仍是源坐标的 composition 字符（extract 时落进 ``state.pkl``）；
- :func:`geometry_from_char_objects`：旧 workdir 兜底，用 ``state.pkl`` 里
  extract 时落盘的 ``page_char_objects``（其 box 仍是源坐标）按段 box 聚类。

产物形状（debug_id → 指标，全部 IL 坐标、y 向上）：

``n_lines`` / ``first_line_dx``（首行左缘相对段 box 左缘，正=缩进、负=悬挂）/
``baseline_pitch``（相邻行顶距中位数）/ ``ascent_top``（首行字顶到段 box 顶）/
``line_boxes`` / ``space_below_pt``（同列下方到最近障碍的净空）。
"""

from __future__ import annotations

import statistics

#: 行聚类容差下限（pt）：同一条源行的字符行高差异不会超过它。
_LINE_TOLERANCE_MIN = 1.0
#: 行 box 与段 box 的横向重叠下限（pt）：低于它认为不是该段的行。
_MIN_X_OVERLAP = 1.0
#: 「下方障碍」判定容差（pt）：box 相接不算遮挡。
_BOX_TOUCH_TOLERANCE = 0.5
#: 旧 workdir 兜底时，字符中心归属段 box 的容差（pt）。
_ASSIGN_TOLERANCE = 1.0
#: 参与「阻挡下方」判定的页布局区类别前缀（与 layout_geometry 一致）。
_OBSTACLE_LAYOUT_PREFIXES = ("figure", "table", "formula")


def _char_box(char) -> tuple[float, float, float, float] | None:
    """字符的墨迹 box（优先 ``visual_bbox``），IL 坐标 (x0, y0, x1, y1)。"""
    for holder in (getattr(char, "visual_bbox", None), char):
        box = getattr(holder, "box", None)
        if box is None:
            continue
        values = (box.x, box.y, box.x2, box.y2)
        if any(value is None for value in values):
            continue
        return (float(box.x), float(box.y), float(box.x2), float(box.y2))
    return None


def _iter_paragraph_chars(paragraph):
    """产出段落 composition 中的全部字符对象（pdf_line/富文本/公式/单字符）。"""
    for composition in paragraph.pdf_paragraph_composition or []:
        if composition is None:
            continue
        line = composition.pdf_line
        if line is not None and line.pdf_character:
            yield from line.pdf_character
        same_style = composition.pdf_same_style_characters
        if same_style is not None and same_style.pdf_character:
            yield from same_style.pdf_character
        if composition.pdf_character is not None:
            yield composition.pdf_character
        formula = composition.pdf_formula
        if formula is not None and formula.pdf_character:
            yield from formula.pdf_character


def _cluster_lines(boxes: list[tuple], tolerance: float) -> list[dict]:
    """把字符 box 按纵坐标聚成源行（从上到下）。"""
    ordered = sorted(boxes, key=lambda box: -((box[1] + box[3]) / 2.0))
    lines: list[dict] = []
    for box in ordered:
        center = (box[1] + box[3]) / 2.0
        if lines and abs(center - lines[-1]["center"]) <= tolerance:
            current = lines[-1]
            current["boxes"].append(box)
            current["center"] = statistics.fmean(
                (item[1] + item[3]) / 2.0 for item in current["boxes"]
            )
            continue
        lines.append({"boxes": [box], "center": center})
    for line in lines:
        items = line["boxes"]
        line["x0"] = min(item[0] for item in items)
        line["y0"] = min(item[1] for item in items)
        line["x1"] = max(item[2] for item in items)
        line["y1"] = max(item[3] for item in items)
        line.pop("boxes", None)
        line.pop("center", None)
    return lines


def _line_tolerance(boxes: list[tuple]) -> float:
    heights = [box[3] - box[1] for box in boxes if box[3] > box[1]]
    if not heights:
        return _LINE_TOLERANCE_MIN
    return max(_LINE_TOLERANCE_MIN, 0.5 * statistics.median(heights))


def _metrics_from_lines(paragraph_box, lines: list[dict]) -> dict | None:
    """由源行盒计算段落几何指标（无行返回 None）。"""
    if not lines or paragraph_box is None:
        return None
    first = lines[0]
    pitch_values = [
        abs(lines[index]["y1"] - lines[index + 1]["y1"])
        for index in range(len(lines) - 1)
    ]
    pitch_values = [value for value in pitch_values if value > 0.1]
    line_boxes = [
        [
            round(line["x0"], 3),
            round(line["y0"], 3),
            round(line["x1"], 3),
            round(line["y1"], 3),
        ]
        for line in lines
    ]
    return {
        "n_lines": len(lines),
        "first_line_dx": round(first["x0"] - float(paragraph_box.x), 3),
        "baseline_pitch": (
            round(statistics.median(pitch_values), 3) if pitch_values else None
        ),
        "ascent_top": round(float(paragraph_box.y2) - first["y1"], 3),
        "line_boxes": line_boxes,
    }


def _is_capturable(paragraph) -> bool:
    """段落是否属于「页面上、非水印、非旋转」的可采集对象。"""
    if paragraph is None:
        return False
    if getattr(paragraph, "xobj_id", None) not in (0, None):
        # 水印（-1）与嵌套 XObject（坐标不在页面上）都跳过。
        return False
    if getattr(paragraph, "vertical", False):
        return False
    return paragraph.box is not None


def _paragraph_box_tuple(paragraph) -> tuple[float, float, float, float]:
    box = paragraph.box
    return (float(box.x), float(box.y), float(box.x2), float(box.y2))


def _overlap(first: tuple, second: tuple) -> tuple[float, float]:
    x_overlap = min(first[2], second[2]) - max(first[0], second[0])
    return x_overlap, min(first[3], second[3]) - max(first[1], second[1])


def _obstacle_boxes(page) -> list[tuple]:
    """页面上会阻挡下扩的布局区（figure/table/formula）。"""
    boxes: list[tuple] = []
    for region in getattr(page, "page_layout", None) or []:
        name = str(getattr(region, "class_name", "") or "")
        if not name.startswith(_OBSTACLE_LAYOUT_PREFIXES):
            continue
        box = getattr(region, "box", None)
        if box is None or None in (box.x, box.y, box.x2, box.y2):
            continue
        boxes.append((float(box.x), float(box.y), float(box.x2), float(box.y2)))
    return boxes


def _page_bottom(page) -> float:
    box = getattr(page, "cropbox", None)
    box = getattr(box, "box", None) if box is not None else None
    if box is None or box.y is None:
        return 0.0
    return float(box.y)


def attach_space_below(entries: list[dict], page) -> None:
    """就地写入 ``space_below_pt``：段 box 下方到最近障碍（或页底）的净空。"""
    obstacles = [tuple(item["box"]) for item in entries] + _obstacle_boxes(page)
    bottom = _page_bottom(page)
    for item in entries:
        box = tuple(item["box"])
        limit = bottom
        for other in obstacles:
            if other == box:
                continue
            x_overlap, _ = _overlap(box, other)
            if x_overlap <= _MIN_X_OVERLAP:
                continue
            if other[3] > box[1] + _BOX_TOUCH_TOLERANCE:
                # 障碍物的顶部高于本段底部 → 不是「下方」的净空。
                continue
            limit = max(limit, other[3])
        item["space_below_pt"] = round(max(0.0, box[1] - limit), 3)


def capture_source_line_geometry(docs) -> dict[str, dict]:
    """采集每段源行几何（必须在 ILTranslator/Typesetting 之前调用）。"""
    geometry: dict[str, dict] = {}
    for page in getattr(docs, "page", None) or []:
        entries: list[dict] = []
        for paragraph in page.pdf_paragraph or []:
            if not _is_capturable(paragraph) or not paragraph.debug_id:
                continue
            boxes = [
                box
                for box in (_char_box(char) for char in _iter_paragraph_chars(paragraph))
                if box is not None
            ]
            if not boxes:
                continue
            lines = _cluster_lines(boxes, _line_tolerance(boxes))
            metrics = _metrics_from_lines(paragraph.box, lines)
            if metrics is None:
                continue
            metrics["page"] = page.page_number
            metrics["box"] = list(_paragraph_box_tuple(paragraph))
            entries.append({"debug_id": paragraph.debug_id, "box": metrics["box"]})
            geometry[paragraph.debug_id] = metrics
        attach_space_below(entries, page)
        for item in entries:
            geometry[item["debug_id"]]["space_below_pt"] = item["space_below_pt"]
    return geometry


def geometry_from_char_objects(docs, page_char_objects) -> dict[str, dict]:
    """旧 workdir 兜底：用 ``page_char_objects``（源坐标）按段 box 聚类。

    ``page_char_objects`` 是 extract 时按页枚举的字符对象（``box`` 为源坐标，
    后续阶段不会回写），因此可以在没有 ``state.pkl`` 行几何时重建源行盒。

    先按段 box **归属字符**再聚类行：双栏页面同一 y 上的左右栏字符会被
    整体聚类成一行，必须先按 box 分离（否则首行缩进/行数全错）。
    """
    geometry: dict[str, dict] = {}
    if not page_char_objects:
        return geometry
    for page in getattr(docs, "page", None) or []:
        chars = page_char_objects.get(page.page_number)
        if not chars:
            continue
        boxes = [
            box for box in (_char_box(char) for char in chars) if box is not None
        ]
        if not boxes:
            continue
        entries: list[dict] = []
        for paragraph in page.pdf_paragraph or []:
            if not _is_capturable(paragraph) or not paragraph.debug_id:
                continue
            para_box = _paragraph_box_tuple(paragraph)
            owned = [box for box in boxes if _center_inside(box, para_box)]
            if not owned:
                continue
            lines = _cluster_lines(owned, _line_tolerance(owned))
            metrics = _metrics_from_lines(paragraph.box, lines)
            if metrics is None:
                continue
            metrics["page"] = page.page_number
            metrics["box"] = list(para_box)
            metrics["source"] = "page-char-objects"
            entries.append({"debug_id": paragraph.debug_id, "box": metrics["box"]})
            geometry[paragraph.debug_id] = metrics
        attach_space_below(entries, page)
        for item in entries:
            geometry[item["debug_id"]]["space_below_pt"] = item["space_below_pt"]
    return geometry


def _center_inside(box: tuple, para_box: tuple) -> bool:
    """字符中心是否落在段 box 内（± 1pt，吸收半字宽差）。"""
    center_x = (box[0] + box[2]) / 2.0
    center_y = (box[1] + box[3]) / 2.0
    return (
        para_box[0] - _ASSIGN_TOLERANCE
        <= center_x
        <= para_box[2] + _ASSIGN_TOLERANCE
        and para_box[1] - _ASSIGN_TOLERANCE
        <= center_y
        <= para_box[3] + _ASSIGN_TOLERANCE
    )
