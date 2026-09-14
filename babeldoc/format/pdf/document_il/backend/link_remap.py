"""超链接矩形重映射：按源字符身份定位译文中的新矩形。

背景（对应 ``.plan/minerU深度融合.md`` 板块 5）：mono PDF 是从源 PDF 打开的
（内容流整体重发），Link 注释仍挂在页上但**矩形停在旧坐标**。旧实现
``_remap_links_by_text`` 在译文里搜同名文字来重定位，模型改写了文字（翻译/断行）
后就搜不到或搜错位置，导致链接漂到无关文字上。

本模块改用**对象身份映射**：解析阶段把每条链接覆盖的源 ``PdfCharacter``
对象记下来（``link_snapshot`` 落盘下标，state.pkl 里 pickle 对象列表保持身份），
重建阶段（Typesetting 之后）按优先级算新矩形：

1. ``stamp``：LaTeX 印章内 ``bdoclink`` 标记注记的真实墨迹矩形（最高优先级）；
2. ``char_union``：链接覆盖的源字符对象仍存活在某个段落 composition 里
   （公式 / 富文本 passthrough / 被跳过未翻译的段落）→ 取它们当前 box 的并集；
3. ``anchor``：源链接覆盖的文字（通常是引文号/章节号等数字序列）在**所属译文
   段落内**做带数字边界校验的局部文本匹配（``3`` 不匹配 ``13``）；同一标签
   重复出现时按源矩形在段内的相对位置就近分配，不把所有链接都塞给第一个命中；
4. ``paragraph`` / ``proportional``：段内文本匹配不可用 → 段落 box（比例投影为
   粗粒度 ``proportional``），显式记入 ``fallbacks`` 诊断，不宣称精确；
5. ``unresolved``：都不可用 → 保持原矩形，记入 ``unresolved``（不删链接）。

存活判定走 ``build_alive_char_ids()``：Typesetting 之后扫一遍所有 composition，
被引用的对象 ``id()`` 进集合。``PdfCharacter`` 带 ``__slots__``，无法在其上
打标记，因此这里用进程内 ``id()`` 集合（Typesetting 与 remap 在同一进程内完成）。

坐标：Link 矩形来自 ``page.get_links()``，是 pymupdf 页面坐标（左上原点）；
IL 的 ``PdfCharacter.box`` 是 PDF 坐标（左下原点）。换算 ``y' = H - y``。

写回策略（保真优先）：直接更新页面 Link 注记的 ``/Rect``（``xref_set_key``），
**不删除/重建**注记——这样任意 ``/A`` / ``/Dest``（URI / GoTo / GoToR / Launch /
命名目的地 / 未来未知动作）、``/Border``、目标视图语义全部原样保留，LAUNCH /
GOTOR 这类旧实现不支持的链接也不会再变成 unresolved。跨行拆分的标记链接
（印章里一行一个注记）保留**每行一个矩形**：首个矩形写回原注记，其余克隆同一
动作追加为新的 Link 注记（逻辑 1 条 → 物理 N 条，分别计数）。任何写入失败都
保留源注记不删。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from dataclasses import field

import pymupdf

logger = logging.getLogger(__name__)

# 字符并集时忽略细碎 box（宽度或高度 < 0.1pt，通常是退化字形）。
MIN_CHAR_EXTENT = 0.1
# 源链接上下文（段内、锚点前后）保留的字符数，用于审计/匹配诊断。
CONTEXT_CHARS = 16
# 目标页文本快照的最大长度（target_text 审计字段）。
TARGET_TEXT_CHARS = 200

#: 判定“精确”的解析方法（其余进 ``fallbacks`` 诊断列表）。
EXACT_METHODS = frozenset({"stamp", "char_union", "anchor"})


@dataclass(slots=True)
class LinkRemapResult:
    """一页的链接重映射结果。"""

    total: int = 0
    remapped: int = 0
    fallback_paragraph: int = 0
    stamp_resolved: int = 0
    unresolved: list[dict] = field(default_factory=list)
    # 新增诊断字段（向后兼容：默认 0 / 空，仅扩展不改变既有语义）。
    char_union_resolved: int = 0
    anchor_resolved: int = 0
    anchor_group_resolved: int = 0
    fallback_proportional: int = 0
    # 逻辑 1 条链接 → 物理注记数（跨行拆分时 > 1）。
    physical_annotations: int = 0
    split_annotations: int = 0
    fallbacks: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "remapped": self.remapped,
            "fallback_paragraph": self.fallback_paragraph,
            "stamp_resolved": self.stamp_resolved,
            "unresolved": list(self.unresolved),
            "char_union_resolved": self.char_union_resolved,
            "anchor_resolved": self.anchor_resolved,
            "anchor_group_resolved": self.anchor_group_resolved,
            "fallback_proportional": self.fallback_proportional,
            "physical_annotations": self.physical_annotations,
            "split_annotations": self.split_annotations,
            "fallbacks": list(self.fallbacks),
        }


def iter_paragraph_chars(paragraph):
    """产出段落 composition 中的全部字符对象（含公式/富文本/line）。"""
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


def build_alive_char_ids(document) -> set[int]:
    """Typesetting 之后仍存活于 composition 的字符对象 id 集合。

    必须在 Typesetting 之后调用：Typesetting 会为译文新建字符对象并把它们写回
    composition，而源对象在重排段落里不再被引用（虽然 state 的 list 仍持有引用，
    但坐标已过期）。
    """
    alive: set[int] = set()
    for page in getattr(document, "page", []) or []:
        for paragraph in getattr(page, "pdf_paragraph", []) or []:
            for char in iter_paragraph_chars(paragraph):
                alive.add(id(char))
    return alive


def il_box_union(chars, alive_ids: set[int] | None = None) -> pymupdf.Rect | None:
    """存活字符的视觉 box 并集（IL 坐标）；无有效 box 时返回 None。"""
    rect = None
    for char in chars:
        if alive_ids is not None and id(char) not in alive_ids:
            continue
        box = None
        visual = getattr(char, "visual_bbox", None)
        if visual is not None and getattr(visual, "box", None) is not None:
            box = visual.box
        elif getattr(char, "box", None) is not None:
            box = char.box
        if box is None:
            continue
        if any(getattr(box, name, None) is None for name in ("x", "y", "x2", "y2")):
            continue
        if (
            float(box.x2) - float(box.x) < MIN_CHAR_EXTENT
            or float(box.y2) - float(box.y) < MIN_CHAR_EXTENT
        ):
            continue
        candidate = pymupdf.Rect(
            float(box.x), float(box.y), float(box.x2), float(box.y2)
        )
        rect = candidate if rect is None else (rect | candidate)
    return rect


def _box_to_rect(box) -> pymupdf.Rect | None:
    if box is None:
        return None
    if any(getattr(box, name, None) is None for name in ("x", "y", "x2", "y2")):
        return None
    rect = pymupdf.Rect(float(box.x), float(box.y), float(box.x2), float(box.y2))
    return None if rect.is_empty else rect


def _project_ratio(
    rect: pymupdf.Rect, ratio: tuple[float, float, float, float]
) -> pymupdf.Rect | None:
    """把相对比例四元组投到译文段 box（IL 坐标）上。"""
    width = rect.x1 - rect.x0
    height = rect.y1 - rect.y0
    if width <= 0 or height <= 0:
        return None
    return pymupdf.Rect(
        rect.x0 + ratio[0] * width,
        rect.y0 + ratio[1] * height,
        rect.x0 + ratio[2] * width,
        rect.y0 + ratio[3] * height,
    )


# --------------------------------------------------------------------------- #
# 段内文本锚点匹配（渲染页文本；源标签/引文号序列）
# --------------------------------------------------------------------------- #
def _paragraph_box_mupdf(paragraph, page_height: float) -> pymupdf.Rect | None:
    """段落 IL box → pymupdf 页面坐标（左上原点）。"""
    box = getattr(paragraph, "box", None)
    if box is None or any(
        getattr(box, name, None) is None for name in ("x", "y", "x2", "y2")
    ):
        return None
    rect = pymupdf.Rect(
        float(box.x),
        page_height - float(box.y2),
        float(box.x2),
        page_height - float(box.y),
    )
    return None if rect.is_empty else rect


def paragraph_text_chars(dst_page, paragraph, page_height: float) -> list:
    """段落 box 内渲染文本的 ``(字符, pymupdf 矩形)`` 列表（按抽取顺序）。

    用渲染页文本而非 IL 字符：LaTeX overlay 后 IL composition 与页面墨迹可能
    脱钩，而 ``rawdict`` 拿到的是**最终页面**的字形位置，正是点击区需要的。
    """
    rect = _paragraph_box_mupdf(paragraph, page_height)
    if rect is None:
        return []
    try:
        raw = dst_page.get_text("rawdict", clip=rect)
    except Exception:  # noqa: BLE001 - 文本抽取失败只降级到几何回退
        logger.debug("段内文本抽取失败", exc_info=True)
        return []
    chars: list = []
    for block in raw.get("blocks", []) or []:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []) or []:
            for span in line.get("spans", []) or []:
                for char in span.get("chars", []) or []:
                    text = char.get("c")
                    bbox = char.get("bbox")
                    if not text or bbox is None:
                        continue
                    try:
                        box = pymupdf.Rect(bbox)
                    except (TypeError, ValueError):
                        continue
                    if box.is_empty:
                        continue
                    chars.append((text, box))
    return chars


def _compact_chars(chars: list) -> list:
    """去掉空白字符后的 ``(字符, 矩形)`` 列表（数字匹配不受空格干扰）。"""
    return [
        (text, box)
        for text, box in chars
        if text and not text.isspace()
    ]


def _find_anchor_occurrences(compact: list, anchor: str) -> list[dict]:
    """在紧凑字符流中找 ``anchor`` 的全部出现，返回 ``{start,end,rect}``。

    数字锚点做边界校验：``3`` 不匹配 ``13`` / ``31``（前后不得紧邻数字）。
    """
    needle = "".join(anchor.split())
    if not needle or not compact:
        return []
    text = "".join(text for text, _box in compact)
    out: list[dict] = []
    start = 0
    while True:
        index = text.find(needle, start)
        if index < 0:
            break
        start = index + 1
        end = index + len(needle)
        if needle.isdigit():
            if index > 0 and text[index - 1].isdigit():
                continue
            if end < len(text) and text[end].isdigit():
                continue
        rect = None
        for _char, box in compact[index:end]:
            rect = box if rect is None else (rect | box)
        if rect is None or rect.is_empty:
            continue
        out.append({"start": index, "end": end, "rect": rect})
    out.sort(key=lambda item: (round(item["rect"].y0, 1), item["rect"].x0))
    return out


def _choose_occurrence(
    occurrences: list[dict],
    entry: dict,
    paragraph,
    page_height: float,
    used: set[int],
) -> tuple[pymupdf.Rect | None, int | None, bool]:
    """在多个同标签出现里选一个未占用的；返回 ``(rect, index, exact)``。

    - 唯一出现 → 精确（``exact=True``）；
    - 多个出现 → 按源矩形在源段内的相对位置就近分配（``exact=False``），
      已占用的下标不再分配，避免所有重复链接都落到第一个命中。
    """
    if not occurrences:
        return None, None, False
    if len(occurrences) == 1:
        if 0 in used:
            return None, None, False
        return occurrences[0]["rect"], 0, True
    ratio = entry.get("src_rect_ratio")
    box = _paragraph_box_mupdf(paragraph, page_height)
    best_index = None
    best_score = None
    for index, occurrence in enumerate(occurrences):
        if index in used:
            continue
        if ratio and box is not None and box.width > 0 and box.height > 0:
            rect = occurrence["rect"]
            cx = (rect.x0 + rect.x1) / 2
            cy = (rect.y0 + rect.y1) / 2
            norm_x = (cx - box.x0) / box.width
            # src_rect_ratio 用 IL 坐标（左下原点 y 向上），这里翻成页面坐标
            # （左上原点 y 向下）后比较。
            norm_y = (cy - box.y0) / box.height
            src_x = (ratio[0] + ratio[2]) / 2
            src_y = 1.0 - (ratio[1] + ratio[3]) / 2
            score = (norm_x - src_x) ** 2 + (norm_y - src_y) ** 2
        else:
            score = float(index)
        if best_score is None or score < best_score:
            best_score = score
            best_index = index
    if best_index is None:
        return None, None, False
    return occurrences[best_index]["rect"], best_index, False


def _resolve_anchor_rect(
    entry: dict,
    paragraph_index: dict,
    dst_page,
    page_height: float,
    text_cache: dict,
    anchor_usage: dict,
    page_char_objects: list | None = None,
) -> tuple[pymupdf.Rect | None, str]:
    """源锚点文字 → 译文段内文本矩形（IL 坐标）。返回 ``(rect, method)``。"""
    anchor = (
        entry.get("source_text")
        or entry.get("covered_text")
        or entry.get("anchor_text")
        or entry.get("target_text")
        or ""
    )
    anchor = str(anchor).strip()
    if not anchor and page_char_objects:
        # 旧缓存快照没有 source_text：按 char_indices 现场重建锚点文字。
        indices = sorted(
            index
            for index in (entry.get("char_indices") or [])
            if 0 <= index < len(page_char_objects)
        )
        anchor = "".join(
            str(getattr(page_char_objects[index], "char_unicode", "") or "")
            for index in indices
        ).strip()
    if not anchor:
        return None, ""
    for paragraph_id in entry.get("paragraph_ids") or []:
        paragraph = paragraph_index.get(paragraph_id)
        if paragraph is None:
            continue
        cache_key = id(paragraph)
        chars = text_cache.get(cache_key)
        if chars is None:
            chars = paragraph_text_chars(dst_page, paragraph, page_height)
            text_cache[cache_key] = chars
        compact = _compact_chars(chars)
        occurrences = _find_anchor_occurrences(compact, anchor)
        if not occurrences:
            continue
        usage_key = (paragraph_id, "".join(anchor.split()))
        used = anchor_usage.setdefault(usage_key, set())
        rect, index, exact = _choose_occurrence(
            occurrences, entry, paragraph, page_height, used
        )
        if rect is None or index is None:
            continue
        used.add(index)
        il_rect = pymupdf.Rect(
            rect.x0, page_height - rect.y1, rect.x1, page_height - rect.y0
        )
        return il_rect, ("anchor" if exact else "anchor_group")
    return None, ""


def resolve_link_rect(
    entry: dict,
    page_char_objects: list,
    paragraph_index: dict,
    alive_ids: set[int] | None = None,
    *,
    dst_page=None,
    page_height: float | None = None,
    text_cache: dict | None = None,
    anchor_usage: dict | None = None,
) -> tuple[pymupdf.Rect | None, str]:
    """算一条链接在 IL 坐标下的新矩形。返回 ``(rect, method)``。

    优先级：``char_union``（存活源字符）→ ``anchor``（段内文本锚点匹配）→
    ``paragraph`` / ``proportional``（段落 box / 段内比例投影）→ ``unresolved``。

    ``src_rect_ratio`` 投影是**近似**（译文重排后源几何不再对应），返回的
    method 为 ``proportional``，调用方据此记入诊断，不当作精确映射。
    """
    chars = [
        page_char_objects[index]
        for index in (entry.get("char_indices") or [])
        if 0 <= index < len(page_char_objects)
    ]
    rect = il_box_union(chars, alive_ids)
    if rect is not None:
        return rect, "char_union"

    if dst_page is not None and page_height is not None:
        rect, method = _resolve_anchor_rect(
            entry,
            paragraph_index,
            dst_page,
            page_height,
            text_cache if text_cache is not None else {},
            anchor_usage if anchor_usage is not None else {},
            page_char_objects,
        )
        if rect is not None:
            return rect, method

    for paragraph_id in entry.get("paragraph_ids") or []:
        paragraph = paragraph_index.get(paragraph_id)
        if paragraph is None:
            continue
        rect = _box_to_rect(getattr(paragraph, "box", None))
        if rect is None:
            continue
        # 段内相对几何投影：链接源矩形在源段内的比例位置 → 译文段内同比例位置。
        # 比整段 box 精确（URL/引用号链接只盖段落一小部分），但仍是近似。
        ratio = entry.get("src_rect_ratio")
        if ratio:
            projected = _project_ratio(rect, ratio)
            if projected is not None and not projected.is_empty:
                return projected, "proportional"
        return rect, "paragraph"

    return None, "unresolved"


# --------------------------------------------------------------------------- #
# 页面坐标 ↔ PDF /Rect 互转（xutils）
# --------------------------------------------------------------------------- #
def _il_to_page(rect: pymupdf.Rect, page_height: float) -> pymupdf.Rect:
    """IL（左下原点）→ pymupdf 页面坐标（左上原点）。"""
    return pymupdf.Rect(
        rect.x0, page_height - rect.y1, rect.x1, page_height - rect.y0
    )


def _page_rect_to_pdf(page: pymupdf.Page, rect: pymupdf.Rect) -> pymupdf.Rect:
    """pymupdf 页面坐标 → PDF ``/Rect`` 用户空间坐标（含页面旋转）。

    ``Page.transformation_matrix`` 是 PDF→页面坐标的变换；先做去旋转，再用其
    逆变换翻回 PDF 空间（含 mediabox 原点/旋转），与 ``Annot.set_rect`` 写出的
    值一致。
    """
    inverse = ~page.transformation_matrix
    return (rect * page.derotation_matrix) * inverse


def _pdf_rect_string(rect: pymupdf.Rect) -> str:
    return (
        f"[{rect.x0:.4f} {rect.y0:.4f} {rect.x1:.4f} {rect.y1:.4f}]"
    )


def _page_link_annots(page: pymupdf.Page) -> list[dict]:
    """页上全部 Link 注记 ``{xref, rect, link}``（含旧版无 xref 时的兜底）。"""
    annots: list[dict] = []
    for index, link in enumerate(page.get_links()):
        rect = link.get("from")
        if rect is None:
            continue
        xref = link.get("xref")
        if not xref:
            xrefs = _page_link_xrefs(page)
            if index < len(xrefs):
                xref = xrefs[index]
        annots.append({"xref": xref, "rect": rect, "link": link})
    return annots


def _append_to_array_string(array_string: str, xref: int) -> str:
    value = array_string.strip()
    if not value.startswith("["):
        value = "[" + value
    if value.endswith("]"):
        return value[:-1].rstrip() + f" {xref} 0 R]"
    return value + f" {xref} 0 R]"


def _append_annot_xref(page: pymupdf.Page, xref: int) -> None:
    """把注记 xref 追加进页 /Annots；保留间接数组结构。"""
    doc = page.parent
    kind, value = doc.xref_get_key(page.xref, "Annots")
    if kind == "xref":
        array_xref = int(value.split()[0])
        array_string = doc.xref_object(array_xref)
        doc.update_object(array_xref, _append_to_array_string(array_string, xref))
        return
    if kind == "array":
        doc.xref_set_key(page.xref, "Annots", _append_to_array_string(value, xref))
        return
    doc.xref_set_key(page.xref, "Annots", f"[{xref} 0 R]")


def _clone_annot_with_rect(page: pymupdf.Page, xref: int, rect: pymupdf.Rect) -> int:
    """克隆注记（同一 ``/A`` / ``/Dest`` 动作）并改 ``/Rect``。返回新 xref。

    上游 ``insert_link`` 会重写动作、丢未知字段，所以跨行拆分的标记链接用
    **原始对象串克隆**：只替换 ``/Rect``，其余动作/边框原样保留。
    """
    doc = page.parent
    base = doc.xref_object(xref)
    pdf_rect = _page_rect_to_pdf(page, rect)
    clone = re.sub(
        r"/Rect\s*\[[^\]]*\]",
        "/Rect " + _pdf_rect_string(pdf_rect),
        base,
        count=1,
    )
    if clone == base:
        # 极端情况下对象省略 /Rect（不该发生）→ 直接插入。
        clone = base.replace("<<", "<</Rect " + _pdf_rect_string(pdf_rect), 1)
    # 去掉克隆体的 /NM，避免重名；其余字段保留。
    clone = re.sub(r"/NM\s*\([^)]*\)", "", clone)
    new_xref = doc.get_new_xref()
    doc.update_object(new_xref, clone)
    _append_annot_xref(page, new_xref)
    return new_xref


def _set_annot_rects(page: pymupdf.Page, xref: int, rects: list) -> int:
    """把一条注记的 ``/Rect`` 改成 ``rects``；多个矩形时克隆追加。

    返回写入的物理注记数。只改 ``/Rect``，动作与其余字段不动。
    """
    doc = page.parent
    written = 0
    for rect in rects:
        if rect is None or rect.is_empty:
            continue
        if written == 0:
            pdf_rect = _page_rect_to_pdf(page, rect)
            doc.xref_set_key(xref, "Rect", _pdf_rect_string(pdf_rect))
        else:
            _clone_annot_with_rect(page, xref, rect)
        written += 1
    return written


# --------------------------------------------------------------------------- #
# remap 主流程
# --------------------------------------------------------------------------- #
def _stamp_rects_page(rects) -> list:
    """印章标记矩形（页面坐标，左上原点）→ 逐行矩形列表（保序去重）。"""
    out: list = []
    for rect in rects or []:
        try:
            candidate = pymupdf.Rect(
                float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1)
            )
        except (AttributeError, TypeError, ValueError):
            continue
        if candidate.is_empty:
            continue
        out.append(candidate)
    return out


def _il_to_page_inverse(rect: pymupdf.Rect, page_height: float) -> pymupdf.Rect:
    """页面坐标 → IL 坐标（``_il_to_page`` 的逆）。"""
    return pymupdf.Rect(
        rect.x0, page_height - rect.y1, rect.x1, page_height - rect.y0
    )


def _stamp_rect_union(rects, page_height: float) -> pymupdf.Rect | None:
    """印章标记矩形（页面坐标）→ IL 坐标并集（兼容旧调用方）。"""
    union: pymupdf.Rect | None = None
    for rect in _stamp_rects_page(rects):
        candidate = _il_to_page_inverse(rect, page_height)
        union = candidate if union is None else (union | candidate)
    return union


def remap_page_links(
    dst_page: pymupdf.Page,
    page_links: list[dict],
    page_char_objects: list,
    paragraph_index: dict,
    page_height: float,
    alive_ids: set[int] | None = None,
    stamp_rects: dict[int, list] | None = None,
) -> LinkRemapResult:
    """按源字符/锚点/段落映射重算一页链接矩形，就地改写注记 ``/Rect``。

    ``dst_page`` 是从源 PDF 打开、内容流已被整体替换的 mono 页——页上链接注释
    仍是源矩形；按 ``from`` 矩形与快照条目一一对应后**原地更新 /Rect**（不删
    注记），因此任意动作类型/边框/目标视图语义都被保留。

    ``stamp_rects``（link_index → 页面坐标矩形列表，pymupdf 左上原点）来自
    LaTeX 印章内的 ``bdoclink`` 标记注记；跨行时保留逐行矩形（克隆注记）。
    """
    result = LinkRemapResult(total=len(page_links))
    if not page_links:
        return result

    by_rect: dict[tuple, list] = {}
    for annot in _page_link_annots(dst_page):
        by_rect.setdefault(_rect_key(annot["rect"]), []).append(annot)

    text_cache: dict = {}
    anchor_usage: dict = {}

    for entry in page_links:
        from_rect = entry.get("from")
        if from_rect is None:
            result.unresolved.append(
                _unresolved_entry(entry, reason="no_source_rect")
            )
            continue
        candidates = by_rect.get(_rect_key(pymupdf.Rect(*from_rect)))
        if not candidates:
            result.unresolved.append(
                _unresolved_entry(entry, reason="source_annotation_missing")
            )
            continue
        annot = candidates.pop(0)
        xref = annot.get("xref")
        old_link = annot.get("link")

        page_rects = _stamp_rects_page(
            (stamp_rects or {}).get(entry.get("link_index"))
        )
        if page_rects:
            method = "stamp"
        else:
            il_rect, method = resolve_link_rect(
                entry,
                page_char_objects,
                paragraph_index,
                alive_ids,
                dst_page=dst_page,
                page_height=page_height,
                text_cache=text_cache,
                anchor_usage=anchor_usage,
            )
            if il_rect is None:
                result.unresolved.append(
                    _unresolved_entry(entry, method=method, reason="no_geometry")
                )
                continue
            page_rects = [_il_to_page(il_rect, page_height)]

        page_rects = [
            rect for rect in page_rects if rect is not None and not rect.is_empty
        ]
        if not page_rects:
            result.unresolved.append(
                _unresolved_entry(entry, method=method, reason="empty_rect")
            )
            continue

        try:
            written = _write_link_geometry(dst_page, xref, old_link, page_rects)
        except Exception:  # noqa: BLE001 - 单条链接失败不应中断重建
            logger.debug("链接重映射失败: %s", entry.get("link_index"), exc_info=True)
            result.unresolved.append(
                _unresolved_entry(entry, method=method, reason="write_failed")
            )
            continue
        if written <= 0:
            result.unresolved.append(
                _unresolved_entry(entry, method=method, reason="write_failed")
            )
            continue

        result.remapped += 1
        result.physical_annotations += written
        if written > 1:
            result.split_annotations += 1
        if method == "stamp":
            result.stamp_resolved += 1
        elif method == "char_union":
            result.char_union_resolved += 1
        elif method == "anchor":
            result.anchor_resolved += 1
        elif method == "anchor_group":
            result.anchor_group_resolved += 1
        elif method == "paragraph":
            result.fallback_paragraph += 1
        elif method == "proportional":
            result.fallback_paragraph += 1
            result.fallback_proportional += 1
        if method not in EXACT_METHODS:
            result.fallbacks.append(
                {
                    "link_index": entry.get("link_index"),
                    "uri": entry.get("uri"),
                    "method": method,
                    "reason": _fallback_reason(entry, method),
                }
            )
    if result.remapped:
        # 就地 xref 写入不会刷新 pymupdf 的页内 link 读缓存（``get_links`` 仍
        # 返回旧矩形）。dual 搬运直接读 mono 页的 ``get_links``，所以这里主动
        # reload 一次，保证同一进程内后续读到的是新矩形（落盘结果本来正确）。
        _refresh_page_link_cache(dst_page)
    return result


def _refresh_page_link_cache(page) -> None:
    """就地 /Rect 写入后刷新 pymupdf 页内链接读缓存。

    优先用 ``JM_refresh_links``：它在原页对象上重建 link 表，不失效调用方持有的
    Page。旧版 pymupdf 没有该函数时退回 ``reload_page``（会返回新页对象，调用方
    需按页码重取）。
    """
    refresh = getattr(pymupdf, "JM_refresh_links", None)
    if refresh is not None:
        try:
            refresh(page._pdf_page())
            return
        except Exception:  # noqa: BLE001 - 退回 reload_page
            logger.debug("JM_refresh_links 失败，退回 reload_page", exc_info=True)
    reload_page = getattr(getattr(page, "parent", None), "reload_page", None)
    if reload_page is None:
        return
    try:
        reload_page(page)
    except Exception:  # noqa: BLE001 - 刷新失败不影响已写入的 xref
        logger.debug("链接读缓存刷新失败", exc_info=True)


def _write_link_geometry(page, xref, old_link, page_rects: list) -> int:
    """写回链接几何：优先原地更新 xref ``/Rect``；无 xref 时保守重插。

    无 xref（极端旧文档）时先用 ``safe_insert_link`` 插入副本，成功后再删原链接，
    保证插入失败不会丢源链接。
    """
    if xref:
        return _set_annot_rects(page, xref, page_rects)

    new_links = []
    for rect in page_rects:
        link = _rebuild_link(old_link or {}, rect)
        if link is None:
            continue
        new_links.append(link)
    if not new_links:
        return 0
    inserted = 0
    for link in new_links:
        try:
            safe_insert_link(page, link)
            inserted += 1
        except Exception:  # noqa: BLE001
            logger.debug("链接插入失败", exc_info=True)
    if inserted == 0:
        return 0
    try:
        page.delete_link(old_link)
    except Exception:  # noqa: BLE001 - 删除失败时保留旧链接（宁多勿丢）
        logger.debug("旧链接删除失败", exc_info=True)
    return inserted


def _fallback_reason(_entry: dict, method: str) -> str:
    if method == "proportional":
        return "译文段内无唯一文本锚点；按源段内比例投影（近似）"
    if method == "paragraph":
        return "译文段内无文本锚点；退到整段 box（粗粒度）"
    if method == "anchor_group":
        return "同段内同标签多次出现；按源相对位置就近分配（近似）"
    return method


def _unresolved_entry(
    entry: dict, *, method: str = "unresolved", reason: str | None = None
) -> dict:
    out = {"link_index": entry.get("link_index"), "uri": entry.get("uri")}
    if method:
        out["method"] = method
    if reason:
        out["reason"] = reason
    return out


def _rect_key(rect) -> tuple:
    return (
        round(float(rect.x0), 2),
        round(float(rect.y0), 2),
        round(float(rect.x1), 2),
        round(float(rect.y1), 2),
    )


def _rebuild_link(link: dict, rect) -> dict | None:
    """按原 link 构造可直接 insert 的新链接（命名目的地转直接目标）。

    ``LINK_NAMED`` 分两种：带 ``page`` 的（可直接跳 → 转成 ``LINK_GOTO``，不依赖
    命名目的地）与只带 ``nameddest`` 的（保留命名目的地）。``LINK_LAUNCH`` /
    ``LINK_GOTOR``（``file`` 目标）按 file 复制（pymupdf 落盘会改写回 GOTOR）。
    """
    kind = link.get("kind")
    if kind == pymupdf.LINK_URI and (link.get("uri") or link.get("file")):
        uri = link.get("uri") or link.get("file")
        return {"kind": pymupdf.LINK_URI, "from": rect, "uri": uri}
    if kind in (pymupdf.LINK_LAUNCH, pymupdf.LINK_GOTOR) and link.get("file"):
        return {"kind": pymupdf.LINK_LAUNCH, "from": rect, "file": link["file"]}
    if kind in (pymupdf.LINK_GOTO, pymupdf.LINK_NAMED):
        if link.get("page") is not None:
            out = {"kind": pymupdf.LINK_GOTO, "from": rect, "page": link["page"]}
            to = link.get("to")
            # ``to`` 可能是 Point、字符串（命名目的地）或缺失：仅在 Point 时携带。
            if to is not None and hasattr(to, "x") and hasattr(to, "y"):
                out["to"] = pymupdf.Point(to.x, to.y)
            return out
        nameddest = link.get("nameddest")
        if nameddest:
            return {
                "kind": pymupdf.LINK_NAMED,
                "from": rect,
                "nameddest": nameddest,
            }
    return None


def _page_link_xrefs(page: pymupdf.Page) -> list[int]:
    """页 /Annots 数组里的全部注解 xref（直读 live 对象，避开读缓存）。"""
    doc = page.parent
    kind, value = doc.xref_get_key(page.xref, "Annots")
    if kind == "xref":
        array_xref = int(value.split()[0])
        value = doc.xref_object(array_xref)
        kind = "array"
    if kind != "array":
        return []
    return [int(m) for m in re.findall(r"(\d+)\s+0\s+R", value)]


def safe_insert_link(page: pymupdf.Page, link: dict) -> None:
    """``insert_link`` 的 pymupdf 1.27 缺陷防御：插入后校验并修复字符串字段。

    上游缺陷：``utils.getLinkText`` 用无界 ``str.replace("/Link", "/Link/NM(name)")``
    给新注记注入 ``/NM``，会连带把 URI/file/nameddest 字段内出现的 ``"/Link"``
    子串改写（实测 ``https://llvm.org/docs/LinkTimeOptimization.html`` 被污染成
    ``.../Link/NM(fitz-L13)TimeOptimization.html``，触发 URI 集合门禁失败）。

    这里在目标字段含 ``"/Link"`` 时，插入后通过 live 对象读取新注记的真实
    落盘值（``annot_xrefs``/``get_links`` 的读缓存可能滞后），不一致则用
    ``xref_set_key`` 修复。
    """
    uri = link.get("uri")
    file_target = link.get("file")
    nameddest = link.get("nameddest")
    if not any("/Link" in str(v) for v in (uri, file_target, nameddest) if v):
        page.insert_link(link)
        return

    before = set(_page_link_xrefs(page))
    page.insert_link(link)
    doc = page.parent
    for xref in _page_link_xrefs(page):
        if xref in before:
            continue
        if uri:
            kind, value = doc.xref_get_key(xref, "A/URI")
            if kind == "string" and _pdf_string_value(value) != uri:
                doc.xref_set_key(xref, "A/URI", pymupdf.get_pdf_str(uri))
        if file_target:
            kind, value = doc.xref_get_key(xref, "A/F/F")
            if kind == "string" and _pdf_string_value(value) != file_target:
                doc.xref_set_key(xref, "A/F/F", pymupdf.get_pdf_str(file_target))
                doc.xref_set_key(xref, "A/F/UF", pymupdf.get_pdf_str(file_target))
        if nameddest:
            kind, value = doc.xref_get_key(xref, "A/D")
            if kind == "string" and _pdf_string_value(value) != nameddest:
                doc.xref_set_key(xref, "A/D", pymupdf.get_pdf_str(nameddest))


def _pdf_string_value(raw: str) -> str:
    """``xref_get_key`` 返回的 PDF 字符串值 → 去括号与反转义。"""
    value = raw.strip()
    if value.startswith("(") and value.endswith(")"):
        value = value[1:-1]
    return value.replace(r"\(", "(").replace(r"\)", ")").replace("\\\\", "\\")


def uri_set(pdf) -> set[str]:
    """文档中所有 URI 链接的集合（门禁用）。"""
    uris: set[str] = set()
    for page in pdf:
        for link in page.get_links():
            if link.get("uri"):
                uris.add(link["uri"])
    return uris


def assert_uri_set_matches(
    expected: set[str], actual: set[str], *, context: str
) -> None:
    """URI 集合门禁：不一致时抛 ``RuntimeError``（含缺失/新增样例）。"""
    if expected == actual:
        return
    missing = sorted(expected - actual)[:5]
    extra = sorted(actual - expected)[:5]
    raise RuntimeError(
        f"link_uri_set_mismatch ({context}): "
        f"源 URI {len(expected)} 条 / 输出 {len(actual)} 条；"
        f"缺失 {missing}；新增 {extra}"
    )


def normalize_link_action(doc, link: dict) -> dict:
    """Normalize a MuPDF link dictionary while preserving action metadata."""
    out = dict(link)
    out.setdefault('kind', link.get('kind'))
    if out.get('kind') == pymupdf.LINK_URI:
        out['uri'] = link.get('uri') or ''
    return out
