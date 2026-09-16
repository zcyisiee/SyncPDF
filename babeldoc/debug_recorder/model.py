"""Debug 归档的数据契约：纯数据类 + 一个 pymupdf 页面帮助函数。

本模块是采集层与查看器之间的稳定契约（计划指定的类型名）：
:class:`PageFrame` / :class:`Box` / :class:`Entity` / :class:`Relation`。

坐标约定：所有 box 一律 **PDF point、左上原点（y 向下）**，由
:data:`PDF_TOPLEFT` 标注；原始坐标系（如 BabelDOC IL 的左下原点）由调用方
自行保留，不在这里做转换。

零业务依赖：只依赖标准库。
"""

from __future__ import annotations

from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field

#: 统一坐标系标识：PDF point，左上原点（y 向下）。
PDF_TOPLEFT = "pdf_topleft"


def _round(value) -> float | None:
    if value is None:
        return None
    try:
        return round(float(value), 3)
    except (TypeError, ValueError):
        return None


def _rect_to_list(rect) -> list[float] | None:
    """``pymupdf.Rect`` / 四元序列 → ``[x0, y0, x1, y1]``；无法解析返回 ``None``。"""
    if rect is None:
        return None
    values = None
    if all(hasattr(rect, name) for name in ("x0", "y0", "x1", "y1")):
        values = [getattr(rect, name) for name in ("x0", "y0", "x1", "y1")]
    else:
        try:
            candidates = list(rect)
        except TypeError:
            return None
        if len(candidates) == 4:
            values = candidates
    if values is None:
        return None
    corners = [_round(value) for value in values]
    if any(corner is None for corner in corners):
        return None
    return corners


def _matrix_to_list(matrix) -> list[float] | None:
    """``pymupdf.Matrix`` → ``[a, b, c, d, e, f]``；无矩阵时返回 ``None``。"""
    if matrix is None:
        return None
    values = [_round(getattr(matrix, name, None)) for name in "abcdef"]
    if any(value is None for value in values):
        return None
    return values


@dataclass(slots=True)
class Box:
    """一个矩形框：坐标已统一为 :data:`PDF_TOPLEFT`。"""

    x0: float
    y0: float
    x1: float
    y1: float
    coord_system: str = PDF_TOPLEFT

    @classmethod
    def from_rect(cls, rect, coord_system: str = PDF_TOPLEFT) -> Box | None:
        corners = _rect_to_list(rect)
        if corners is None:
            return None
        return cls(*corners, coord_system=coord_system)

    def to_list(self) -> list[float]:
        return [self.x0, self.y0, self.x1, self.y1]

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class PageFrame:
    """一页的几何框架：MediaBox / CropBox / 旋转 / 未旋转显示尺寸。

    - ``page_index``：0-based 物理页下标；
    - ``width`` / ``height``：``page.rect`` 的未旋转裁剪显示尺寸；
    - ``mediabox`` / ``cropbox``：未旋转的原始矩形；
    - ``display_matrix``：pymupdf 的 ``derotation_matrix``（显示坐标 → 页面坐标）；
    - ``page_number_original``：物理页号（1-based）；
    - ``page_number_displayed``：显示页号；裁页（``--pages``）时与物理页号不同，
      本版本一律填物理页号，字段留给后续接线。
    """

    page_index: int
    width: float
    height: float
    mediabox: list[float] | None = None
    cropbox: list[float] | None = None
    rotation: int = 0
    display_matrix: list[float] | None = None
    page_number_original: int = 0
    page_number_displayed: int = 0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class Entity:
    """一个可观察对象（布局区域 / 段落 / 翻译单元 / 排版框 / 贴片）。"""

    id: str
    kind: str
    label: str | None = None
    page: int | None = None
    box: Box | None = None
    attrs: dict = field(default_factory=dict)
    parent_id: str | None = None
    children_ids: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class Relation:
    """两个实体之间的关联；``method`` 标注推断方式，``ambiguous`` 标注歧义。"""

    from_id: str
    to_id: str
    kind: str
    method: str = "explicit"
    ambiguous: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


def frame_from_pymupdf_page(page, page_number: int) -> PageFrame:
    """由 pymupdf ``Page`` 构造 :class:`PageFrame`（``page_number`` 为 1-based 物理页号）。"""
    rect = getattr(page, "rect", None)
    width = _round(getattr(rect, "width", None)) if rect is not None else None
    height = _round(getattr(rect, "height", None)) if rect is not None else None
    number = int(page_number)
    return PageFrame(
        page_index=number - 1,
        width=width if width is not None else 0.0,
        height=height if height is not None else 0.0,
        mediabox=_rect_to_list(getattr(page, "mediabox", None)),
        cropbox=_rect_to_list(getattr(page, "cropbox", None)),
        rotation=int(getattr(page, "rotation", 0) or 0),
        display_matrix=_matrix_to_list(getattr(page, "derotation_matrix", None)),
        page_number_original=number,
        page_number_displayed=number,
    )
