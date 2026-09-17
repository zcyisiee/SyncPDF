"""圈号标记修复：把"圆圈 + 数字"这类装饰性矢量图形合并为单个 Unicode 圈号字符。

背景：不少 PDF 用「一个数字字符 + 一圈矢量贝塞尔曲线」来表示圈号（① ② ③ …）。
BabelDOC 只重排文字，装饰性曲线不属于任何段落，因此译文重排后圆圈留在原位、
文字移到别处，形成一堆漂浮的小圆圈（实测 e2e-f1872 第 2 页 12 个）。

处理策略：
1. 识别小尺寸、近似圆形、仅描边、由贝塞尔曲线闭合的矢量图形（PdfCurve）；
2. 找到完全落在该图形内部的单个字母/数字字符；
3. 把该字符替换为对应的 Unicode 圈号（①-⑳ / ⓐ-ⓩ / Ⓐ-Ⓩ）；
4. 从 ``page.pdf_curve`` 删除该装饰图形。

这样圈号变成**文本的一部分**，会随译文一起重排，不再漂浮。

已知边界（MVP）：
- 只处理单个字母/数字（1-20、a-z、A-Z）；圈内多字符（如 ㉑、10）暂不处理；
- 只处理「闭合小图形」；下划线、方框、高亮等其它装饰暂不处理；
- 圈号样式统一映射到空心圈号（①），不区分实心/圆角等原样式。
"""

from __future__ import annotations

import logging

from babeldoc.format.pdf.document_il import il_version_1
from babeldoc.format.pdf.document_il.utils.layout_helper import build_layout_index
from babeldoc.format.pdf.document_il.utils.layout_helper import (
    is_curve_in_figure_table_layout,
)
from babeldoc.format.pdf.translation_config import TranslationConfig

logger = logging.getLogger(__name__)

# 闭合小图形的尺寸范围（pt）与长宽比上限
MIN_SIZE = 3.0
MAX_SIZE = 24.0
MAX_ASPECT = 1.6
# 字符必须落在图形内部的容差（pt）
CONTAIN_TOLERANCE = 1.5
# 贝塞尔/曲线操作符
_CURVE_OPS = {"c", "v", "y"}


def enclosed_unicode(text: str) -> str | None:
    """单个字符 → Unicode 圈号字符；无对应返回 None。"""
    if len(text) != 1:
        return None
    ch = text
    if ch.isdigit():
        n = int(ch)
        if n == 0:
            return "\u24ea"  # ⓪
        if 1 <= n <= 20:
            return chr(0x2460 + n - 1)  # ①..⑳
        return None
    if "a" <= ch <= "z":
        return chr(0x24D0 + ord(ch) - ord("a"))  # ⓐ..ⓩ
    if "A" <= ch <= "Z":
        return chr(0x24B6 + ord(ch) - ord("A"))  # Ⓐ..Ⓩ
    return None


def _is_small_closed_shape(box) -> bool:
    if box is None:
        return False
    w = float(box.x2 - box.x)
    h = float(box.y2 - box.y)
    if w <= 0 or h <= 0:
        return False
    if not (MIN_SIZE <= w <= MAX_SIZE and MIN_SIZE <= h <= MAX_SIZE):
        return False
    if max(w, h) / min(w, h) > MAX_ASPECT:
        return False
    return True


def _path_ops(curve: il_version_1.PdfCurve) -> list[str]:
    """取路径操作符；兼容 pdf_path / pdf_original_path 两种表示。"""
    ops: list[str] = []
    for item in curve.pdf_path or []:
        op = getattr(item, "op", None)
        if op:
            ops.append(op)
    if ops:
        return ops
    for item in curve.pdf_original_path or []:
        inner = getattr(item, "pdf_path", None)
        op = getattr(inner, "op", None) if inner is not None else getattr(item, "op", None)
        if op:
            ops.append(op)
    return ops


def _looks_like_enclosing_shape(curve: il_version_1.PdfCurve) -> bool:
    """小尺寸、仅描边、含贝塞尔曲线的闭合图形（圆圈/椭圆/圆角框）。"""
    if curve.fill_background:
        return False
    if not _is_small_closed_shape(curve.box):
        return False
    ops = _path_ops(curve)
    if not ops or ops[0] != "m":
        return False
    if not any(op in _CURVE_OPS for op in ops):
        return False
    # 折线多边形（大量 l）不是圈
    if ops.count("l") > 4:
        return False
    return True


def _char_box(ch: il_version_1.PdfCharacter):
    if ch.visual_bbox is not None and ch.visual_bbox.box is not None:
        return ch.visual_bbox.box
    return ch.box


def _chars_inside(page: il_version_1.Page, box) -> list[il_version_1.PdfCharacter]:
    found = []
    for ch in page.pdf_character:
        if not ch.char_unicode or not ch.char_unicode.strip():
            continue
        cb = _char_box(ch)
        if cb is None:
            continue
        if (
            cb.x >= box.x - CONTAIN_TOLERANCE
            and cb.x2 <= box.x2 + CONTAIN_TOLERANCE
            and cb.y >= box.y - CONTAIN_TOLERANCE
            and cb.y2 <= box.y2 + CONTAIN_TOLERANCE
        ):
            found.append(ch)
    return found


class EnclosedMarkerFixer:
    """把「装饰圈 + 单字符」合并为 Unicode 圈号，并移除装饰圈。"""

    stage_name = "Fix Enclosed Markers"

    def __init__(self, translation_config: TranslationConfig):
        self.translation_config = translation_config
        self.enabled = getattr(translation_config, "fix_enclosed_markers", True)

    def process(self, docs: il_version_1.Document):
        # last_stats 供 debug 采集读取（关闭时无人读，开销可忽略）。
        self.last_stats = None
        if not self.enabled:
            return docs
        stats = {"shapes": 0, "converted": 0, "removed": 0}
        for page in docs.page:
            if not page.pdf_curve:
                continue
            layout_index, layout_map = build_layout_index(page)
            to_remove = []
            for curve in page.pdf_curve:
                if not _looks_like_enclosing_shape(curve):
                    continue
                # 图/表内部的圆圈属于图形本身，不能动
                if is_curve_in_figure_table_layout(
                    curve, layout_index, layout_map
                ):
                    continue
                stats["shapes"] += 1
                chars = _chars_inside(page, curve.box)
                if len(chars) != 1:
                    continue
                mapped = enclosed_unicode(chars[0].char_unicode.strip())
                if mapped is None:
                    continue
                chars[0].char_unicode = mapped
                to_remove.append(curve)
                stats["converted"] += 1
            for curve in to_remove:
                if curve in page.pdf_curve:
                    page.pdf_curve.remove(curve)
                    stats["removed"] += 1
        if stats["converted"]:
            logger.info(
                "EnclosedMarkerFixer: converted %d enclosed markers "
                "(candidate shapes=%d, removed curves=%d)",
                stats["converted"],
                stats["shapes"],
                stats["removed"],
            )
        self.last_stats = stats
        return docs
