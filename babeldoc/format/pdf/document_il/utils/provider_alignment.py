"""原生字符 ↔ MinerU provider span 对齐（交叉印证）。

背景（对应 .plan/minerU深度融合.md 板块 2）：MinerU 提供文档结构，BabelDOC 的
原生 ``PdfCharacter`` 才是重建真源。本模块把每页原生字符按 bbox 对齐到 provider
IR 的 span 上，用对齐结果做两件事：

1. **审计**：``alignment.json`` 记录匹配率、ambiguous/unmatched 明细，并提供
   span 文本与原生字符文本的一致性校验（``span_text_mismatch``）；
2. **行内公式保护**：MinerU 的 ``inline_equation`` span 覆盖的原生字符，由
   ``inline_math_protector`` 转成 ``formula`` 布局区域，进而被既有
   ParagraphFinder/StylesAndFormulas 链路聚成 ``PdfFormula`` → 翻译输入里
   显示为 ``{vN}`` 占位符（而不是裸文本公式）。

坐标约定：MinerU bbox 是页面坐标（左上原点），原生字符 bbox 是 PDF 坐标
（左下原点）。对齐前把 MinerU bbox 用页高转换到 IL 坐标（``y' = H - y``）。

匹配规则（与任务书一致，按顺序）：

1. 字符中心落在 span bbox 内 → 直接匹配；
2. span 覆盖字符面积 ≥ 0.6（字符面积在 span 内的占比） → 匹配；
3. 回退到 line bbox（同样用规则 1/2）。

多个 span 同时命中判为 ``ambiguous``，取覆盖率更高者（同分取面积更小者，
小 span 更可能是精确的公式盒子）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from dataclasses import field
from typing import Any

from babeldoc.docvision.provider_ir import ProviderBlock
from babeldoc.docvision.provider_ir import ProviderDocument
from babeldoc.docvision.provider_ir import ProviderLine
from babeldoc.docvision.provider_ir import ProviderPage
from babeldoc.docvision.provider_ir import ProviderSpan
from babeldoc.format.pdf.document_il import il_version_1

logger = logging.getLogger(__name__)

# 字符面积落在 span 内的比例阈值（规则 2）。
SPAN_CONTAINMENT_THRESHOLD = 0.6
# 字符中心落入 bbox 的容差（pt），抵消坐标舍入与 ±1 padding。
CENTER_TOLERANCE = 1.0
# span 文本一致性校验的摘要长度。
TEXT_SUMMARY_LIMIT = 40


@dataclass(slots=True)
class ProviderBox:
    """已转到 IL 坐标的 provider bbox（左下原点）。"""

    x: float
    y: float
    x2: float
    y2: float

    @property
    def area(self) -> float:
        return max(0.0, self.x2 - self.x) * max(0.0, self.y2 - self.y)


def to_il_box(bbox: Any, page_height: float) -> ProviderBox | None:
    """MinerU 页面坐标 bbox → IL 坐标 ``ProviderBox``。

    ``y' = H - y``：MinerU 的 y 向下，IL 的 y 向上。bbox 非法时返回 None。
    """
    if not isinstance(bbox, list | tuple) or len(bbox) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(v) for v in bbox)
    except (TypeError, ValueError):
        return None
    return ProviderBox(x=x0, y=page_height - y1, x2=x1, y2=page_height - y0)


def _center_inside(box: ProviderBox, char_box) -> bool:
    cx = (float(char_box.x) + float(char_box.x2)) / 2
    cy = (float(char_box.y) + float(char_box.y2)) / 2
    return (
        box.x - CENTER_TOLERANCE <= cx <= box.x2 + CENTER_TOLERANCE
        and box.y - CENTER_TOLERANCE <= cy <= box.y2 + CENTER_TOLERANCE
    )


def _containment(box: ProviderBox, char_box) -> float:
    """字符面积落在 ``box`` 内的比例（0..1）。"""
    char_area = max(0.0, float(char_box.x2) - float(char_box.x)) * max(
        0.0, float(char_box.y2) - float(char_box.y)
    )
    if char_area <= 0:
        return 0.0
    x0 = max(box.x, float(char_box.x))
    y0 = max(box.y, float(char_box.y))
    x1 = min(box.x2, float(char_box.x2))
    y1 = min(box.y2, float(char_box.y2))
    if x1 <= x0 or y1 <= y0:
        return 0.0
    return (x1 - x0) * (y1 - y0) / char_area


@dataclass(slots=True)
class SpanMatch:
    """一个 provider span/line 与原生字符的对齐结果。"""

    span_id: str
    kind: str
    matched: bool
    method: str | None = None  # center | containment | line_center | line_containment
    char_indices: list[int] = field(default_factory=list)
    ambiguous: bool = False
    char_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "span_id": self.span_id,
            "kind": self.kind,
            "matched": self.matched,
            "method": self.method,
            "char_count": self.char_count,
            "ambiguous": self.ambiguous,
        }


@dataclass(slots=True)
class PageAlignment:
    """一页的对齐审计结果。"""

    page_index: int
    total_chars: int = 0
    matched_chars: int = 0
    unmatched_chars: int = 0
    ambiguous_chars: int = 0
    char_to_span: dict[int, str] = field(default_factory=dict)
    span_to_chars: dict[str, list[int]] = field(default_factory=dict)
    inline_matches: list[SpanMatch] = field(default_factory=list)
    span_text_mismatch: int = 0
    span_text_samples: list[dict[str, Any]] = field(default_factory=list)

    @property
    def coverage(self) -> float:
        if self.total_chars <= 0:
            return 1.0
        return self.matched_chars / self.total_chars

    def to_dict(self) -> dict[str, Any]:
        assert self.total_chars == self.matched_chars + self.unmatched_chars, (
            "matched/unmatched 统计必须覆盖全部字符"
        )
        return {
            "page_index": self.page_index,
            "total_chars": self.total_chars,
            "matched_chars": self.matched_chars,
            "unmatched_chars": self.unmatched_chars,
            "ambiguous_chars": self.ambiguous_chars,
            "coverage": round(self.coverage, 6),
            "span_to_chars": {k: v for k, v in self.span_to_chars.items() if v},
            "inline_equation": [
                match.to_dict()
                for match in self.inline_matches
            ],
            "span_text_mismatch": self.span_text_mismatch,
            "span_text_samples": self.span_text_samples[:20],
        }


@dataclass(slots=True)
class AlignmentReport:
    """整篇对齐审计（落盘 alignment.json）。"""

    version: int = 1
    pages: list[PageAlignment] = field(default_factory=list)
    protected_inline_math: list[dict[str, Any]] = field(default_factory=list)

    @property
    def total_chars(self) -> int:
        return sum(page.total_chars for page in self.pages)

    @property
    def matched_chars(self) -> int:
        return sum(page.matched_chars for page in self.pages)

    @property
    def inline_total(self) -> int:
        return sum(len(page.inline_matches) for page in self.pages)

    @property
    def inline_matched(self) -> int:
        return sum(
            1 for page in self.pages for match in page.inline_matches if match.matched
        )

    def to_dict(self) -> dict[str, Any]:
        total = self.total_chars
        matched = self.matched_chars
        return {
            "version": self.version,
            "summary": {
                "page_count": len(self.pages),
                "total_chars": total,
                "matched_chars": matched,
                "unmatched_chars": sum(p.unmatched_chars for p in self.pages),
                "ambiguous_chars": sum(p.ambiguous_chars for p in self.pages),
                "native_char_coverage": round(matched / total, 6) if total else 1.0,
                "inline_equation_total": self.inline_total,
                "inline_equation_matched": self.inline_matched,
                "span_text_mismatch": sum(p.span_text_mismatch for p in self.pages),
                "protected_inline_math": len(self.protected_inline_math),
            },
            "pages": [page.to_dict() for page in self.pages],
            "protected_inline_math": self.protected_inline_math,
        }


@dataclass(slots=True)
class _Candidate:
    """一个候选 provider span（含 IL 坐标 box 与所属 line）。"""

    span: ProviderSpan
    box: ProviderBox
    line: ProviderLine
    line_box: ProviderBox | None


def _page_chars(page: il_version_1.Page) -> list[il_version_1.PdfCharacter]:
    """页内全部原生字符（LayoutParser 之后 ``page.pdf_character`` 仍是全量）。"""
    return list(page.pdf_character or [])


def _iter_candidates(provider_page: ProviderPage, page_height: float):
    """深度优先产出页内全部 span 候选（带 IL 坐标 box）。"""
    for block in provider_page.iter_blocks(recursive=True):
        for line in block.lines:
            line_box = to_il_box(line.bbox, page_height)
            for span in line.spans:
                box = to_il_box(span.bbox, page_height)
                if box is None:
                    continue
                yield _Candidate(span=span, box=box, line=line, line_box=line_box)


def _char_box(char: il_version_1.PdfCharacter):
    if char.visual_bbox is not None and char.visual_bbox.box is not None:
        return char.visual_bbox.box
    return char.box


def align_page(
    page: il_version_1.Page,
    provider_page: ProviderPage,
    page_height: float,
) -> PageAlignment:
    """把一页原生字符对齐到 provider span/line 上。"""
    result = PageAlignment(page_index=provider_page.page_index)
    chars = _page_chars(page)
    result.total_chars = len(chars)
    if not chars:
        return result

    candidates = list(_iter_candidates(provider_page, page_height))
    boxes = [_char_box(char) for char in chars]

    # 先按「中心命中」建候选，再按「覆盖率」补；两类都命中判 ambiguous。
    for index, char_box in enumerate(boxes):
        if char_box is None:
            result.unmatched_chars += 1
            continue
        hits: list[tuple[int, str, float, float]] = []
        for candidate in candidates:
            reason_rank = None
            score = 0.0
            if _center_inside(candidate.box, char_box):
                reason_rank, score = 0, 1.0
            else:
                containment = _containment(candidate.box, char_box)
                if containment >= SPAN_CONTAINMENT_THRESHOLD:
                    reason_rank, score = 1, containment
            if reason_rank is None:
                continue
            if candidate.line_box is not None:
                line_containment = _containment(candidate.line_box, char_box)
                score = max(score, line_containment)
            hits.append((reason_rank, candidate.span.span_id, score, candidate.box.area))

        if not hits:
            # 回退：line bbox 匹配（规则 3）。
            line_hits: list[tuple[str, float, float]] = []
            for candidate in candidates:
                if candidate.line_box is None:
                    continue
                if _center_inside(candidate.line_box, char_box):
                    line_hits.append((candidate.span.span_id, 1.0, candidate.box.area))
                else:
                    line_containment = _containment(candidate.line_box, char_box)
                    if line_containment >= SPAN_CONTAINMENT_THRESHOLD:
                        line_hits.append(
                            (candidate.span.span_id, line_containment, candidate.box.area)
                        )
            if not line_hits:
                result.unmatched_chars += 1
                continue
            line_hits.sort(key=lambda item: (-item[1], item[2]))
            if len(line_hits) > 1:
                result.ambiguous_chars += 1
            span_id = line_hits[0][0]
            result.char_to_span[index] = span_id
            result.span_to_chars.setdefault(span_id, []).append(index)
            result.matched_chars += 1
            continue

        hits.sort(key=lambda item: (item[0], -item[2], item[3]))
        if len(hits) > 1:
            result.ambiguous_chars += 1
        span_id = hits[0][1]
        result.char_to_span[index] = span_id
        result.span_to_chars.setdefault(span_id, []).append(index)
        result.matched_chars += 1

    # inline_equation span 明细 + 文本一致性校验。
    for candidate in candidates:
        if candidate.span.kind != "inline_equation":
            continue
        char_indices = sorted(result.span_to_chars.get(candidate.span.span_id, []))
        match = SpanMatch(
            span_id=candidate.span.span_id,
            kind=candidate.span.kind,
            matched=bool(char_indices),
            method="char_bbox" if char_indices else None,
            char_indices=char_indices,
            char_count=len(char_indices),
        )
        result.inline_matches.append(match)
        if char_indices:
            native_text = "".join(
                (chars[i].char_unicode or "") for i in char_indices
            )
            if not _span_text_consistent(candidate.span.content, native_text):
                result.span_text_mismatch += 1
                if len(result.span_text_samples) < 20:
                    result.span_text_samples.append(
                        {
                            "span_id": candidate.span.span_id,
                            "span_content": _summary(candidate.span.content),
                            "native_text": _summary(native_text),
                        }
                    )

    return result


def _summary(text: str) -> str:
    cleaned = " ".join((text or "").split())
    if len(cleaned) <= TEXT_SUMMARY_LIMIT:
        return cleaned
    return cleaned[:TEXT_SUMMARY_LIMIT] + "…"


_LATEX_NOISE = (
    "mathrm",
    "mathcal",
    "mathbb",
    "widehat",
    "widetilde",
    "left",
    "right",
    "times",
    "leqslant",
    "sqrt",
    "cal",
    "dot",
    "hat",
    "bar",
)


def _letters(text: str) -> str:
    """归一化成「字母数字」用于跨表示比对（LaTeX vs 原生字符）。"""
    import unicodedata

    normalized = unicodedata.normalize("NFKC", text or "")
    for token in _LATEX_NOISE:
        normalized = normalized.replace(token, " ")
    return "".join(ch for ch in normalized if ch.isalnum())


def _span_text_consistent(span_content: str, native_text: str) -> bool:
    """span 文本与对齐到的原生字符是否一致（只做宽松字母集比对，不阻断）。"""
    span_letters = _letters(span_content)
    native_letters = _letters(native_text)
    if not span_letters:
        return True
    if not native_letters:
        return False
    hit = sum(1 for ch in set(span_letters) if ch in native_letters)
    return hit / len(set(span_letters)) >= 0.6


def align_document(
    docs: il_version_1.Document,
    provider_document: ProviderDocument,
) -> AlignmentReport:
    """整篇对齐：按 ``page_index`` 匹配 IL Page 与 ProviderPage。"""
    by_index = {page.page_index: page for page in provider_document.pages}
    report = AlignmentReport()
    for page in docs.page:
        provider_page = by_index.get(page.page_number)
        if provider_page is None:
            continue
        page_height = 0.0
        if page.cropbox is not None and page.cropbox.box is not None:
            page_height = float(page.cropbox.box.y2) - float(page.cropbox.box.y)
        report.pages.append(align_page(page, provider_page, page_height))
    return report


def inline_equation_regions(
    provider_page: ProviderPage,
    page_height: float,
) -> list[ProviderBox]:
    """页内 inline_equation span 的 IL 坐标 bbox 列表（供 protector 转布局区域）。"""
    regions: list[ProviderBox] = []
    for candidate in _iter_candidates(provider_page, page_height):
        if candidate.span.kind == "inline_equation":
            regions.append(candidate.box)
    return regions
