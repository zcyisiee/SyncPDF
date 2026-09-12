"""generate 阶段的选择性 LaTeX bbox overlay。

生命周期（``pdf_creater.write``）：

1. :meth:`LatexBboxOverlay.prepare` —— **内容流生成之前**：按资格门禁选段、
   编译 XeLaTeX 贴片，返回「已成功编译」的 debug_id 集合（``stamped_ids``）；
2. 调用方在生成内容流时跳过 ``stamped_ids`` 的字符（含公式 form/curve），
   已贴片段落的旧路径译文不再进内容流 —— 无双层文本靠构造保证；
3. :meth:`LatexBboxOverlay.stamp` —— 内容流生成之后：把贴片落到 bbox 矩形。

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

import json
import logging
import re
import tempfile
import threading
from collections import Counter
from pathlib import Path

import pymupdf

from babeldoc.format.pdf.document_il.backend.latex_bbox.capability import (
    probe_latex_capability,
)
from babeldoc.format.pdf.document_il.backend.latex_bbox.fusion import FormulaLatexIndex
from babeldoc.format.pdf.document_il.backend.latex_bbox.fusion import fuse_paragraph
from babeldoc.format.pdf.document_il.backend.latex_bbox.fusion import (
    iter_composition_units,
)
from babeldoc.format.pdf.document_il.backend.latex_bbox.renderer import (
    DEFAULT_LEAD_RATIO,
)
from babeldoc.format.pdf.document_il.backend.latex_bbox.renderer import (
    BboxStampRenderer,
)
from babeldoc.format.pdf.document_il.backend.latex_bbox.renderer import StampRequest
from babeldoc.format.pdf.document_il.backend.latex_bbox.renderer import StampResult

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


def capture_layout_sources(docs, config, source_texts: dict | None = None) -> dict:
    """Typesetting 之前捕获 LaTeX overlay 所需的源几何与融合 body。

    必须在 ``Typesetting`` 之前调用：此时段落 box / 公式 box 仍是源坐标，
    composition 仍由 ``parse_translate_output`` 按译文顺序回填（融合的
    顺序契约成立）。结果写入 ``config.latex_bbox_state``。

    ``source_texts``（debug_id → 源文）用于未翻译段判定：workflow 路径从
    ``state.pkl`` 的 ``inputs`` 传入，high_level 路径用
    :func:`record_source_texts` 预先写入 ``config.latex_source_texts``。
    """
    if source_texts is None:
        source_texts = getattr(config, "latex_source_texts", None) or {}
    latex_index = FormulaLatexIndex.from_documents(docs, config)
    paragraphs: dict[str, dict] = {}
    bodies: dict[str, str] = {}
    plain_texts: dict[str, str] = {}
    failures: dict[str, list[str]] = {}

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
            paragraphs[paragraph.debug_id] = meta
            fused = fuse_paragraph(
                paragraph, page.page_number, page_font_map, latex_index
            )
            if fused.ok:
                bodies[paragraph.debug_id] = fused.body
                plain_texts[paragraph.debug_id] = fused.plain_text
            else:
                failures[paragraph.debug_id] = fused.reasons

    state = {
        "paragraphs": paragraphs,
        "bodies": bodies,
        "plain_texts": plain_texts,
        "fusion_failures": failures,
        "provider_inline_spans": latex_index.total_spans if latex_index else 0,
    }
    config.latex_bbox_state = state
    return state


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
        self._watermark_rects_cache: dict[int, list[pymupdf.Rect]] = {}

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
                "fill_before": None,
                "fill_after": None,
                "font_scale": None,
                "lead": None,
                "attempts": None,
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
        return set(self.stamped_ids)

    def _prepare_jobs(self, capability) -> None:
        jobs, reasons = self._select_candidates(self._paragraphs, self._bodies)
        self._reasons = Counter(reasons)
        self.stats["attempted"] = len(jobs)
        if not jobs:
            self.stats["fallback"] = len(self._paragraphs)
            self.stats["fallback_reasons"] = dict(self._reasons)
            return
        self._tmpdir = tempfile.TemporaryDirectory(prefix="babeldoc-latex-bbox-")
        renderer = BboxStampRenderer(
            capability,
            timeout_seconds=getattr(self.config, "latex_compile_timeout_seconds", 45.0),
            max_workers=getattr(self.config, "latex_max_compile_workers", 2),
        )
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
                Path(self._tmpdir.name),
            )
            for job in jobs
        ]
        stamps = renderer.render_many(requests)
        self.stats["compile"] = {
            "attempts": sum(r.compile_attempts for r in stamps.values()),
            "seconds": round(renderer.compile_seconds, 3),
            "cache_hits": renderer.cache_hits,
        }
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
            write_report(self.config, self.stats)
            self._cleanup()

    # ------------------------------------------------------------------
    def _apply_legacy(self) -> pymupdf.Document:
        """repair 模式：内容流生成后选段 + redaction + 贴片（旧行为）。"""
        self._load_state()
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
        self.stats["attempted"] = len(jobs)
        if not jobs:
            self.stats["fallback"] = len(self._paragraphs)
            self.stats["fallback_reasons"] = dict(counter)
            return pdf
        renderer = BboxStampRenderer(
            capability,
            timeout_seconds=getattr(self.config, "latex_compile_timeout_seconds", 45.0),
            max_workers=getattr(self.config, "latex_max_compile_workers", 2),
        )
        with tempfile.TemporaryDirectory(prefix="babeldoc-latex-bbox-") as tmp:
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
            self.stats["compile"] = {
                "attempts": sum(r.compile_attempts for r in stamps.values()),
                "seconds": round(renderer.compile_seconds, 3),
                "cache_hits": renderer.cache_hits,
            }
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
            box = meta["box"]
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
                    "width": width,
                    "height": height,
                    "font_size": font_size,
                    "body": body,
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
        by_page: dict[int, list[dict]] = {}
        for job in jobs:
            if job["debug_id"] in successful:
                by_page.setdefault(job["page"], []).append(job)

        for page_index, page_jobs in by_page.items():
            page = pdf[page_index]
            links_before = page.get_links()
            # 只擦除「区域内确有文本层」的矩形：full 模式预选跳过后通常为空。
            redact_rects = [
                job["rect"] + (-0.2, -0.2, 0.2, 0.2)
                for job in page_jobs
                if page.get_text("text", clip=job["rect"]).strip()
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
                )
            self.stats["pages_affected"].append(page_index)
        return applied, restored

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
