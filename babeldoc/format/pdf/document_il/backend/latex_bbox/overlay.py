"""generate 阶段的选择性 LaTeX bbox overlay。

生命周期（``pdf_creater.write``）：

1. :meth:`LatexBboxOverlay.prepare` —— **内容流生成之前**：按资格门禁选段、
   编译 XeLaTeX 贴片，返回「已成功编译」的 debug_id 集合（``stamped_ids``）；
2. 调用方在生成内容流时跳过 ``stamped_ids`` 的字符（含公式 form/curve），
   已贴片段落的旧路径译文不再进内容流 —— 无双层文本靠构造保证；
3. :meth:`LatexBboxOverlay.stamp` —— 内容流生成之后：把贴片落到 bbox 矩形。

可选第四步（P6 编译后扩框）：``config.latex_bbox_box_overrides``
（debug_id → IL box）只替换贴片矩形，不动 Typesetting 输入框也不写
``layout_overrides.json``；由 ``layout.reconstruct_pdf`` 在首遍产物里有缩字段时
按译文 PDF 的 PP-DocLayoutV3 区域算出，再跑第二遍。扩框只向下，擦除仍只针对
原框（扩框区域不再擦除，否则会连带删掉邻居在内容流里的译文）。

redaction 只在「stamp rect 内仍有文本层」时兜底（正常流程下为空）：擦除必须走
``apply_redactions`` 物理移除（禁止白矩形 ``draw_rect`` 假擦除——那会留下双层
文本污染，架构原则 4 / FINAL.md §6）。redaction 会连带删除区域内 Link 注记：
overlay 前快照页内链接，redaction 后按原矩形重新插入，再交给现有 ``link_remap``
重定位；文档级校验（每页链接集合 + URI 集合）不通过则回滚（full 模式重新生成
受影响页内容流，repair 模式回退到 overlay 前字节快照），保证 URI 集合门禁永远
不会因 overlay 失败（架构原则 4/5）。

所有失败（能力缺失、编译失败、融合失败、几何不安全）都只是回退现有渲染路径并
计入结构化统计，绝不让翻译失败（架构原则 1）。``latex_bbox_mode="repair"`` 时
保留旧门的禁行为（``line-fill-ok`` / ``no-body-lines`` 词表、无预选跳过）。
"""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import re
import tempfile
import threading
from collections import Counter
from pathlib import Path
from statistics import median

import pymupdf

from babeldoc.format.pdf.document_il.backend.latex_bbox.capability import (
    probe_latex_capability,
)
from babeldoc.format.pdf.document_il.backend.latex_bbox.fusion import FRAGMENT_PATTERN
from babeldoc.format.pdf.document_il.backend.latex_bbox.fusion import FormulaLatexIndex
from babeldoc.format.pdf.document_il.backend.latex_bbox.fusion import FragmentRef
from babeldoc.format.pdf.document_il.backend.latex_bbox.fusion import fuse_paragraph
from babeldoc.format.pdf.document_il.backend.latex_bbox.fusion import (
    iter_composition_units,
)
from babeldoc.format.pdf.document_il.backend.latex_bbox.fusion import (
    render_fragment_latex,
)
from babeldoc.format.pdf.document_il.backend.latex_bbox.renderer import (
    DEFAULT_LEAD_RATIO,
)
from babeldoc.format.pdf.document_il.backend.latex_bbox.renderer import StampRequest
from babeldoc.format.pdf.document_il.backend.latex_bbox.renderer import StampResult
from babeldoc.format.pdf.document_il.backend.latex_bbox.renderer import derive_lead
from babeldoc.format.pdf.document_il.backend.latex_bbox.renderer_batch import (
    BatchStampRenderer,
)
from babeldoc.format.pdf.document_il.backend.latex_bbox.stamp_cache import (
    build_stamp_cache,
)

logger = logging.getLogger(__name__)

#: 段落 box 与页面边界的最小安全边距（pt）。
_PAGE_MARGIN = 1.0
#: 段落最小宽度/高度（pt）。
_MIN_BOX_WIDTH = 30.0
#: 水印文本标记（Typesetting.add_watermark 注入的段落文本）。
_WATERMARK_MARKERS = ("funstory.ai", "BabelDOC")
#: 逐段决策报告只记录这些「正文本体」标签；水印/页码/目录等不记录。
_BODY_LABELS = frozenset(
    {
        "text",
        "list",
        "figure_caption",
        "table_caption",
        "page_footnote",
        "table_footnote",
    }
)
#: 允许的资格模式：full（默认，正文本体段落默认重排）/ repair（复现旧门禁行为）。
_LATEX_BBOX_MODES = ("full", "repair")
#: box 相交判定的容差（pt）：相邻段落 box 相接不算重叠。
_BOX_TOUCH_TOLERANCE = 0.5
#: 下扩时与下方障碍保留的最小净空（pt）：bbox 最多扩到 ``h + space_below − 2``。
_EXPANSION_MARGIN = 2.0
#: 触发「先安全下扩」的失败原因（尺寸不够，不是内容错误）。
_EXPANSION_REASONS = (
    "vertical-overflow",
    "overfull-vbox",
    "text-clipped",
    "text-clipped-tail",
)
#: 段落主字体的默认 serif 取值（拿不到字体信息时按衬线处理）。
_DEFAULT_SERIF = True
#: 源页行合并容差（pt）：同一视觉行的多个 span 基线差。
_ROW_MERGE_TOLERANCE = 3.0


def _normalize_ws(text: str | None) -> str:
    """去掉全部空白，用于「译文 == 源文」判定。"""
    return re.sub(r"\s+", "", text or "")


def record_source_texts(docs, config) -> dict:
    """翻译之前记录每段源文（debug_id → unicode）。

    必须早于 ILTranslator：翻译会原地改写 ``paragraph.unicode``。overlay 用
    它与译文比对，判定「未翻译段」（LLM 原样返回源文）并跳过 LaTeX。
    """
    texts: dict[str, str] = {}
    for page in docs.page:
        for paragraph in page.pdf_paragraph:
            if paragraph.debug_id and (paragraph.unicode or "").strip():
                texts[paragraph.debug_id] = paragraph.unicode
    config.latex_source_texts = texts
    return texts


def capture_layout_sources(docs, config, source_texts: dict | None = None, link_state: dict | None = None) -> dict:
    """Typesetting 之前捕获 LaTeX overlay 所需的源几何与融合 body。

    必须在 ``Typesetting`` 之前调用：此时段落 box / 公式 box 仍是源坐标，
    composition 仍由 ``parse_translate_output`` 按译文顺序回填（融合的
    顺序契约成立）。结果写入 ``config.latex_bbox_state``。

    ``source_texts``（debug_id → 源文）用于未翻译段判定：workflow 路径从
    ``state.pkl`` 的 ``inputs`` 传入，high_level 路径用
    :func:`record_source_texts` 预先写入 ``config.latex_source_texts``。

    ``link_state``（``{"link_snapshot", "page_char_objects"}``，来自超链接
    快照）供融合的 ``cornermark`` 级按链接归属还原上标引文：字符对象身份
    在同一 state.pkl 内一致，这里用 ``id()`` 就地建页内反查表；无链接状态
    （high_level 路径）时角标仍还原抬升/字号，只是不挂标记链接/颜色。
    """
    if source_texts is None:
        source_texts = getattr(config, "latex_source_texts", None) or {}
    source_geometry = getattr(config, "latex_source_geometry", None) or {}
    primary_font_family = getattr(config, "primary_font_family", None)
    latex_index = FormulaLatexIndex.from_documents(docs, config)
    char_link_by_page: dict[int, dict[int, int]] = {}
    link_meta_by_page: dict[int, dict[int, dict]] = {}
    style_link_by_page: dict[int, dict[int, int]] = {}
    if link_state:
        snap = link_state.get("link_snapshot") or {}
        char_objects = link_state.get("page_char_objects") or {}
        for raw_page_index, entries in snap.items():
            page_index = int(raw_page_index)
            chars = char_objects.get(page_index) or []
            char_link: dict[int, int] = {}
            link_meta: dict[int, dict] = {}
            # 样式对象身份 → 链接（行内正文链接用）：仅当该样式覆盖的全部
            # 字符都唯一属于同一链接时收录，避免把 \href 扩大到无关文字。
            style_total: dict[int, int] = {}
            for char in chars:
                if char is not None and char.pdf_style is not None:
                    style_total[id(char.pdf_style)] = (
                        style_total.get(id(char.pdf_style), 0) + 1
                    )
            style_hits: dict[int, set[int]] = {}
            style_hit_count: dict[int, int] = {}
            for entry in entries:
                link_index = entry.get("link_index")
                if link_index is None:
                    continue
                link_meta[int(link_index)] = {
                    "color": entry.get("color"),
                    "font_size": entry.get("font_size"),
                    "raise_bp": entry.get("raise_bp"),
                }
                for idx in entry.get("char_indices") or []:
                    if 0 <= idx < len(chars):
                        char_link[id(chars[idx])] = int(link_index)
                        style = chars[idx].pdf_style
                        if style is not None:
                            style_hits.setdefault(id(style), set()).add(
                                int(link_index)
                            )
                            style_hit_count[id(style)] = (
                                style_hit_count.get(id(style), 0) + 1
                            )
            style_link = {
                style_id: next(iter(link_ids))
                for style_id, link_ids in style_hits.items()
                if len(link_ids) == 1
                and style_hit_count[style_id] >= style_total.get(style_id, 0)
            }
            char_link_by_page[page_index] = char_link
            link_meta_by_page[page_index] = link_meta
            if style_link:
                style_link_by_page[page_index] = style_link
    paragraphs: dict[str, dict] = {}
    bodies: dict[str, str] = {}
    plain_texts: dict[str, str] = {}
    failures: dict[str, list[str]] = {}
    fragments: dict[str, dict] = {}
    class_counts: Counter = Counter()
    note_counts: Counter = Counter()

    for page in docs.page:
        page_font_map = {font.font_id: font for font in page.pdf_font}
        for paragraph in page.pdf_paragraph:
            # xobj_id 约定：0 = 页面主内容流（绝大多数正文），>=1 = 嵌套
            # XObject（图形/表格内文本，坐标不在页面上，跳过），
            # -1 = Typesetting.add_watermark 注入的水印段落（跳过）。
            if paragraph.xobj_id is not None and paragraph.xobj_id != 0:
                continue
            if not paragraph.debug_id or paragraph.box is None:
                continue
            if paragraph.vertical:
                continue
            if paragraph.pdf_style is None or not paragraph.pdf_style.font_size:
                continue
            translated = any(
                composition is not None
                and composition.pdf_same_style_unicode_characters is not None
                and (composition.pdf_same_style_unicode_characters.unicode or "").strip()
                for composition in paragraph.pdf_paragraph_composition or []
            )
            if not translated:
                continue
            box = paragraph.box
            meta = {
                "page": page.page_number,
                "box": [
                    float(box.x),
                    float(box.y),
                    float(box.x2),
                    float(box.y2),
                ],
                "font_size": float(paragraph.pdf_style.font_size),
                "layout_label": paragraph.layout_label,
                "has_formula": any(
                    composition is not None and composition.pdf_formula is not None
                    for composition in paragraph.pdf_paragraph_composition or []
                ),
                # 源文（可能为空：旧产物/未记录时不做未翻译判定）。
                "source_text": source_texts.get(paragraph.debug_id, ""),
            }
            meta["fuse_kinds"] = sorted(
                {
                    kind
                    for kind, _unit, _style in iter_composition_units(paragraph)
                }
            )
            meta["serif"] = _paragraph_serif(
                paragraph, page_font_map, primary_font_family
            )
            geometry = source_geometry.get(paragraph.debug_id)
            if geometry:
                # 源行几何（P3-0）：缩进 / 行距 / 首行 ascent / 下方净空。
                meta["source_geometry"] = geometry
            paragraphs[paragraph.debug_id] = meta
            fused = fuse_paragraph(
                paragraph,
                page.page_number,
                page_font_map,
                latex_index,
                char_link=char_link_by_page.get(page.page_number),
                link_meta=link_meta_by_page.get(page.page_number),
                style_link=style_link_by_page.get(page.page_number),
            )
            if fused.ok:
                bodies[paragraph.debug_id] = fused.body
                plain_texts[paragraph.debug_id] = fused.plain_text
                meta["fuse_classes"] = list(fused.formula_classes)
                if fused.fragments:
                    meta["fragment_keys"] = [frag.key for frag in fused.fragments]
                for frag in fused.fragments:
                    fragments[frag.key] = {
                        "page": frag.page,
                        "box": list(frag.box),
                        "y_offset": frag.y_offset,
                        "height": frag.height,
                    }
                class_counts.update(fused.formula_classes)
                note_counts.update(fused.formula_notes)
            else:
                failures[paragraph.debug_id] = fused.reasons

    state = {
        "paragraphs": paragraphs,
        "bodies": bodies,
        "plain_texts": plain_texts,
        "fusion_failures": failures,
        #: 需要从源 PDF 裁区域嵌入的片段（key → page/box/y_offset/height）。
        "fragments": fragments,
        #: 四级分类统计（text/mineru/simple_math/fragment）与降级留痕。
        "fusion_stats": {
            "classes": dict(class_counts),
            "notes": dict(note_counts),
        },
        "provider_inline_spans": latex_index.total_spans if latex_index else 0,
    }
    config.latex_bbox_state = state
    return state


