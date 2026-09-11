"""PDF 注释快照：目录（书签）与超链接。

对应 ``.plan/minerU深度融合.md`` 板块 4/5 的解析侧契约：重建阶段不能依赖
「在译文里搜同名文字」来定位注释——必须在解析阶段就把源 PDF 的注释结构与
它覆盖的源字符/源页绑定下来，重建时按对象身份映射回去。

本模块实现两类快照：

- **书签快照**（板块 4）：``<workdir>/agent/source/bookmarks.json``；
- **超链接快照**（板块 5）：``<workdir>/agent/source/links.json``，并把
  「链接 → 源字符下标 / 所属段落 id」写进 state.pkl（``link_snapshot`` /
  ``page_char_objects``），供重建阶段按对象身份重算矩形。

产物：``<workdir>/agent/source/bookmarks.json``

```json
[
  {"level": 1, "title": "Introduction", "page": 4, "to": [70.87, 756.85],
   "nameddest": "section.1", "collapse": false}
]
```

``page`` 是 1-based 页码（``doc.get_toc()`` 口径）；``to`` 是目标点坐标
（``Point``，页面坐标），无显式目标时为 ``null``。``nameddest`` 保留源 PDF 的
命名目的地，便于重建时对照。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger(__name__)

BOOKMARKS_ARTIFACT = "bookmarks.json"
LINKS_ARTIFACT = "links.json"
# 书签标题的最大保留长度（防御异常长标题）
MAX_TITLE_CHARS = 400
# 链接矩形与字符相交的判定阈值（交面积 / 字符面积）。
LINK_CHAR_OVERLAP_THRESHOLD = 0.3


def iter_page_paragraph_chars(paragraph):
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


def collect_page_chars(page) -> list:
    """页内字符的规范枚举顺序：段落 composition 字符，再是未入段的残存字符。

    ``char_indices`` 的语义就是这个列表的下标。该顺序在 extract 时确定并
    pickle 进 state（对象身份保持一致），重建阶段用同一列表读 box。
    """
    chars: list = []
    for paragraph in page.pdf_paragraph or []:
        chars.extend(iter_page_paragraph_chars(paragraph))
    chars.extend(page.pdf_character or [])
    return chars


def _char_box(char):
    visual = getattr(char, "visual_bbox", None)
    if visual is not None and getattr(visual, "box", None) is not None:
        return visual.box
    return getattr(char, "box", None)


def _char_box_tuple(char) -> tuple[float, float, float, float] | None:
    box = _char_box(char)
    if box is None:
        return None
    if any(getattr(box, name, None) is None for name in ("x", "y", "x2", "y2")):
        return None
    return (float(box.x), float(box.y), float(box.x2), float(box.y2))


def _paragraph_box_tuple(paragraph) -> tuple[float, float, float, float] | None:
    """段落 box → (x0, y0, x1, y1) 元组（IL 坐标）。"""
    if paragraph is None:
        return None
    box = getattr(paragraph, "box", None)
    if box is None or any(
        getattr(box, name, None) is None for name in ("x", "y", "x2", "y2")
    ):
        return None
    return (float(box.x), float(box.y), float(box.x2), float(box.y2))


def _relative_ratio(
    region: tuple[float, float, float, float],
    src_box: tuple[float, float, float, float],
) -> tuple[float, float, float, float] | None:
    """源矩形在源段落 box 内的相对位置（0..1 四元组）。

    供译文段内同比例投影：译文行数/宽度变化后，按比例定位比整段 box 精确。
    区域完全在段外（宽度为 0 的投影）时返回 None。
    """
    width = src_box[2] - src_box[0]
    height = src_box[3] - src_box[1]
    if width <= 0 or height <= 0:
        return None
    ratio = (
        max(0.0, (region[0] - src_box[0]) / width),
        max(0.0, (region[1] - src_box[1]) / height),
        min(1.0, (region[2] - src_box[0]) / width),
        min(1.0, (region[3] - src_box[1]) / height),
    )
    if ratio[2] <= ratio[0] or ratio[3] <= ratio[1]:
        return None
    return ratio


def _overlap_ratio(char_box, region) -> float:
    """字符面积落在区域内的比例（0..1）。参数均为 ``(x0, y0, x1, y1)`` 元组。"""
    cx0, cy0, cx1, cy1 = char_box
    area = max(0.0, cx1 - cx0) * max(0.0, cy1 - cy0)
    if area <= 0:
        return 0.0
    x0 = max(cx0, region[0])
    y0 = max(cy0, region[1])
    x1 = min(cx1, region[2])
    y1 = min(cy1, region[3])
    if x1 <= x0 or y1 <= y0:
        return 0.0
    return (x1 - x0) * (y1 - y0) / area


def _center_inside(char_box, region) -> bool:
    cx = (char_box[0] + char_box[2]) / 2
    cy = (char_box[1] + char_box[3]) / 2
    return region[0] <= cx <= region[2] and region[1] <= cy <= region[3]


def resolve_link_chars(links: list[dict], page, page_height: float) -> list[dict]:
    """把一页链接关联到源字符下标与所属段落 id。

    匹配口径：字符中心落在链接矩形内，或字符面积与矩形的交 ≥ 30%。
    必须在 Typesetting 之前调用（那时字符 box 仍是源坐标）。
    """
    chars = collect_page_chars(page)
    # 字符 → 所属段落 debug_id；段落 debug_id → 段落对象（算源段落 box 用）
    owner: dict[int, str] = {}
    paragraph_by_id: dict[str, object] = {}
    index = 0
    for paragraph in page.pdf_paragraph or []:
        debug_id = paragraph.debug_id
        if debug_id:
            paragraph_by_id[debug_id] = paragraph
        for _char in iter_page_paragraph_chars(paragraph):
            if debug_id:
                owner[index] = debug_id
            index += 1

    annotated: list[dict] = []
    for link in links:
        from_rect = link.get("from")
        entry = dict(link)
        entry["char_indices"] = []
        entry["paragraph_ids"] = []
        if from_rect is None:
            annotated.append(entry)
            continue
        import pymupdf

        rect = pymupdf.Rect(*from_rect)
        # 链接矩形是 pymupdf 坐标（左上原点），统一翻到 IL 坐标（左下原点）
        # 后与字符 box 直接比较。
        region = (rect.x0, page_height - rect.y1, rect.x1, page_height - rect.y0)
        hit_indices: list[int] = []
        hit_paragraphs: list[str] = []
        for char_index, char in enumerate(chars):
            char_box = _char_box_tuple(char)
            if char_box is None:
                continue
            if _center_inside(char_box, region) or (
                _overlap_ratio(char_box, region) >= LINK_CHAR_OVERLAP_THRESHOLD
            ):
                hit_indices.append(char_index)
                pid = owner.get(char_index)
                if pid and pid not in hit_paragraphs:
                    hit_paragraphs.append(pid)
        entry["char_indices"] = hit_indices
        entry["paragraph_ids"] = hit_paragraphs
        # 段落回退时的相对几何：源矩形与首个命中段落的源 box 的相对比例，
        # 供 link_remap 在译文段内做同比例投影（比整段 box 精确得多）。
        if hit_paragraphs:
            first_para = paragraph_by_id.get(hit_paragraphs[0])
            src_box = _paragraph_box_tuple(first_para)
            if src_box is not None:
                entry["src_rect_ratio"] = _relative_ratio(region, src_box)
        annotated.append(entry)
    return annotated


def snapshot_links(pdf_path: str | Path) -> dict:
    """读取 PDF 超链接注释，按页返回 JSON 可序列化的元数据。

    ```json
    {"0": [{"link_index": 0, "page_index": 0, "kind": "URI",
             "uri": "https://...", "page": null,
             "to": [x, y], "from": [x0, y0, x1, y1]}], ...}
    ```
    """
    import pymupdf

    doc = pymupdf.open(str(pdf_path))
    try:
        pages: dict[str, list[dict]] = {}
        for page_index in range(doc.page_count):
            entries: list[dict] = []
            for link_index, link in enumerate(doc[page_index].get_links()):
                rect = link.get("from")
                if rect is None:
                    continue
                to_point = link.get("to")
                entries.append(
                    {
                        "link_index": link_index,
                        "page_index": page_index,
                        "kind": _kind_name(link.get("kind")),
                        "uri": _link_uri(link),
                        "page": link.get("page"),
                        "to": (
                            [float(to_point.x), float(to_point.y)]
                            if to_point is not None
                            else None
                        ),
                        "from": [
                            float(rect.x0),
                            float(rect.y0),
                            float(rect.x1),
                            float(rect.y1),
                        ],
                    }
                )
            if entries:
                pages[str(page_index)] = entries
        return pages
    finally:
        doc.close()


def _kind_name(kind) -> str:
    """链接 kind → 名称。pymupdf 不同版本可能返回 int 或数字字符串。"""
    try:
        value = int(kind)
    except (TypeError, ValueError):
        return str(kind)
    return _KIND_NAMES.get(value, str(kind))


_KIND_NAMES = {
    2: "URI",
    1: "GOTO",
    4: "NAMED",
    3: "LAUNCH",
}


def _link_uri(link: dict) -> str | None:
    """取链接的 URI：pymupdf 对 URL 型链接用 ``uri``，部分构造路径落到 ``file``。"""
    uri = link.get("uri")
    if uri:
        return uri
    file_value = link.get("file")
    if file_value and str(file_value).startswith(("http://", "https://")):
        return str(file_value)
    return None


def build_link_state(pdf_path: str | Path, docs) -> dict:
    """构建 state 用的链接映射状态（**必须在 Typesetting 之前**调用）。

    返回 ``{"link_snapshot": {page_index: entries}, "page_char_objects": {page_index: [PdfCharacter, ...]}}``：
    ``page_char_objects`` 与快照里的 ``char_indices`` 用同一枚举顺序，
    因此 pickle 后重建阶段可按下标取回同一对象。
    """
    raw = snapshot_links(pdf_path)
    page_by_index = {page.page_number: page for page in docs.page}
    link_snapshot: dict[int, list[dict]] = {}
    char_objects: dict[int, list] = {}
    for key, entries in raw.items():
        page_index = int(key)
        page = page_by_index.get(page_index)
        if page is None:
            continue
        if page.cropbox is None or page.cropbox.box is None:
            continue
        page_height = float(page.cropbox.box.y2) - float(page.cropbox.box.y)
        link_snapshot[page_index] = resolve_link_chars(entries, page, page_height)
        char_objects[page_index] = collect_page_chars(page)
    return {"link_snapshot": link_snapshot, "page_char_objects": char_objects}


def write_links(snapshot: dict, out_path: str | Path) -> dict:
    """落盘 links.json（审计产物）。失败只 warning。"""
    out_path = Path(out_path)
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(out_path)
    except OSError:
        logger.warning("写入 links.json 失败", exc_info=True)
        return {"path": str(out_path), "written": False}
    count = sum(len(entries) for entries in snapshot.values())
    return {"path": str(out_path), "count": count, "written": True}


def links_path(agent_dir: str | Path) -> Path:
    """``<agent>/source/links.json``。"""
    return Path(agent_dir) / "source" / LINKS_ARTIFACT


def snapshot_bookmarks(pdf_path: str | Path) -> list[dict]:
    """读取 PDF 书签（outline），返回可 JSON 序列化的列表。

    用 ``get_toc(simple=False)`` 拿完整目标信息（``to`` / ``nameddest``）；
    环境不支持时退回 ``get_toc()``（只有 level/title/page）。
    """
    import pymupdf

    doc = pymupdf.open(str(pdf_path))
    try:
        try:
            raw = doc.get_toc(simple=False)
        except Exception:  # noqa: BLE001 - 老版本/异常结构回退
            logger.debug("get_toc(simple=False) 失败，回退 simple", exc_info=True)
            raw = doc.get_toc()
    finally:
        doc.close()

    entries: list[dict] = []
    for item in raw:
        if not isinstance(item, list | tuple) or len(item) < 3:
            continue
        level, title, page = item[0], item[1], item[2]
        detail = item[3] if len(item) > 3 and isinstance(item[3], dict) else {}
        to_point = detail.get("to")
        entries.append(
            {
                "level": int(level),
                "title": str(title or "")[:MAX_TITLE_CHARS],
                "page": int(page),
                "to": (
                    [float(to_point.x), float(to_point.y)]
                    if to_point is not None
                    else None
                ),
                "nameddest": detail.get("nameddest"),
                "collapse": bool(detail.get("collapse") or False),
            }
        )
    return entries


def write_bookmarks(pdf_path: str | Path, out_path: str | Path) -> dict:
    """快照书签并落盘；返回 ``{"path", "count"}``。失败只 warning。"""
    out_path = Path(out_path)
    try:
        entries = snapshot_bookmarks(pdf_path)
    except Exception:  # noqa: BLE001 - 注释快照是审计产物，不阻断解析
        logger.warning("书签快照失败: %s", pdf_path, exc_info=True)
        entries = []
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_suffix(".tmp")
        tmp.write_text(
            json.dumps(entries, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(out_path)
    except OSError:
        logger.warning("写入 bookmarks.json 失败", exc_info=True)
        return {"path": str(out_path), "count": len(entries), "written": False}
    return {"path": str(out_path), "count": len(entries), "written": True}


def bookmarks_path(agent_dir: str | Path) -> Path:
    """``<agent>/source/bookmarks.json``。"""
    return Path(agent_dir) / "source" / BOOKMARKS_ARTIFACT
