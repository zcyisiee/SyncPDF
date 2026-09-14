"""超链接矩形重映射：按源字符身份定位译文中的新矩形。

背景（对应 ``.plan/minerU深度融合.md`` 板块 5）：mono PDF 是从源 PDF 打开的
（内容流整体重发），Link 注释仍挂在页上但**矩形停在旧坐标**。旧实现
``_remap_links_by_text`` 在译文里搜同名文字来重定位，模型改写了文字（翻译/断行）
后就搜不到或搜错位置，导致链接漂到无关文字上。

本模块改用**对象身份映射**：解析阶段把每条链接覆盖的源 ``PdfCharacter``
对象记下来（``link_snapshot`` 落盘下标，state.pkl 里 pickle 对象列表保持身份），
重建阶段（Typesetting 之后）按优先级算新矩形：

1. **字符并集**：链接覆盖的源字符对象仍存活在某个段落 composition 里
   （公式 / 富文本 passthrough / 被跳过未翻译的段落）→ 取它们当前 box 的并集。
2. **段落 box**：源字符所在段落被整体重排（Typesetting 为译文新建字符对象，
   源对象不再出现在 composition 中）→ 用该段落当前的 ``paragraph.box``
   （粗粒度但稳定；实测与字符并集的 IoU 中位数 0.93）。
3. **unresolved**：都不可用 → 保持原矩形，记入 ``link_unresolved``（不删链接）。

存活判定走 ``build_alive_char_ids()``：Typesetting 之后扫一遍所有 composition，
被引用的对象 ``id()`` 进集合。``PdfCharacter`` 带 ``__slots__``，无法在其上
打标记，因此这里用进程内 ``id()`` 集合（Typesetting 与 remap 在同一进程内完成）。

坐标系：Link 矩形来自 ``page.get_links()``，是 pymupdf 页面坐标（左上原点）；
IL 的 ``PdfCharacter.box`` 是 PDF 坐标（左下原点）。换算 ``y' = H - y``。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from dataclasses import field

import pymupdf

logger = logging.getLogger(__name__)

# 字符并集时忽略细碎 box（宽度或高度 < 0.1pt，通常是退化字形）。
MIN_CHAR_EXTENT = 0.1


@dataclass(slots=True)
class LinkRemapResult:
    """一页的链接重映射结果。"""

    total: int = 0
    remapped: int = 0
    fallback_paragraph: int = 0
    stamp_resolved: int = 0
    unresolved: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "total": self.total,
            "remapped": self.remapped,
            "fallback_paragraph": self.fallback_paragraph,
            "stamp_resolved": self.stamp_resolved,
            "unresolved": list(self.unresolved),
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


def resolve_link_rect(
    entry: dict,
    page_char_objects: list,
    paragraph_index: dict,
    alive_ids: set[int] | None = None,
) -> tuple[pymupdf.Rect | None, str]:
    """算一条链接在 IL 坐标下的新矩形。返回 ``(rect, method)``。

    三级回退（与 ``.plan/minerU深度融合.md`` 板块 5 一致）：

    1. ``char_union``：链接覆盖的源字符对象仍存活于 composition（公式/富文本
       passthrough/被跳过未翻译的段落）→ 取它们当前 box 的并集；
    2. ``paragraph``：源字符所在段落被整体重排（Typesetting 为译文新建了字符
       对象）→ 用该段落当前的 ``paragraph.box``（粗粒度但稳定）；
    3. ``unresolved``：都不可用 → 保持原矩形。

    为何不用「按源几何比例把矩形收窄到段落内某一处」：译文会重排/换行，源坐标
    与译文坐标不再对应（实测用段内比例投影后，矩形能盖到「对应文字」的比例从
    99.3% 降到 21.8%）；点击区域宁可大一点，也不能落到无关文字上。
    """
    chars = [
        page_char_objects[index]
        for index in (entry.get("char_indices") or [])
        if 0 <= index < len(page_char_objects)
    ]
    rect = il_box_union(chars, alive_ids)
    if rect is not None:
        return rect, "char_union"

    for paragraph_id in entry.get("paragraph_ids") or []:
        paragraph = paragraph_index.get(paragraph_id)
        if paragraph is None:
            continue
        rect = _box_to_rect(getattr(paragraph, "box", None))
        if rect is None:
            continue
        # 段内相对几何投影：链接源矩形在源段内的比例位置 → 译文段内同比例位置。
        # 比整段 box 精确（URL/引用号链接只盖段落一小部分），且不依赖文字搜索。
        ratio = entry.get("src_rect_ratio")
        if ratio:
            projected = _project_ratio(rect, ratio)
            if projected is not None and not projected.is_empty:
                return projected, "paragraph"
        return rect, "paragraph"

    return None, "unresolved"


def remap_page_links(
    dst_page: pymupdf.Page,
    page_links: list[dict],
    page_char_objects: list,
    paragraph_index: dict,
    page_height: float,
    alive_ids: set[int] | None = None,
    stamp_rects: dict[int, list] | None = None,
) -> LinkRemapResult:
    """按源字符映射重算一页的链接矩形并写回 ``dst_page``。

    ``dst_page`` 是从源 PDF 打开、内容流已被整体替换的 mono 页——页上链接注释
    仍是源矩形；按 ``from`` 矩形与快照条目一一对应后替换为映射矩形。

    ``stamp_rects``（link_index → 页面坐标矩形列表，pymupdf 左上原点）来自
    LaTeX 印章内的 ``bdoclink`` 标记注记：那是上标引文在印章里的**真实墨迹
    位置**，精度高于一切几何推导，作为最高优先级。
    """
    result = LinkRemapResult(total=len(page_links))
    if not page_links:
        return result

    by_rect: dict[tuple, list] = {}
    for link in dst_page.get_links():
        rect = link.get("from")
        if rect is not None:
            by_rect.setdefault(_rect_key(rect), []).append(link)

    for entry in page_links:
        from_rect = entry.get("from")
        if from_rect is None:
            continue
        candidates = by_rect.get(_rect_key(pymupdf.Rect(*from_rect)))
        if not candidates:
            result.unresolved.append(_unresolved_entry(entry))
            continue
        old_link = candidates.pop(0)
        stamp_il = _stamp_rect_union(
            (stamp_rects or {}).get(entry.get("link_index")), page_height
        )
        if stamp_il is not None:
            rect, method = stamp_il, "stamp"
        else:
            rect, method = resolve_link_rect(
                entry, page_char_objects, paragraph_index, alive_ids
            )
        if rect is None:
            result.unresolved.append(_unresolved_entry(entry))
            continue
        # IL（左下原点）→ pymupdf（左上原点）
        new_rect = pymupdf.Rect(
            rect.x0, page_height - rect.y1, rect.x1, page_height - rect.y0
        )
        new_link = _rebuild_link(old_link, new_rect)
        if new_rect.is_empty or new_link is None:
            result.unresolved.append(_unresolved_entry(entry))
            continue
        try:
            dst_page.delete_link(old_link)
            safe_insert_link(dst_page, new_link)
        except Exception:  # noqa: BLE001 - 单条链接失败不应中断重建
            logger.debug("链接重映射失败: %s", entry.get("link_index"), exc_info=True)
            result.unresolved.append(_unresolved_entry(entry))
            continue
        result.remapped += 1
        if method == "paragraph":
            result.fallback_paragraph += 1
        elif method == "stamp":
            result.stamp_resolved += 1
    return result


def _stamp_rect_union(rects, page_height: float) -> pymupdf.Rect | None:
    """印章标记矩形（页面坐标，左上原点）→ IL 坐标并集。

    跨行断开的链接会有多个矩形（hyperref 每行一个注记）：取并集保持
    快照条目与页面注记 1:1（点击区域宁可稍大）。
    """
    union: pymupdf.Rect | None = None
    for rect in rects or []:
        try:
            candidate = pymupdf.Rect(
                float(rect.x0),
                page_height - float(rect.y1),
                float(rect.x1),
                page_height - float(rect.y0),
            )
        except (AttributeError, TypeError, ValueError):
            continue
        if candidate.is_empty:
            continue
        union = candidate if union is None else (union | candidate)
    return union


def _unresolved_entry(entry: dict) -> dict:
    return {"link_index": entry.get("link_index"), "uri": entry.get("uri")}


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
    命名目的地）与只带 ``nameddest`` 的（保留命名目的地）。
    """
    kind = link.get("kind")
    if kind == pymupdf.LINK_URI and (link.get("uri") or link.get("file")):
        uri = link.get("uri") or link.get("file")
        return {"kind": pymupdf.LINK_URI, "from": rect, "uri": uri}
    if kind in (pymupdf.LINK_GOTO, pymupdf.LINK_NAMED):
        if link.get("page") is not None:
            out = {"kind": pymupdf.LINK_GOTO, "from": rect, "page": link["page"]}
            to = link.get("to")
            if to is not None:
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
    import re

    doc = page.parent
    kind, value = doc.xref_get_key(page.xref, "Annots")
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