def _paragraph_serif(paragraph, page_font_map: dict, primary_font_family) -> bool:
    """段落主字体是否衬线（决定拉丁/中文用 Serif 还是 Sans 字体）。

    ``primary_font_family`` 与产品的 FontMapper 同口径：serif → 衬线；
    sans-serif / script → 非衬线；None → 看源段落主字体自身的 serif 标志。
    """
    family = (primary_font_family or "").strip().lower()
    if family == "serif":
        return True
    if family in ("sans-serif", "script"):
        return False
    font_id = getattr(getattr(paragraph, "pdf_style", None), "font_id", None)
    font = page_font_map.get(font_id) if font_id else None
    serif = getattr(font, "serif", None)
    return _DEFAULT_SERIF if serif is None else bool(serif)


def measure_source_rows(page: pymupdf.Page, clip: pymupdf.Rect) -> list[dict]:
    """量测 ``clip`` 区域内的源文本行（按 y 合并同一视觉行的多 span）。

    prepare 在内容流生成之前调用，此时页面仍是**源文**：这里得到的行数/行距/
    首行缩进就是源排版事实，比 IL 字符聚类稳定（字符聚类会把同一行按字高拆开，
    导致 n_lines 偏大、pitch 偏小）。

    返回按纵坐标升序的行：``{"y0"(顶), "y1"(底), "x0", "x1"}``。
    """
    rows: list[dict] = []
    for block in page.get_text("dict", clip=clip)["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = [span for span in line["spans"] if span["text"].strip()]
            if not spans:
                continue
            rows.append(
                {
                    "center": sum(
                        (span["bbox"][1] + span["bbox"][3]) / 2 for span in spans
                    )
                    / len(spans),
                    "y0": min(span["bbox"][1] for span in spans),
                    "y1": max(span["bbox"][3] for span in spans),
                    "x0": min(span["bbox"][0] for span in spans),
                    "x1": max(span["bbox"][2] for span in spans),
                }
            )
    rows.sort(key=lambda row: row["center"])
    merged: list[dict] = []
    for row in rows:
        if merged and _same_visual_line(merged[-1], row):
            current = merged[-1]
            current["y0"] = min(current["y0"], row["y0"])
            current["y1"] = max(current["y1"], row["y1"])
            current["x0"] = min(current["x0"], row["x0"])
            current["x1"] = max(current["x1"], row["x1"])
        else:
            merged.append(dict(row))
    return merged


def _same_visual_line(a: dict, b: dict) -> bool:
    """两个 pymupdf line 是否属于同一视觉行。

    行内公式（上下标）会被 pymupdf 拆成独立 line：字高小、center 偏上/下，
    但 y 区间与正文行大量重叠。用「y 区间重叠占较矮行高≥ 50%」判定比
    center 距离更稳：center 容差在字高差大时会误拆（rows[0] 错取公式行、
    first_line_dx 偏差可达数百 bp）；而纯重叠 >1bp 在行距紧、span 含
    上下标拉伸时会误合相邻行。50% 阈值两头都避开（回归：8tHmq 段
    𝐺𝑡 行重叠 83% 合并、下一行重叠 48% 不合并）。
    """
    shorter = min(a["y1"] - a["y0"], b["y1"] - b["y0"])
    if shorter <= 0:
        return abs(a["center"] - b["center"]) <= _ROW_MERGE_TOLERANCE
    overlap = min(a["y1"], b["y1"]) - max(a["y0"], b["y0"])
    if overlap / shorter >= 0.5:
        return True
    return abs(a["center"] - b["center"]) <= _ROW_MERGE_TOLERANCE


def _rows_geometry(rows: list[dict], rect: pymupdf.Rect) -> dict | None:
    """源行 → {n_lines, baseline_pitch, first_line_dx, ascent_top}（页面坐标）。"""
    if not rows:
        return None
    pitches = [
        rows[index + 1]["center"] - rows[index]["center"]
        for index in range(len(rows) - 1)
    ]
    pitches = [value for value in pitches if value > 0.5]
    first = rows[0]
    return {
        "n_lines": len(rows),
        "baseline_pitch": (
            round(median(pitches), 3) if pitches else None
        ),
        "first_line_dx": round(first["x0"] - rect.x0, 3),
        "ascent_top": round(max(0.0, first["y0"] - rect.y0), 3),
        "source": "source-page-rows",
    }


def _compile_stats(renderer, stamps: dict) -> dict:
    """编译统计（P4 起含批编译轮/段明细；单段渲染器没有这些字段时为 0）。"""
    stats = {
        "attempts": sum(result.compile_attempts for result in stamps.values()),
        "seconds": round(renderer.compile_seconds, 3),
        "cache_hits": renderer.cache_hits,
        "batches": getattr(renderer, "batch_count", 0),
        "segments": getattr(renderer, "segment_count", 0),
    }
    rounds = getattr(renderer, "round_log", None)
    if rounds:
        stats["rounds"] = rounds
        stats["fallback_segments"] = getattr(renderer, "fallback_segments", 0)
        stats["fallback_seconds"] = round(
            getattr(renderer, "fallback_seconds", 0.0), 3
        )
    return stats


def _source_pdf_path(config) -> Path | None:
    """定位源 PDF（裁片段用）：显式配置 > input_file > working_dir/input.pdf。"""
    explicit = getattr(config, "latex_source_pdf_path", None)
    if explicit:
        candidate = Path(explicit)
        if candidate.is_file():
            return candidate
    input_file = getattr(config, "input_file", None)
    if input_file:
        candidate = Path(input_file)
        if candidate.is_file():
            return candidate
    getter = getattr(config, "get_working_file_path", None)
    if callable(getter):
        try:
            candidate = Path(getter("input.pdf"))
        except Exception:  # noqa: BLE001 - 路径解析失败只是拿不到源 PDF
            return None
        if candidate.is_file():
            return candidate
    return None


def crop_fragment_pdf(
    source: pymupdf.Document, page_index: int, rect: pymupdf.Rect, out_path: Path
) -> bool:
    """从源 PDF 裁出 ``rect`` 区域存成单页 PDF（片段内联用）。

    源页面尺寸未知（越界）时返回 False，调用方按「片段不可用」回退整段。
    """
    if page_index < 0 or page_index >= len(source):
        return False
    page_rect = source[page_index].rect
    clipped = pymupdf.Rect(rect) & page_rect
    if clipped.is_empty or clipped.width <= 0.5 or clipped.height <= 0.5:
        return False
    out = pymupdf.open()
    try:
        target = out.new_page(width=clipped.width, height=clipped.height)
        target.show_pdf_page(target.rect, source, page_index, clip=clipped)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out.save(out_path)
    finally:
        out.close()
    return out_path.is_file()


def _shared_fragment_dir() -> str | None:
    """共享片段目录：贴片缓存根下的 ``fragments/``；未配置共享根时 None。

    与 stamp 缓存同根，一起被 ``--cleanup`` 覆盖；片段路径进 LaTeX body，所以
    必须跨进程稳定（见 :meth:`LatexBboxOverlay._fragment_path`）。
    """
    from babeldoc.format.pdf.document_il.backend.latex_bbox.stamp_cache import (
        SHARED_CACHE_ENV,
    )

    root = os.environ.get(SHARED_CACHE_ENV)
    if not root or not str(root).strip():
        return None
    return str(Path(str(root).strip()) / "fragments")


def _rect_key(rect) -> tuple:
    return (
        round(float(rect.x0), 2),
        round(float(rect.y0), 2),
        round(float(rect.x1), 2),
        round(float(rect.y1), 2),
    )


def _link_target(link: dict) -> tuple:
    """链接的**语义目标**（与 pymupdf 落盘时可能改写的 kind 无关）。

    ``insert_link`` 无法保留原始 kind：``LINK_LAUNCH``(3) 会落盘为
    ``LINK_GOTOR``(5)，``LINK_NAMED`` 带 page 时 page 会丢失。门禁关心的是
    「没丢链接 / 目标不变」，因此按目标类型归一化。

    ``file`` 目标额外做 URL 解码归一化：pymupdf 对 GOTOR 的 file 会在每次
    落盘时做一层百分号编码（``:`` → ``%3A``），重插后会出现多次编码，
    但目标语义未变。
    """
    if link.get("uri"):
        return ("uri", str(link["uri"]))
    if link.get("file"):
        return ("file", _canonical_file_target(str(link["file"])))
    if link.get("nameddest"):
        return ("named", str(link["nameddest"]))
    return ("page", link.get("page"))


def _canonical_file_target(value: str) -> str:
    """把 pymupdf 对 file 目标的多次百分号编码归一化为不带编码的形态。"""
    from urllib.parse import unquote

    seen = value
    for _ in range(4):  # 最多解 4 层，防止恶意嵌套
        decoded = unquote(seen)
        if decoded == seen:
            break
        seen = decoded
    return seen


def _link_signature(link: dict) -> tuple:
    """链接身份键：矩形 + 语义目标（忽略 pymupdf 会改写的 kind/编码）。"""
    return (_rect_key(link.get("from") or pymupdf.Rect()), _link_target(link))


def _links_by_signature(links: list[dict]) -> dict[tuple, int]:
    counts: dict[tuple, int] = {}
    for link in links:
        counts[_link_signature(link)] = counts.get(_link_signature(link), 0) + 1
    return counts


def _box_expanded_beyond(current_box, source_box) -> bool:
    """当前段落 box 是否超出捕获的源 box（超出 >2pt 即视为被 Typesetting 扩过）。"""
    if current_box is None:
        return False
    tolerance = 2.0
    sx0, sy0, sx1, sy1 = source_box
    return (
        (current_box.x or 0) < sx0 - tolerance
        or (current_box.y or 0) < sy0 - tolerance
        or (current_box.x2 or 0) > sx1 + tolerance
        or (current_box.y2 or 0) > sy1 + tolerance
    )


def _page_rect(box, page_rect) -> pymupdf.Rect:
    """IL box（y 向上）→ pymupdf 页面矩形（y 向下）。"""
    x0, y0, x1, y1 = (float(value) for value in box)
    return pymupdf.Rect(x0, page_rect.height - y1, x1, page_rect.height - y0)


def _boxes_overlap(first, second, tolerance: float = _BOX_TOUCH_TOLERANCE) -> bool:
    """两个 IL box 是否实质重叠（相接不算）。"""
    if first is None or second is None:
        return False
    for value in (first.x, first.y, first.x2, first.y2, second.x, second.y,
                  second.x2, second.y2):
        if value is None:
            return False
    return (
        first.x < second.x2 - tolerance
        and second.x < first.x2 - tolerance
        and first.y < second.y2 - tolerance
        and second.y < first.y2 - tolerance
    )


def measure_line_fill(page: pymupdf.Page, clip: pymupdf.Rect) -> dict:
    """量测 clip 区域内当前渲染的非末行填充率（检测异常短行）。"""
    box_width = clip.width
    if box_width <= 0:
        return {"n_lines": 0, "min_body_fill": None, "watermark": False}
    rows: dict[float, list] = {}
    watermark = False
    text_all = ""
    for block in page.get_text("dict", clip=clip)["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = [s for s in line["spans"] if s["text"].strip()]
            if not spans:
                continue
            key = round(line["bbox"][1], 0)
            rows.setdefault(key, []).append(
                (
                    min(s["bbox"][0] for s in spans),
                    max(s["bbox"][2] for s in spans),
                )
            )
            text_all += "".join(s["text"] for s in spans)
    for marker in _WATERMARK_MARKERS:
        if marker in text_all:
            watermark = True
            break
    fills = []
    for key in sorted(rows):
        x0 = min(r[0] for r in rows[key])
        x1 = max(r[1] for r in rows[key])
        fills.append((x1 - x0) / box_width)
    body_fills = fills[:-1] if len(fills) > 1 else []
    return {
        "n_lines": len(fills),
        "min_body_fill": min(body_fills) if body_fills else None,
        "watermark": watermark,
    }


class LatexBboxOverlay:
    """对已生成内容流的 mono PDF 做选择性 LaTeX 贴片。"""

    def __init__(self, pdf: pymupdf.Document, docs, config):
        self.pdf = pdf
        self.docs = docs
        self.config = config
        mode = str(getattr(config, "latex_bbox_mode", "full") or "full").strip().lower()
        self.mode = mode if mode in _LATEX_BBOX_MODES else "full"
        self.stats: dict = {
            "enabled": True,
            "mode": self.mode,
            "available": False,
            "attempted": 0,
            "applied": 0,
            "fallback": 0,
            "failed": 0,
            "fallback_reasons": {},
            "applied_paragraphs": [],
            "pages_affected": [],
            "compile": {"attempts": 0, "seconds": 0.0, "cache_hits": 0},
            "links": {
                "before_total": 0,
                "after_total": 0,
                "restored": 0,
                "uri_set_match": None,
            },
            "reverted": False,
            "error": None,
            #: 逐段决策留痕（正文类候选段；由 ``export_decisions`` 填充）。
            "decisions": [],
        }
        #: debug_id → 决策记录（字段未到该阶段时为 None）。
        self._decisions: dict[str, dict] = {}
        #: capture 状态快照。
        self._paragraphs: dict[str, dict] = {}
        self._bodies: dict[str, str] = {}
        self._plain_texts: dict[str, str] = {}
        #: 片段引用（key → page/box/y_offset/height，capture 阶段写出）。
        self._fragments: dict[str, dict] = {}
        #: capture 阶段的四级分类统计（写入报告）。
        self._fusion_stats: dict = {}
        #: prepare 的结果：成功编译的段落（内容流需跳过其字符）。
        self._jobs: list[dict] = []
        self._compiled: dict[str, StampResult] = {}
        self._reasons: Counter = Counter()
        self._tmpdir: tempfile.TemporaryDirectory | None = None
        self._prepared = False
        #: 内容流生成时应跳过字符的 debug_id 集合（prepare 的返回值）。
        self.stamped_ids: set[str] = set()
        #: 页面查表缓存。
        self._pages_by_number: dict[int, object] | None = None
        self._paragraphs_by_page: dict[int, dict[str, object]] | None = None
        #: P6 编译后精修框（debug_id → IL box）：来自
        #: ``config.latex_bbox_box_overrides``，由 layout_refine 按译文版面算出。
        #: 只替换贴片矩形；源行几何仍按原框量测。
        self._box_overrides: dict[str, tuple] = {
            key: tuple(float(value) for value in box)
            for key, box in (
                getattr(config, "latex_bbox_box_overrides", None) or {}
            ).items()
        }
        self._watermark_rects_cache: dict[int, list[pymupdf.Rect]] = {}
        #: 印章内标记链接（``bdoclink://l<N>``）映射回的页面矩形：
        #: {page_index: {link_index: [页面坐标 Rect, ...]}}。
        #: ``_stamp_pages`` 贴片时填充；PDFCreater 在链接重映射前取走，
        #: 作为该链接新矩形的最高优先级（比字符并集精确——那是印章的真实墨迹）。
        self.stamp_link_rects: dict[int, dict[int, list[pymupdf.Rect]]] = {}
        #: 诊断 recorder（``config.debug_recorder`` → 进程内当前值兜底）；
        #: 关闭时全部采集调用为 no-op。
        self._recorder = getattr(config, "debug_recorder", None)
        if self._recorder is None:
            try:
                from babeldoc.debug_recorder import get_current

                self._recorder = get_current()
            except Exception:  # noqa: BLE001 - recorder 不可用只影响采集
                self._recorder = None

    # ------------------------------------------------------------------
    def _init_decisions(self, paragraphs: dict) -> None:
        """为正文类候选段建立决策骨架（各字段先留 None）。"""
        self._decisions = {}
        for debug_id, meta in paragraphs.items():
            label = meta.get("layout_label")
            if label not in _BODY_LABELS:
                continue
            self._decisions[debug_id] = {
                "debug_id": debug_id,
                "page": meta.get("page"),
                "label": label,
                "reason": None,
                "fuse_kinds": list(meta.get("fuse_kinds") or []),
                #: 该段公式单元的四级分类（text/mineru/simple_math/fragment）。
                "fuse_classes": list(meta.get("fuse_classes") or []),
                #: 期望可见文本（译文去标记 + text/mineru/simple_math 片段原文，
                #: fragment 除外）：验收脚本用它做「贴片文本层零差异」判定。
                "expected_text": self._plain_texts.get(debug_id, ""),
                "fill_before": None,
                "fill_after": None,
                "font_scale": None,
                "lead": None,
                "attempts": None,
                #: 安全下扩的额外高度（P3-5，0 表示未扩）。
                "expanded_pt": 0.0,
                #: 源行数（P3-0 几何；无几何时为 None）。
                "n_lines_source": (meta.get("source_geometry") or {}).get("n_lines"),
                #: 首行缩进（P3-0 几何，正=缩进、负=悬挂）。
                "indent_pt": (meta.get("source_geometry") or {}).get("first_line_dx"),
                #: 最终真实贴片矩形（页面坐标 [x0,y0,x1,y1]；扩框后与源框不同）。
                "stamp_box": None,
            }

    def _decide(self, debug_id: str, reason: str | None = None, **fields) -> None:
        """更新一个决策记录（``reason=None`` 表示只填字段，不改原因）。"""
        record = self._decisions.get(debug_id)
        if record is None:
            return
        if reason is not None:
            record["reason"] = reason
        for key, value in fields.items():
            record[key] = value

    def _event(self, kind: str, data: dict) -> None:
        """发布 build 阶段事件；recorder 关闭时为 no-op。"""
        recorder = self._recorder
        if not recorder:
            return
        recorder.record_event("build", kind, data)

    def export_decisions(self) -> list[dict]:
        """按 (page, debug_id) 排序导出逐段决策记录。"""
        return sorted(
            self._decisions.values(),
            key=lambda item: (
                item["page"] if item.get("page") is not None else -1,
                item["debug_id"],
            ),
        )

    # ------------------------------------------------------------------
    def _load_state(self) -> None:
        state = getattr(self.config, "latex_bbox_state", None) or {}
        self._paragraphs = state.get("paragraphs") or {}
        self._bodies = state.get("bodies") or {}
        self._plain_texts = state.get("plain_texts") or {}
        self._fragments = state.get("fragments") or {}
        self._fusion_stats = state.get("fusion_stats") or {}

    def _docs_pages(self) -> dict[int, object]:
        if self._pages_by_number is None:
            self._pages_by_number = {
                page.page_number: page for page in self.docs.page
            }
        return self._pages_by_number

    def _page_paragraph_index(self) -> dict[int, dict[str, object]]:
        if self._paragraphs_by_page is None:
            index: dict[int, dict[str, object]] = {}
            for number, page in self._docs_pages().items():
                index[number] = {
                    paragraph.debug_id: paragraph
                    for paragraph in page.pdf_paragraph
                    if paragraph.debug_id
                }
            self._paragraphs_by_page = index
        return self._paragraphs_by_page

    def _find_paragraph(self, page_index: int, debug_id: str):
        return self._page_paragraph_index().get(page_index, {}).get(debug_id)

    def _watermark_rects(self, page_index: int) -> list[pymupdf.Rect]:
        """水印段落的字符矩形（页面坐标），用于 full 模式的「压水印」判定。

        full 模式在内容流生成**之前**选段，此时页面还是源文、没有水印文本，
        无法用文本标记判定；改用 Typesetting 已排好的水印字符 box（IL 坐标，
        含真实换行位置）。
        """
        cached = self._watermark_rects_cache.get(page_index)
        if cached is not None:
            return cached
        rects: list[pymupdf.Rect] = []
        page = self._docs_pages().get(page_index)
        if page is not None and page_index < len(self.pdf):
            page_height = self.pdf[page_index].rect.height
            for paragraph in page.pdf_paragraph:
                if paragraph.xobj_id != -1:
                    continue
                for composition in paragraph.pdf_paragraph_composition or []:
                    chars = []
                    if composition.pdf_character is not None:
                        chars.append(composition.pdf_character)
                    elif composition.pdf_formula is not None:
                        chars.extend(composition.pdf_formula.pdf_character)
                    for char in chars:
                        box = getattr(char, "box", None)
                        if box is None or None in (box.x, box.y, box.x2, box.y2):
                            continue
                        rects.append(
                            pymupdf.Rect(
                                box.x,
                                page_height - box.y2,
                                box.x2,
                                page_height - box.y,
                            )
                        )
        self._watermark_rects_cache[page_index] = rects
        return rects

    def _expanded_box_overlaps_others(self, debug_id: str, page_index: int, box) -> bool:
        """扩后 box 内是否落有其他正文段落的 box。

        full 模式不再因「box 被 Typesetting 扩过」直接跳过（已贴片段落的字符
        本来就不进内容流，不会出现双层文本），改为安全门禁：只有扩后区域落有
        别的段落（贴片/兜底 redaction 可能伤到它们）才跳过。
        """
        for other_id, other in self._page_paragraph_index().get(page_index, {}).items():
            if other_id == debug_id:
                continue
            if getattr(other, "xobj_id", None) not in (0, None):
                # 嵌套 XObject 的坐标不在页面上；水印另做字符级判定。
                continue
            if _boxes_overlap(box, getattr(other, "box", None)):
                return True
        return False

    # ------------------------------------------------------------------
    def prepare(self) -> set[str]:
        """选段 + 编译贴片（full 模式必须在内容流生成**之前**调用）。

        返回成功编译、内容流生成时应跳过其字符的 debug_id 集合。
        repair 模式不做预选（保留旧行为，选段仍在 :meth:`stamp` 内发生）。
        任何异常都只记录并返回空集合（= 不跳过任何字符，保留现有渲染）。
        """
        self._load_state()
        self._init_decisions(self._paragraphs)
        self.stats["fusion"] = copy.deepcopy(self._fusion_stats)
        if self.mode == "repair":
            self._prepared = True
            return set()
        if not self._paragraphs:
            self.stats["fallback"] = 0
            self.stats["fallback_reasons"] = {"no-captured-paragraphs": 0}
            self._prepared = True
            return set()
        capability = probe_latex_capability(
            xelatex_path=getattr(self.config, "latex_xelatex_path", None),
            font_path=getattr(self.config, "latex_cjk_font_path", None),
            primary_font_family=getattr(self.config, "primary_font_family", None),
        )
        self.stats["available"] = capability.available
        self.stats["capability"] = capability.to_dict()
        self._event("latex_capability", capability.to_dict())
        if not capability.available:
            self.stats["fallback"] = len(self._paragraphs)
            self.stats["fallback_reasons"] = {
                "capability-unavailable": len(self._paragraphs)
            }
            for debug_id in self._decisions:
                self._decide(debug_id, "capability-unavailable")
            self._prepared = True
            return set()
        try:
            self._prepare_jobs(capability)
        except Exception as exc:  # noqa: BLE001 - 预选失败只是不跳过字符
            logger.warning("LaTeX bbox 选段/编译失败，保留现有渲染", exc_info=True)
            self.stats["error"] = f"{type(exc).__name__}: {exc}"
            self.stats["fallback"] = len(self._paragraphs)
            for record in self._decisions.values():
                if record["reason"] is None:
                    record["reason"] = "overlay-error"
            self._abort_prepared()
            return set()
        self._prepared = True
        self._event(
            "latex_prepare",
            {
                "mode": self.mode,
                "attempted": int(self.stats.get("attempted") or 0),
                "compiled": len(self._compiled),
                "stamped_ids": sorted(self.stamped_ids),
                "selection_reasons": dict(self._reasons),
                "compile": self.stats.get("compile") or {},
            },
        )
        return set(self.stamped_ids)

    def _build_renderer(self, capability):
        """P4：整文档轮次制批编译（坏段内部回退单段渲染 + 落盘 stamp 缓存）。

        返回的渲染器与 :class:`BboxStampRenderer` 同构（``render_many`` /
        ``cache_hits`` / ``compile_seconds``），调用方无需区分。
        """
        timeout = getattr(self.config, "latex_compile_timeout_seconds", 45.0)
        workers = getattr(self.config, "latex_max_compile_workers", 2)
        cache = build_stamp_cache(self.config, capability)
        return BatchStampRenderer(
            capability,
            timeout_seconds=timeout,
            max_workers=workers,
            cache=cache,
            debug_recorder=self._recorder,
        )

    def _prepare_jobs(self, capability) -> None:
        jobs, reasons = self._select_candidates(self._paragraphs, self._bodies)
        self._reasons = Counter(reasons)
        selected_ids = {job["debug_id"] for job in jobs}
        self._event(
            "latex_candidates",
            {
                "selected": [
                    {
                        "id": job["debug_id"],
                        "page": job["page"],
                        "width": round(float(job["width"]), 3),
                        "height": round(float(job["height"]), 3),
                        "font_size": round(float(job["font_size"]), 3),
                    }
                    for job in jobs
                ],
                "rejected": {
                    debug_id: record["reason"]
                    for debug_id, record in self._decisions.items()
                    if record["reason"] and debug_id not in selected_ids
                },
                "reason_counts": dict(self._reasons),
            },
        )
        self._tmpdir = tempfile.TemporaryDirectory(prefix="babeldoc-latex-bbox-")
        jobs = self._substitute_fragments(jobs, Path(self._tmpdir.name))
        self.stats["attempted"] = len(jobs)
        if not jobs:
            self.stats["fallback"] = len(self._paragraphs)
            self.stats["fallback_reasons"] = dict(self._reasons)
            return
        renderer = self._build_renderer(capability)
        requests = [
            (
                self._stamp_request(job),
                Path(self._tmpdir.name),
            )
            for job in jobs
        ]
        stamps = renderer.render_many(requests)
        # P3-5：垂直放不下的段落先安全下扩（不缩字）；扩后区域必须无他内容。
        self._expand_vertical_failures(jobs, stamps, renderer, Path(self._tmpdir.name))
        self.stats["compile"] = _compile_stats(renderer, stamps)
        for job in jobs:
            stamp = stamps.get(job["debug_id"])
            if stamp is None:
                continue
            if stamp.ok and stamp.pdf_path:
                effective_size = stamp.font_size or job["font_size"]
                self._decide(
                    job["debug_id"],
                    font_scale=(
                        round(stamp.scale, 4) if stamp.scale is not None else None
                    ),
                    lead=(
                        round(stamp.lead, 3)
                        if stamp.lead is not None
                        else round(effective_size * DEFAULT_LEAD_RATIO, 3)
                    ),
                    attempts=stamp.compile_attempts,
                    expanded_pt=round(job.get("expanded_pt", 0.0), 3),
                )
            else:
                self._decide(
                    job["debug_id"],
                    f"compile:{stamp.reason or 'unknown'}",
                    attempts=stamp.compile_attempts,
                )
        successful = {k: v for k, v in stamps.items() if v.ok and v.pdf_path}
        failed = {
            k: v.reason for k, v in stamps.items() if not (v.ok and v.pdf_path)
        }
        self._reasons.update({f"compile:{reason}": 1 for reason in failed.values()})
        if not successful:
            self.stats["failed"] = len(stamps)
            self.stats["fallback"] = len(self._paragraphs)
            self.stats["fallback_reasons"] = dict(self._reasons)
            self._abort_prepared()
            return
        self._jobs = jobs
        self._compiled = successful
        self.stamped_ids = set(successful)

    def _stamp_request(self, job: dict) -> StampRequest:
        """由 job（含源行几何）构造编译请求。

        几何优先级：源页面文本行（
        :func:`measure_source_rows`）> capture 阶段的 IL 字符聚类。
        """
        meta = self._paragraphs.get(job["debug_id"]) or {}
        geometry = job.get("row_geometry") or {}
        if not geometry:
            geometry = meta.get("source_geometry") or {}
        font_size = job["font_size"]
        lead = derive_lead(font_size, geometry.get("baseline_pitch"))
        # 段落级中文字体族（serve 端写入 meta）；非字符串视为未指定。
        font_family = meta.get("font_family")
        if not isinstance(font_family, str):
            font_family = None
        return StampRequest(
            key=job["debug_id"],
            body=job["body"],
            width=job["width"],
            height=job["height"],
            font_size=font_size,
            expected_text=self._plain_texts.get(job["debug_id"], ""),
            lead=lead,
            first_line_dx=float(geometry.get("first_line_dx") or 0.0),
            ascent_top=(
                float(geometry["ascent_top"])
                if geometry.get("ascent_top") is not None
                else None
            ),
            serif=bool(meta.get("serif", _DEFAULT_SERIF)),
            font_family=font_family,
        )

    def _expand_vertical_failures(
        self,
        jobs: list[dict],
        stamps: dict,
        renderer,
        tmpdir: Path,
    ) -> None:
        """垂直放不下的段落：先安全下扩重试一轮（P3-5）。

        只扩到 ``h + space_below − 2bp``；新增区域必须无文本/图形/图片（源页
        内容），否则不扩（交给字号缩小）。扩后同步更新 job 的 rect/height。
        """
        if not jobs:
            return
        retry: list[tuple[StampRequest, Path]] = []
        pending: dict[str, dict] = {}
        for job in jobs:
            stamp = stamps.get(job["debug_id"])
            if stamp is None or (stamp.ok and stamp.pdf_path):
                continue
            if not any(reason in (stamp.reason or "") for reason in _EXPANSION_REASONS):
                continue
            meta = self._paragraphs.get(job["debug_id"]) or {}
            geometry = meta.get("source_geometry") or {}
            space_below = geometry.get("space_below_pt")
            if not space_below:
                continue
            added = float(space_below) - _EXPANSION_MARGIN
            if added <= 1.0:
                continue
            if not self._strip_is_clear(job["page"], job["rect"], added):
                continue
            request = self._stamp_request(job)
            request.height = job["height"] + added
            # 诊断：扩框重试候选的父链指向上一次失败的真实候选。
            request.debug_parent = (
                (stamp.debug_ref or {}).get("candidate_id")
                if getattr(stamp, "debug_ref", None)
                else None
            )
            retry.append((request, tmpdir))
            pending[job["debug_id"]] = {"job": job, "request": request, "added": added}
        if not retry:
            return
        expanded = renderer.render_many(retry)
        for debug_id, info in pending.items():
            stamp = expanded.get(debug_id)
            if stamp is None:
                continue
            job = info["job"]
            job["attempts"] = int(job.get("attempts", 0)) + stamp.compile_attempts
            if stamp.ok and stamp.pdf_path:
                added = float(info["added"])
                job["height"] = float(info["request"].height)
                job["rect"] = pymupdf.Rect(
                    job["rect"].x0,
                    job["rect"].y0,
                    job["rect"].x1,
                    job["rect"].y1 + added,
                )
                job["expanded_pt"] = added
                stamps[debug_id] = stamp
            self._event(
                "compile_expand",
                {
                    "key": debug_id,
                    "added_pt": round(float(info["added"]), 3),
                    "height_after": round(float(info["request"].height), 3),
                    "parent_id": info["request"].debug_parent,
                    "candidate_id": (
                        (stamp.debug_ref or {}).get("candidate_id")
                        if getattr(stamp, "debug_ref", None)
                        else None
                    ),
                    "ok": bool(stamp.ok and stamp.pdf_path),
                    "reason": stamp.reason,
                },
            )

    def _strip_is_clear(
        self, page_index: int, rect: pymupdf.Rect, added: float
    ) -> bool:
        """下扩新增区域（原 box 底边之下 ``added`` pt）是否无他内容。"""
        if added <= 0 or page_index >= len(self.pdf):
            return False
        page = self.pdf[page_index]
        # 页面坐标 y 向下：box 下方 = rect.y1 再往下 added。
        strip = pymupdf.Rect(rect.x0, rect.y1, rect.x1, rect.y1 + added - 0.5)
        if strip.is_empty or strip.height <= 0.5:
            return False
        if strip.y1 > page.rect.y1:
            return False
        if page.get_text("words", clip=strip):
            return False
        try:
            for drawing in page.get_drawings():
                if pymupdf.Rect(drawing["rect"]).intersects(strip):
                    return False
        except Exception:  # noqa: BLE001 - 图形探测失败按不安全处理
            logger.debug("下扩净空判定：get_drawings 失败", exc_info=True)
            return False
        try:
            for info in page.get_image_info():
                if pymupdf.Rect(info["bbox"]).intersects(strip):
                    return False
        except Exception:  # noqa: BLE001 - 图片探测失败按不安全处理
            logger.debug("下扩净空判定：get_image_info 失败", exc_info=True)
            return False
        # 水印区域（prepare 时页面上尚无水印文本，需用 IL 水印段字符盒判定）。
        for mark in self._watermark_rects(page_index):
            if mark.intersects(strip):
                return False
        return True

    def _substitute_fragments(self, jobs: list[dict], tmpdir: Path) -> list[dict]:
        """把 body 里的片段占位符替换成源 PDF 裁出来的内联图片。

        片段来自**源 PDF**（不是译文/贴片 PDF），按源 box 外扩 0.5bp 裁单页；
        任一片段裁剪失败即整段回退现有渲染（不会留下空图片引用）。
        """
        if not any(FRAGMENT_PATTERN.search(job["body"]) for job in jobs):
            return jobs
        source_path = _source_pdf_path(self.config)
        kept: list[dict] = []
        if source_path is None:
            for job in jobs:
                if FRAGMENT_PATTERN.search(job["body"]):
                    self._reject_job(job, "fragment-source-missing")
                    continue
                kept.append(job)
            return kept
        try:
            source = pymupdf.open(source_path)
        except Exception:  # noqa: BLE001 - 打不开源 PDF 只影响片段
            logger.warning("打开源 PDF 失败，含片段的段落回退", exc_info=True)
            for job in jobs:
                if FRAGMENT_PATTERN.search(job["body"]):
                    self._reject_job(job, "fragment-source-missing")
                    continue
                kept.append(job)
            return kept
        try:
            rendered: dict[str, str] = {}
            for job in jobs:
                keys = FRAGMENT_PATTERN.findall(job["body"])
                if not keys:
                    kept.append(job)
                    continue
                body = job["body"]
                failed = False
                for key in keys:
                    if key not in rendered:
                        path = self._crop_fragment(source, tmpdir, key)
                        if path is None:
                            failed = True
                            break
                        rendered[key] = path
                    body = FRAGMENT_PATTERN.sub(
                        lambda match, _key=key: rendered[_key]
                        if match.group(1) == _key
                        else match.group(0),
                        body,
                    )
                if failed:
                    self._reject_job(job, "fragment-crop-failed")
                    continue
                job["body"] = body
                kept.append(job)
            return kept
        finally:
            source.close()

    def _crop_fragment(self, source, tmpdir: Path, key: str) -> str | None:
        """裁一个片段并返回可用于 ``\\includegraphics`` 的绝对路径。

        落点目录优先用 ``config.latex_fragment_dir``（内容寻址的稳定目录）：片段
        的绝对路径会进 body，而 body 是 :attr:`StampRequest.cache_key` 的第一项，
        所以路径必须对「同一片段」跨进程稳定——否则每次编译都是新 key，持久
        stamp 缓存对**所有含行内公式的段落**永久失效（实测该类段落占比约一半）。
        文件名取几何 + 源 PDF 身份的摘要：内容相同就是同一个文件，可安全复用，
        已存在时连裁剪都省掉。没有配置该目录时退回 ``tmpdir``（旧行为）。
        """
        ref = self._fragments.get(key)
        if not ref:
            return None
        box = ref.get("box") or []
        if len(box) != 4:
            return None
        page_index = int(ref.get("page", -1))
        height = float(ref.get("height") or 0.0)
        if height <= 0:
            return None
        page_height = self._page_height(page_index)
        if page_height <= 0:
            return None
        # IL 坐标（y 向上）→ PDF 矩形（y 向下），外扩 0.5bp 保证字形边缘完整。
        expand = 0.5
        rect = pymupdf.Rect(
            box[0] - expand,
            page_height - box[3] - expand,
            box[2] + expand,
            page_height - box[1] + expand,
        )
        fragment = FragmentRef(
            key=key,
            page=page_index,
            box=(box[0], box[1], box[2], box[3]),
            y_offset=float(ref.get("y_offset") or 0.0),
            height=height,
        )
        out_path = self._fragment_path(tmpdir, key, page_index, rect)
        try:
            if not out_path.is_file() and not crop_fragment_pdf(
                source, page_index, rect, out_path
            ):
                return None
        except Exception:  # noqa: BLE001 - 裁剪失败按片段不可用处理
            logger.debug("片段裁剪失败 key=%s", key, exc_info=True)
            return None
        return render_fragment_latex(fragment, str(out_path))

    def _fragment_path(
        self, tmpdir: Path, key: str, page_index: int, rect: pymupdf.Rect
    ) -> Path:
        """片段裁剪的落点：内容寻址的共享目录，或退回 ``tmpdir``。

        身份 = 源 PDF（路径 + 大小 + mtime）+ 页号 + 裁剪矩形；片段内容只由这
        些决定。摘要进文件名，所以同一片段在任何进程里都解析到同一路径。
        """
        safe = re.sub(r"[^0-9A-Za-z_-]", "_", key)
        shared = getattr(self.config, "latex_fragment_dir", None) or _shared_fragment_dir()
        if not shared:
            return tmpdir / f"frag-{safe}.pdf"
        digest = hashlib.sha1(  # noqa: S324 - 非密码学用途，仅作内容寻址文件名
            "|".join(
                (
                    self._fragment_source_identity(),
                    str(page_index),
                    *(f"{value:.3f}" for value in _rect_key(rect)),
                )
            ).encode("utf-8")
        ).hexdigest()[:20]
        directory = Path(shared)
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError:
            # 共享目录不可写只是失去跨运行复用，不该让整段回退。
            logger.debug("片段共享目录不可用，回退临时目录", exc_info=True)
            return tmpdir / f"frag-{safe}.pdf"
        return directory / f"frag-{digest}.pdf"

    def _fragment_source_identity(self) -> str:
        """源 PDF 的身份串（路径 + 大小 + mtime），用于片段内容寻址。"""
        cached = getattr(self, "_fragment_identity", None)
        if cached is not None:
            return cached
        source_path = _source_pdf_path(self.config)
        identity = str(source_path or "")
        try:
            if source_path is not None:
                stat = Path(source_path).stat()
                identity = f"{source_path}|{stat.st_size}|{stat.st_mtime_ns}"
        except OSError:
            pass
        self._fragment_identity = identity
        return identity

    def _page_height(self, page_index: int) -> float:
        page = self._docs_pages().get(page_index)
        if page is not None and page.cropbox is not None and page.cropbox.box is not None:
            return float(page.cropbox.box.y2) - float(page.cropbox.box.y)
        if 0 <= page_index < len(self.pdf):
            return float(self.pdf[page_index].rect.height)
        return 0.0

    def _reject_job(self, job: dict, reason: str) -> None:
        """把已入选的段落降级为回退（片段不可用等编译期前失败）。"""
        self._reasons.update([reason])
        self._decide(job["debug_id"], reason)

    def _abort_prepared(self) -> None:
        """预选/编译失败：清掉临时目录与选段结果（不跳过任何字符）。"""
        self._jobs = []
        self._compiled = {}
        self.stamped_ids = set()
        self._cleanup()

    def _cleanup(self) -> None:
        tmpdir = self._tmpdir
        self._tmpdir = None
        if tmpdir is not None:
            try:
                tmpdir.cleanup()
            except OSError:
                logger.debug("清理 LaTeX bbox 临时目录失败", exc_info=True)

    # ------------------------------------------------------------------
    def stamp(self, regenerate_pages=None) -> pymupdf.Document:
        """把贴片落到页面；返回最终文档（回滚时为新文档）。

        ``regenerate_pages``：full 模式在「内容流生成时跳过了字符」之后调用时
        由调用方提供的回调 ``callable(page_indices)``，用它在链接校验失败时
        重新生成受影响页（不跳过字符）以回滚；为空表示调用方未做预选跳过
        （此时回退到 overlay 前字节快照，与旧行为一致）。
        """
        try:
            if self.mode == "repair":
                return self._apply_legacy()
            if not self._prepared:
                return self.pdf
            if not self._jobs or not self._compiled:
                return self.pdf
            return self._apply_prepared(regenerate_pages)
        except Exception as exc:  # noqa: BLE001 - overlay 任何异常都不阻断翻译
            logger.warning("LaTeX bbox overlay 失败，整体回退现有渲染", exc_info=True)
            self.stats["error"] = f"{type(exc).__name__}: {exc}"
            if regenerate_pages is not None and self.mode != "repair":
                # 内容流已按 stamped_ids 跳过字符：重新生成全部受影响的页，
                # 否则未贴到片的段落会既无旧字符、又无贴片（丢字）。
                pages = sorted({job["page"] for job in self._jobs})
                try:
                    regenerate_pages(pages)
                    self.stats["reverted"] = True
                except Exception:  # noqa: BLE001 - 回滚失败只记录
                    logger.warning("LaTeX bbox 回滚重新生成失败", exc_info=True)
            self._revert()
            return self.pdf
        finally:
            self.stats["decisions"] = self.export_decisions()
            self._event(
                "latex_stamp",
                {
                    "mode": self.mode,
                    "applied": int(self.stats.get("applied") or 0),
                    "reverted": bool(self.stats.get("reverted")),
                    "links": dict(self.stats.get("links") or {}),
                    "error": self.stats.get("error"),
                    "fallback_reasons": dict(
                        self.stats.get("fallback_reasons") or {}
                    ),
                },
            )
            write_report(self.config, self.stats)
            self._cleanup()

    # ------------------------------------------------------------------
    def _apply_legacy(self) -> pymupdf.Document:
        """repair 模式：内容流生成后选段 + redaction + 贴片（旧行为）。"""
        self._load_state()
        self.stats["fusion"] = copy.deepcopy(self._fusion_stats)
        if not self._paragraphs:
            self.stats["fallback"] = 0
            self.stats["fallback_reasons"] = {"no-captured-paragraphs": 0}
            return self.pdf
        capability = probe_latex_capability(
            xelatex_path=getattr(self.config, "latex_xelatex_path", None),
            font_path=getattr(self.config, "latex_cjk_font_path", None),
            primary_font_family=getattr(self.config, "primary_font_family", None),
        )
        self.stats["available"] = capability.available
        self.stats["capability"] = capability.to_dict()
        self._event("latex_capability", capability.to_dict())
        if not capability.available:
            self.stats["fallback"] = len(self._paragraphs)
            self.stats["fallback_reasons"] = {
                "capability-unavailable": len(self._paragraphs)
            }
            for debug_id in self._decisions:
                self._decide(debug_id, "capability-unavailable")
            return self.pdf
        pdf = self.pdf
        jobs, reasons = self._select_candidates(self._paragraphs, self._bodies)
        counter = Counter(reasons)
        selected_ids = {job["debug_id"] for job in jobs}
        self._event(
            "latex_candidates",
            {
                "selected": [
                    {
                        "id": job["debug_id"],
                        "page": job["page"],
                        "width": round(float(job["width"]), 3),
                        "height": round(float(job["height"]), 3),
                        "font_size": round(float(job["font_size"]), 3),
                    }
                    for job in jobs
                ],
                "rejected": {
                    debug_id: record["reason"]
                    for debug_id, record in self._decisions.items()
                    if record["reason"] and debug_id not in selected_ids
                },
                "reason_counts": dict(counter),
            },
        )
        self.stats["attempted"] = len(jobs)
        if not jobs:
            self.stats["fallback"] = len(self._paragraphs)
            self.stats["fallback_reasons"] = dict(counter)
            return pdf
        renderer = self._build_renderer(capability)
        with tempfile.TemporaryDirectory(prefix="babeldoc-latex-bbox-") as tmp:
            jobs = self._substitute_fragments(jobs, Path(tmp))
            if not jobs:
                self.stats["fallback"] = len(self._paragraphs)
                self.stats["fallback_reasons"] = dict(counter)
                return pdf
            requests = [
                (
                    StampRequest(
                        key=job["debug_id"],
                        body=job["body"],
                        width=job["width"],
                        height=job["height"],
                        font_size=job["font_size"],
                        expected_text=self._plain_texts.get(job["debug_id"], ""),
                    ),
                    Path(tmp),
                )
                for job in jobs
            ]
            stamps = renderer.render_many(requests)
            self.stats["compile"] = _compile_stats(renderer, stamps)
            for job in jobs:
                stamp = stamps.get(job["debug_id"])
                if stamp is None:
                    continue
                if stamp.ok and stamp.pdf_path:
                    effective_size = stamp.font_size or job["font_size"]
                    self._decide(
                        job["debug_id"],
                        font_scale=(
                            round(stamp.scale, 4) if stamp.scale is not None else None
                        ),
                        lead=round(effective_size * DEFAULT_LEAD_RATIO, 3),
                        attempts=stamp.compile_attempts,
                    )
                else:
                    self._decide(
                        job["debug_id"],
                        f"compile:{stamp.reason or 'unknown'}",
                        attempts=stamp.compile_attempts,
                    )
            successful = {k: v for k, v in stamps.items() if v.ok and v.pdf_path}
            failed = {
                k: v.reason for k, v in stamps.items() if not (v.ok and v.pdf_path)
            }
            counter.update({f"compile:{reason}": 1 for reason in failed.values()})
            if not successful:
                self.stats["failed"] = len(stamps)
                self.stats["fallback"] = len(self._paragraphs)
                self.stats["fallback_reasons"] = dict(counter)
                return pdf
            pre_bytes = pdf.tobytes(deflate=True, garbage=0)
            affected_pages = {
                job["page"] for job in jobs if job["debug_id"] in successful
            }
            pre_links = {
                page_index: _links_by_signature(pdf[page_index].get_links())
                for page_index in affected_pages
            }
            pre_uri_set = self._uri_set(pdf)
            self.stats["links"]["before_total"] = sum(
                len(page.get_links()) for page in pdf
            )
            applied, restored = self._stamp_pages(jobs, successful)
            counter.update({"applied": applied})
            ok = self._verify_links(pre_links, pre_uri_set)
            self.stats["links"]["uri_set_match"] = ok
            if not ok:
                logger.warning(
                    "LaTeX bbox overlay 链接校验失败，回滚到 overlay 前状态"
                )
                self.stats["reverted"] = True
                doc = pymupdf.open(stream=pre_bytes, filetype="pdf")
                self._reset_applied_stats(doc)
                return doc
            self.stats["applied"] = applied
            self.stats["links"]["restored"] = restored
            self.stats["links"]["after_total"] = sum(
                len(page.get_links()) for page in pdf
            )
            self.stats["failed"] = len(failed)
            self.stats["fallback"] = len(self._paragraphs) - applied
            self.stats["fallback_reasons"] = dict(counter)
            return pdf

    def _apply_prepared(self, regenerate_pages=None) -> pymupdf.Document:
        """full 模式：贴片 prepare 已编译好的贴片并做链接硬校验。"""
        pdf = self.pdf
        jobs = self._jobs
        successful = self._compiled
        counter = Counter(self._reasons)
        affected_pages = {
            job["page"] for job in jobs if job["debug_id"] in successful
        }
        pre_links = {
            page_index: _links_by_signature(pdf[page_index].get_links())
            for page_index in sorted(affected_pages)
        }
        pre_uri_set = self._uri_set(pdf)
        self.stats["links"]["before_total"] = sum(
            len(page.get_links()) for page in pdf
        )
        use_byte_rollback = regenerate_pages is None
        pre_bytes = pdf.tobytes(deflate=True, garbage=0) if use_byte_rollback else None

        applied, restored = self._stamp_pages(jobs, successful)
        counter.update({"applied": applied})
        ok = self._verify_links(pre_links, pre_uri_set)
        self.stats["links"]["uri_set_match"] = ok
        if not ok:
            logger.warning("LaTeX bbox overlay 链接校验失败，回滚")
            self.stats["reverted"] = True
            if use_byte_rollback:
                doc = pymupdf.open(stream=pre_bytes, filetype="pdf")
            else:
                # 内容流已按 stamped_ids 跳过字符：重新生成受影响页
                # （set_contents 会一并丢弃贴片）即回到 overlay 前状态。
                pages = sorted(affected_pages)
                regenerate_pages(pages)
                doc = pdf
                self.stats["links"]["uri_set_match"] = self._verify_links(
                    pre_links, pre_uri_set
                )
            self._reset_applied_stats(doc)
            return doc
        self.stats["applied"] = applied
        self.stats["links"]["restored"] = restored
        self.stats["links"]["after_total"] = sum(
            len(page.get_links()) for page in pdf
        )
        self.stats["failed"] = len(jobs) - applied
        self.stats["fallback"] = len(self._paragraphs) - applied
        self.stats["fallback_reasons"] = dict(counter)
        return pdf

    def _reset_applied_stats(self, doc: pymupdf.Document) -> None:
        self.stats["applied"] = 0
        self.stats["applied_paragraphs"] = []
        self.stats["pages_affected"] = []
        self.stats["links"]["restored"] = 0
        # 回滚后 applied 计数归零，fallback 原因同步改记回滚，避免报告自相矛盾。
        reasons = self.stats.get("fallback_reasons") or {}
        if reasons.get("applied"):
            reasons["reverted-link-check"] = (
                reasons.get("reverted-link-check", 0) + reasons.pop("applied")
            )
        for record in self._decisions.values():
            if record["reason"] == "applied":
                record["reason"] = "reverted-link-check"
                record["fill_after"] = None
                record["stamp_box"] = None
        self.stats["links"]["after_total"] = sum(
            len(page.get_links()) for page in doc
        )

    # ------------------------------------------------------------------
    def _select_candidates(self, paragraphs: dict, bodies: dict):
        """选择可安全替换的段落。返回 (jobs, fallback_reasons)。"""
        pdf = self.pdf
        jobs = []
        reasons = []
        full_mode = self.mode != "repair"

        def reject(debug_id: str, reason: str) -> None:
            """记录聚合原因（现有统计口径）与逐段决策原因。"""
            reasons.append(reason)
            self._decide(debug_id, reason)

        for debug_id, meta in paragraphs.items():
            base_box = meta["box"]
            # P6：编译后按译文版面算出的精修框只替换贴片矩形（见 _box_overrides）。
            override = self._box_overrides.get(debug_id)
            box = list(override) if override else base_box
            page_index = meta["page"]
            label = meta.get("layout_label")
            if full_mode and label not in _BODY_LABELS:
                reject(debug_id, "label-not-eligible")
                continue
            if page_index >= len(pdf):
                reject(debug_id, "page-out-of-range")
                continue
            page = pdf[page_index]
            if page.rotation != 0:
                reject(debug_id, "rotated-page")
                continue
            body = bodies.get(debug_id)
            if not body:
                reject(debug_id, "formula-fusion-failed")
                continue
            # 段落必须仍存在于当前 IR（页号一致）。
            paragraph = self._find_paragraph(page_index, debug_id)
            if paragraph is None or not (paragraph.unicode or "").strip():
                reject(debug_id, "paragraph-missing")
                continue
            if full_mode:
                source_text = meta.get("source_text") or ""
                if source_text and (
                    _normalize_ws(paragraph.unicode) == _normalize_ws(source_text)
                ):
                    # 译文与源文一致：LLM 未翻译（或原样返回），不走 LaTeX。
                    reject(debug_id, "untranslated")
                    continue

            page_rect = page.rect
            x0, y0, x1, y1 = box
            rect = pymupdf.Rect(x0, page_rect.height - y1, x1, page_rect.height - y0)
            width = x1 - x0
            height = y1 - y0
            font_size = meta["font_size"]
            if width < _MIN_BOX_WIDTH or height < font_size * 1.05:
                reject(debug_id, "box-too-small")
                continue
            if (
                rect.x0 < page_rect.x0 - _PAGE_MARGIN
                or rect.y0 < page_rect.y0 - _PAGE_MARGIN
                or rect.x1 > page_rect.x1 + _PAGE_MARGIN
                or rect.y1 > page_rect.y1 + _PAGE_MARGIN
            ):
                reject(debug_id, "box-outside-page")
                continue

            expanded = _box_expanded_beyond(paragraph.box, box)
            if expanded:
                if full_mode:
                    # 扩后区域安全门禁：落有别的段落时不贴片。
                    if self._expanded_box_overlaps_others(
                        debug_id, page_index, paragraph.box
                    ):
                        reject(debug_id, "box-expanded-after-typesetting")
                        continue
                else:
                    reject(debug_id, "box-expanded-after-typesetting")
                    continue

            metrics = measure_line_fill(page, rect)
            self._decide(debug_id, fill_before=metrics["min_body_fill"])
            # 源行几何（P3）：优先用源**页面文本行**（prepare 时页面仍是源文），
            # 比 IL 字符聚类稳定；无文本行时用 capture 的 IL 几何兵底。
            # 精修框改的是贴片矩形：首行量测一律回**原框**口径。clip 回原框，
            # 否则会把邻居源行并进 n_lines/行距；first_line_dx/ascent_top 也相对
            # 原框算，否则 ``\topskip`` 会跟着新框顶一起涨，向上扩出来的高度被首行
            # skip 吃掉（可用行数一点不变）—— 向上扩只钉住口径才真的让出高度。
            row_geometry = None
            if full_mode:
                measure_rect = _page_rect(base_box, page_rect)
                row_geometry = _rows_geometry(
                    measure_source_rows(page, measure_rect), measure_rect
                )
                if row_geometry:
                    self._decide(
                        debug_id,
                        n_lines_source=row_geometry["n_lines"],
                        indent_pt=row_geometry["first_line_dx"],
                    )
            watermark = metrics["watermark"]
            if not watermark and full_mode:
                watermark = any(
                    candidate.intersects(rect)
                    for candidate in self._watermark_rects(page_index)
                )
            if watermark:
                reject(debug_id, "watermark-overlap")
                continue
            if full_mode:
                # 资格：正文本体标签 ∧ 源行数 ≥2（源行数取当前页面里的源文行数）。
                if metrics["n_lines"] < 2:
                    reject(debug_id, "single-line")
                    continue
            elif not meta["has_formula"]:
                # repair（旧行为）：纯文本段落仅在检测到异常短行时才替换。
                if metrics["n_lines"] < 2 or metrics["min_body_fill"] is None:
                    reject(debug_id, "no-body-lines")
                    continue
                if metrics["min_body_fill"] >= self.config.latex_min_line_fill:
                    reject(debug_id, "line-fill-ok")
                    continue

            jobs.append(
                {
                    "debug_id": debug_id,
                    "page": page_index,
                    "rect": rect,
                    # 擦除只针对**原**框：扩框后的区域只贴片，擦除它会连带删掉
                    # 邻居在内容流里的译文（精修框与邻居框重叠、但不与墨迹重叠）。
                    "redact_rect": _page_rect(base_box, page_rect),
                    "width": width,
                    "height": height,
                    "font_size": font_size,
                    "body": body,
                    "row_geometry": row_geometry,
                }
            )
        return jobs, reasons

    # ------------------------------------------------------------------
    def _stamp_pages(self, jobs: list[dict], successful: dict) -> tuple[int, int]:
        """逐页贴片（必要时 redact + 重插链接）。返回 (applied, restored)。

        正常（full 模式预选跳过字符）时 stamp rect 内没有文本层，直接贴片；
        旧路径/直接调用时 rect 内仍有旧译文，才走 redaction 擦除。

        链接生命周期：``apply_redactions`` 会删除与 redaction 矩形相交的 Link
        注记，且 pymupdf 的 link 读缓存在 redaction 后是脏的（``get_links()``
        仍返回已删链接），因此这里用「预判相交矩形」决定哪些链接需要重插，
        重插后再由 ``_verify_links`` 在重新打开的副本上做硬校验。
        """
        pdf = self.pdf
        applied = 0
        restored = 0
        stamp_links_found = 0
        # regenerate 回滚会整页重来：每次贴片从空表重建，避免残留过期矩形。
        self.stamp_link_rects = {}
        by_page: dict[int, list[dict]] = {}
        for job in jobs:
            if job["debug_id"] in successful:
                by_page.setdefault(job["page"], []).append(job)

        for page_index, page_jobs in by_page.items():
            page = pdf[page_index]
            links_before = page.get_links()
            # 只擦除「区域内确有文本层」的矩形：full 模式预选跳过后通常为空。
            redact_rects = [
                job.get("redact_rect", job["rect"]) + (-0.2, -0.2, 0.2, 0.2)
                for job in page_jobs
                if page.get_text("text", clip=job.get("redact_rect", job["rect"])).strip()
            ]
            if redact_rects:
                # 预判：与任一 redaction 矩形相交的链接会被删除，需重插。
                removed = [
                    link
                    for link in links_before
                    if any(
                        pymupdf.Rect(link["from"]).intersects(redact_rect)
                        for redact_rect in redact_rects
                        if link.get("from") is not None
                    )
                ]
                for redact_rect in redact_rects:
                    page.add_redact_annot(redact_rect)
                page.apply_redactions(
                    images=pymupdf.PDF_REDACT_IMAGE_NONE,
                    graphics=pymupdf.PDF_REDACT_LINE_ART_REMOVE_IF_COVERED,
                    text=pymupdf.PDF_REDACT_TEXT_REMOVE,
                )

                # redaction 后重插被删链接（原矩形）；随后 link_remap 会按原矩形
                # 匹配并重定位到译文/贴片位置。
                from babeldoc.format.pdf.document_il.backend.link_remap import (
                    safe_insert_link,
                )

                for link in removed:
                    try:
                        safe_insert_link(page, link)
                        restored += 1
                    except Exception:  # noqa: BLE001 - 单条链接失败由硬校验兜底
                        logger.debug("重插链接失败", exc_info=True)

            # 贴片：stamp 页面尺寸与 bbox 一致，1:1 映射。
            for job in page_jobs:
                stamp_result = successful[job["debug_id"]]
                stamp_doc = pymupdf.open(stamp_result.pdf_path)
                try:
                    stamp_links_found += self._collect_stamp_links(
                        stamp_doc, page_index, job
                    )
                    page.show_pdf_page(
                        job["rect"],
                        stamp_doc,
                        0,
                        keep_proportion=False,
                        overlay=True,
                    )
                finally:
                    stamp_doc.close()
                applied += 1
                self.stats["applied_paragraphs"].append(job["debug_id"])
                self._decide(
                    job["debug_id"],
                    "applied",
                    fill_after=measure_line_fill(page, job["rect"])["min_body_fill"],
                    stamp_box=[
                        round(float(job["rect"].x0), 3),
                        round(float(job["rect"].y0), 3),
                        round(float(job["rect"].x1), 3),
                        round(float(job["rect"].y1), 3),
                    ],
                )
            self.stats["pages_affected"].append(page_index)
        self.stats["links"]["stamp_marked"] = stamp_links_found
        return applied, restored

    def _collect_stamp_links(
        self, stamp_doc: pymupdf.Document, page_index: int, job: dict
    ) -> int:
        """读印章内的 ``bdoclink://l<N>`` 链接注记，映射为页面坐标矩形。

        融合阶段把链接覆盖的角标 run 包进 ``\\href{bdoclink://l<N>}``，
        XeLaTeX 落成印章页自己的 URI 注记——矩形就是上标引文在印章里的
        **真实墨迹位置**。``show_pdf_page`` 只搬内容流不搬注记，所以这里
        读出来存进 ``stamp_link_rects``，由链接重映射换成真实目标。

        坐标：印章页与 job rect 同尺寸（构造保证），仿射 1:1 缩放即可；
        两边都是 pymupdf 页面坐标（左上原点），无需翻转。
        """
        from babeldoc.format.pdf.document_il.backend.latex_bbox.fusion import (
            STAMP_LINK_URI_PREFIX,
        )

        found = 0
        try:
            stamp_page = stamp_doc[0]
            stamp_rect = stamp_page.rect
            if stamp_rect.width <= 0 or stamp_rect.height <= 0:
                return 0
            sx = job["rect"].width / stamp_rect.width
            sy = job["rect"].height / stamp_rect.height
            page_map = self.stamp_link_rects.setdefault(page_index, {})
            for link in stamp_page.get_links():
                uri = link.get("uri") or ""
                if not uri.startswith(STAMP_LINK_URI_PREFIX):
                    continue
                digits = uri[len(STAMP_LINK_URI_PREFIX) :]
                if not digits.isdigit():
                    continue
                rect = link.get("from")
                if rect is None:
                    continue
                mapped = pymupdf.Rect(
                    job["rect"].x0 + rect.x0 * sx,
                    job["rect"].y0 + rect.y0 * sy,
                    job["rect"].x0 + rect.x1 * sx,
                    job["rect"].y0 + rect.y1 * sy,
                )
                if mapped.is_empty:
                    continue
                page_map.setdefault(int(digits), []).append(mapped)
                found += 1
        except Exception:  # noqa: BLE001 - 标记链接丢失只降级到常规重映射
            logger.debug("读取印章标记链接失败", exc_info=True)
        return found

    # ------------------------------------------------------------------
    def _verify_links(self, pre_links: dict, pre_uri_set: set) -> bool:
        """在重新打开的副本上做链接硬校验。

        不能读内存文档的 ``get_links()``：``apply_redactions`` / ``insert_link`` /
        ``show_pdf_page`` 之后 pymupdf 的 link 读缓存是脏的（会漏报新插链接、
        误报已删链接）。用 ``tobytes()`` 重新打开得到真实落盘结果。
        """
        probe = pymupdf.open("pdf", self.pdf.tobytes(deflate=True, garbage=0))
        try:
            for page_index, before in pre_links.items():
                if page_index >= len(probe):
                    return False
                after = _links_by_signature(probe[page_index].get_links())
                if after != before:
                    return False
            return self._uri_set(probe) == pre_uri_set
        finally:
            probe.close()

    @staticmethod
    def _uri_set(pdf: pymupdf.Document) -> set[str]:
        uris: set[str] = set()
        for page in pdf:
            for link in page.get_links():
                if link.get("uri"):
                    uris.add(link["uri"])
        return uris

    def _revert(self) -> bool:
        """异常兜底回滚（无字节快照时尽力保持现状并报告）。"""
        self.stats["applied"] = 0
        self.stats["applied_paragraphs"] = []
        self.stats["pages_affected"] = []
        return False


def measure_line_fills(page: pymupdf.Page, clip: pymupdf.Rect) -> dict:
    """量测 clip 区域内每行的填充率（相对 box 宽度的占比）。

    返回 ``{"n_lines", "fills", "min_body_fill", "watermark"}``；
    ``fills`` 为按纵坐标排序的逐行填充率，``min_body_fill`` 取非末行最小值
    （与 :func:`measure_line_fill` 口径一致，供验收脚本做分布统计）。
    """
    box_width = clip.width
    if box_width <= 0:
        return {
            "n_lines": 0,
            "fills": [],
            "min_body_fill": None,
            "watermark": False,
        }
    rows: dict[float, list] = {}
    watermark = False
    text_all = ""
    for block in page.get_text("dict", clip=clip)["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = [s for s in line["spans"] if s["text"].strip()]
            if not spans:
                continue
            key = round(line["bbox"][1], 0)
            rows.setdefault(key, []).append(
                (
                    min(s["bbox"][0] for s in spans),
                    max(s["bbox"][2] for s in spans),
                )
            )
            text_all += "".join(s["text"] for s in spans)
    for marker in _WATERMARK_MARKERS:
        if marker in text_all:
            watermark = True
            break
    fills = []
    for key in sorted(rows):
        x0 = min(r[0] for r in rows[key])
        x1 = max(r[1] for r in rows[key])
        fills.append((x1 - x0) / box_width)
    body_fills = fills[:-1] if len(fills) > 1 else []
    return {
        "n_lines": len(fills),
        "fills": fills,
        "min_body_fill": min(body_fills) if body_fills else None,
        "watermark": watermark,
    }


def write_report(config, stats: dict) -> Path | None:
    """把 overlay 统计写入 working_dir/latex_bbox_report.json。"""
    working_dir = getattr(config, "working_dir", None)
    if not working_dir:
        return None
    try:
        path = Path(working_dir) / "latex_bbox_report.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(stats, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path
    except OSError:
        logger.warning("写入 latex_bbox_report.json 失败", exc_info=True)
        return None


#: 供 PDFCreater 复用的执行入口（含一次性保护）。
_overlay_lock = threading.Lock()


def apply_latex_bbox_overlay(
    pdf: pymupdf.Document, docs, config
) -> tuple[pymupdf.Document, dict]:
    """直接执行完整 overlay（prepare + stamp）：返回 (最终文档, 统计)。

    ``PDFCreater.write`` 需要「内容流生成前预选、生成后贴片」两步，它直接用
    :class:`LatexBboxOverlay`；本函数供独立调用方（测试/实验脚本）使用，此时
    旧译文已在内容流里，``stamp`` 会自动走 redaction 兜底。
    """
    with _overlay_lock:
        overlay = LatexBboxOverlay(pdf, docs, config)
        try:
            overlay.prepare()
        except Exception:  # noqa: BLE001 - 预选失败只是不贴片
            logger.warning("LaTeX bbox 预选失败，保留现有渲染", exc_info=True)
        # stamp 内部已把 decisions 与报告落盘（任何路径）。
        return overlay.stamp(), overlay.stats
