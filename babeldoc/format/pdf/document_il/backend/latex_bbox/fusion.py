"""译文文本与 MinerU 公式的 LaTeX 融合（Typesetting 之前执行）。

对应架构原则 2：译文中的公式以 ``{vN}`` 占位符存在，公式源码不在译文里。
本模块在 **Typesetting 之前**（composition 仍由 ``parse_translate_output``
按译文顺序回填、公式 box 仍是源坐标）把 ``paragraph.unicode`` 转成可安全
交给 XeLaTeX 的 body：

1. prose 逐字符 LaTeX 转义（``& % $ # _ { } ~ ^ \\`` 等特殊字符）；
2. ``<style id='N'>…</style>`` / ``<bN>…</bN>`` 富文本标记按 composition
   中对应样式 run 解析为 ``\\textbf{}``/``\\textit{}``；
3. ``{vN}`` 按出现顺序对应 composition 中第 N 个 ``PdfFormula`` 对象
   （parse_translate_output 按译文顺序回填公式），用公式**源 bbox** 匹配
   MinerU ``inline_equation`` span 取 LaTeX 源码并包裹 ``$...$``；
4. 任何环节无法建立一一对应（占位符数量不符、公式无 MinerU 匹配、
   顺序校验失败）→ 返回"不可替换"，走现有 ``PdfFormula`` 矢量路径，
   绝不猜测公式。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from dataclasses import field

logger = logging.getLogger(__name__)

FORMULA_PLACEHOLDER = re.compile(r"\{v(\d+)\}")
STYLE_OPEN = re.compile(r"<style\s+id='(\d+)'\s*>", re.IGNORECASE)
STYLE_CLOSE = re.compile(r"</style\s*>", re.IGNORECASE)
# 其他 translator 的富文本标记（<b3>/</b3>、<b>/</b>、<i> 等）。
GENERIC_TAG = re.compile(r"</?[biue](?:\d+)?>|</?(?:strong|em)>", re.IGNORECASE)

# 与实验脚本 latex_box.py 相同的转义表。
LATEX_SPECIALS = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}

# formula 源 box 与 MinerU span 的匹配阈值：IoU 或中心点包含。
_MIN_IOU = 0.25


def escape_latex(text: str) -> str:
    """把 prose 转义成 LaTeX 字面文本（不会进入数学模式）。"""
    return "".join(LATEX_SPECIALS.get(ch, ch) for ch in text)


@dataclass(slots=True)
class FuseResult:
    """一个段落的融合结果。"""

    ok: bool = False
    body: str = ""
    #: 期望可见文本（去标记、去公式），用于检测贴片后是否丢字。
    plain_text: str = ""
    formula_count: int = 0
    formulas_matched: int = 0
    style_runs: int = 0
    reasons: list[str] = field(default_factory=list)


def _normalize_for_compare(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def iter_composition_units(paragraph):
    """按顺序产出 composition 单元，用于与译文 token 流对齐。

    覆盖 ``parse_translate_output`` 回填后的全部形态：pdf_formula、
    pdf_same_style_unicode_characters（译文文本/样式 run）、
    pdf_same_style_characters（passthrough 样式 run）、pdf_character。
    """
    for composition in paragraph.pdf_paragraph_composition or []:
        if composition is None:
            continue
        if composition.pdf_formula is not None:
            yield ("formula", composition.pdf_formula, None)
        elif composition.pdf_same_style_unicode_characters is not None:
            run = composition.pdf_same_style_unicode_characters
            yield ("text", run.unicode or "", run.pdf_style)
        elif composition.pdf_same_style_characters is not None:
            run = composition.pdf_same_style_characters
            yield (
                "text",
                "".join(c.char_unicode or "" for c in run.pdf_character or []),
                run.pdf_style,
            )
        elif composition.pdf_character is not None:
            char = composition.pdf_character
            yield ("text", char.char_unicode or "", char.pdf_style)


def _style_flags(style, page_font_map) -> tuple[bool, bool]:
    """从 composition 样式解析 (bold, italic)。"""
    if style is None or not style.font_id:
        return False, False
    font = page_font_map.get(style.font_id)
    if font is None:
        return False, False
    bold = bool(font.bold)
    italic = bool(font.italic)
    if not (bold or italic) and font.name:
        lowered = font.name.lower()
        bold = "bold" in lowered or "black" in lowered
        italic = "italic" in lowered or "oblique" in lowered
    return bold, italic


def _style_command(bold: bool, italic: bool) -> str:
    if bold and italic:
        return r"\textbf{\textit{%s}}"
    if bold:
        return r"\textbf{%s}"
    if italic:
        return r"\textit{%s}"
    return "%s"


def _iter_events(text: str):
    """按位置产出 (start, priority, kind, payload) 标记事件流。"""
    events = []
    for m in FORMULA_PLACEHOLDER.finditer(text):
        events.append((m.start(), 0, "formula", (m.start(), m.group(1), m.end())))
    for m in STYLE_OPEN.finditer(text):
        events.append((m.start(), 1, "style-open", (m.start(), m.end())))
    for m in STYLE_CLOSE.finditer(text):
        events.append((m.start(), 1, "style-close", (m.start(), m.end())))
    for m in GENERIC_TAG.finditer(text):
        events.append((m.start(), 2, "generic-tag", (m.start(), m.end())))
    events.sort(key=lambda item: (item[0], item[1]))
    return events


def parse_segments(text: str) -> list[tuple[str, str]] | None:
    """把译文切成顺序 segment 列表：("text"|"formula"|"styled", content)。

    ``styled`` 为 ``<style>``/``<bN>`` 成对标记包裹的内容；不成对的关闭
    标记按普通文本处理。返回 None 表示标记嵌套异常（不可替换）。
    """
    segments: list[tuple[str, str]] = []
    cursor = 0
    open_tag_end = None  # styled 段内容起点
    depth = 0
    for _, _, kind, payload in _iter_events(text):
        start, end = payload[0], payload[-1]
        if kind == "formula":
            if depth == 0:
                if start > cursor:
                    segments.append(("text", text[cursor:start]))
                segments.append(("formula", payload[1]))
                cursor = end
        elif kind in ("style-open", "generic-tag"):
            tag = text[start:end]
            if depth == 0 and not tag.startswith("</"):
                if start > cursor:
                    segments.append(("text", text[cursor:start]))
                open_tag_end = end
                depth = 1
            # 嵌套打开或关闭态下的打开：按普通文本保留（后续兜底检查会拒绝）。
        elif kind in ("style-close",):
            if depth == 1:
                segments.append(("styled", text[open_tag_end:start]))
                depth = 0
                cursor = end
            elif depth == 0:
                # 未配对的 </style>：并入普通文本。
                if start > cursor:
                    segments.append(("text", text[cursor:start]))
                cursor = start  # 标签本身按文本保留
    if depth != 0:
        return None
    if cursor < len(text):
        segments.append(("text", text[cursor:]))
    return segments


class FormulaLatexIndex:
    """MinerU ``inline_equation`` span 的 LaTeX 源码索引（IL 坐标）。

    MinerU bbox 为 top-left 原点，转换沿用 provider_alignment 的页高翻转；
    匹配策略：IoU 优先，公式源 box 中心点落入 span box 兜底。
    """

    def __init__(self, spans_by_page: dict[int, list[tuple[tuple, str]]]):
        self._spans_by_page = spans_by_page

    @property
    def total_spans(self) -> int:
        return sum(len(entries) for entries in self._spans_by_page.values())

    @classmethod
    def from_documents(
        cls, docs, config
    ) -> FormulaLatexIndex | None:
        """从 provider IR + IL 文档构建索引（页高用 IL cropbox）。"""
        from babeldoc.format.pdf.document_il.midend.inline_math_protector import (
            load_provider_document,
        )
        from babeldoc.format.pdf.document_il.utils.provider_alignment import to_il_box

        provider = load_provider_document(
            getattr(config, "provider_ir_dir", None),
            getattr(config, "working_dir", None),
        )
        if provider is None:
            return None
        page_heights: dict[int, float] = {}
        for page in docs.page:
            if page.cropbox is not None and page.cropbox.box is not None:
                box = page.cropbox.box
                page_heights[page.page_number] = float(box.y2) - float(box.y)
        spans_by_page: dict[int, list[tuple[tuple, str]]] = {}
        for page in provider.pages:
            height = page_heights.get(page.page_index)
            if height is None or height <= 0:
                continue
            entries = spans_by_page.setdefault(page.page_index, [])
            for block in page.iter_blocks(recursive=True):
                for line in block.lines:
                    for span in line.spans:
                        if span.kind != "inline_equation":
                            continue
                        latex = (span.content or "").strip()
                        box = to_il_box(span.bbox, height)
                        if latex and box is not None:
                            entries.append(((box.x, box.y, box.x2, box.y2), latex))
        return cls(spans_by_page)

    def lookup(
        self, page_index: int, box: tuple[float, float, float, float]
    ) -> str | None:
        entries = self._spans_by_page.get(page_index)
        if not entries:
            return None
        x0, y0, x1, y1 = box
        area = max(0.0, x1 - x0) * max(0.0, y1 - y0)
        if area <= 0:
            return None
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        best = None
        best_score = 0.0
        for span_box, latex in entries:
            sx0, sy0, sx1, sy1 = span_box
            ix0, iy0 = max(x0, sx0), max(y0, sy0)
            ix1, iy1 = min(x1, sx1), min(y1, sy1)
            inter = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
            span_area = max(0.0, sx1 - sx0) * max(0.0, sy1 - sy0)
            union = area + span_area - inter
            score = inter / union if union > 0 else 0.0
            if score < _MIN_IOU and sx0 <= cx <= sx1 and sy0 <= cy <= sy1:
                score = _MIN_IOU
            if score > best_score:
                best_score = score
                best = latex
        if best is not None and best_score >= _MIN_IOU:
            return best
        return None


def fuse_paragraph(paragraph, page_index: int, page_font_map: dict, latex_index: FormulaLatexIndex | None) -> FuseResult:
    """把 ``paragraph.unicode`` 融合为 LaTeX body（Typesetting 之前调用）。

    顺序契约：译文 token 流（text/styled/formula）与 composition 单元流
    （text/formula）一一对应；用归一化全文拼接校验，不一致即回退。
    """
    result = FuseResult()
    text = paragraph.unicode or ""
    if not text.strip():
        result.reasons.append("empty-text")
        return result

    segments = parse_segments(text)
    if segments is None:
        result.reasons.append("unbalanced-style-markup")
        return result
    if not any(kind == "formula" for kind, _ in segments):
        # 纯文本段落：无公式需融合，但仍必须解析富文本标记（不能把
        # <style id='N'> 原样交给 XeLaTeX）。
        if any(kind == "styled" for kind, _ in segments):
            units = list(iter_composition_units(paragraph))
            text_units = [u for u in units if u[0] == "text"]
            text_segments = [s for s in segments if s[0] in ("text", "styled")]
            joined_units = _normalize_for_compare("".join(t for _, t, _ in text_units))
            joined_segments = _normalize_for_compare(
                "".join(t for _, t in text_segments)
            )
            if joined_units != joined_segments:
                result.reasons.append("segment-order-mismatch")
                return result
            body_parts: list[str] = []
            text_iter = iter(text_units)
            for kind, content in segments:
                if kind == "styled":
                    result.style_runs += 1
                    _, _unit_text, unit_style = next(text_iter)
                    bold, italic = _style_flags(unit_style, page_font_map)
                    body_parts.append(
                        _style_command(bold, italic) % escape_latex(content)
                    )
                else:
                    next(text_iter)
                    body_parts.append(escape_latex(content))
            body = "".join(body_parts).strip()
            if not body:
                result.reasons.append("empty-body")
                return result
            result.ok = True
            result.body = body
            result.plain_text = "".join(
                content for kind, content in segments if kind != "formula"
            )
            return result
        result.ok = True
        result.body = escape_latex(text)
        result.plain_text = text
        return result

    if latex_index is None:
        result.reasons.append("no-provider-ir")
        return result

    units = list(iter_composition_units(paragraph))
    formula_units = [u for u in units if u[0] == "formula"]
    text_units = [u for u in units if u[0] == "text"]
    formula_segments = [s for s in segments if s[0] == "formula"]
    text_segments = [s for s in segments if s[0] in ("text", "styled")]
    result.formula_count = len(formula_segments)

    if len(formula_segments) != len(formula_units):
        result.reasons.append(
            f"placeholder-formula-mismatch({len(formula_segments)}vs{len(formula_units)})"
        )
        return result
    # 全文拼接校验：composition 文本单元拼接 == 译文去掉标记后的拼接。
    joined_units = _normalize_for_compare("".join(t for _, t, _ in text_units))
    joined_segments = _normalize_for_compare("".join(t for _, t in text_segments))
    if joined_units != joined_segments:
        result.reasons.append("segment-order-mismatch")
        return result

    body_parts: list[str] = []
    text_iter = iter(text_units)
    formula_iter = iter(formula_units)
    matched = 0
    for kind, content in segments:
        if kind == "formula":
            _, formula, _ = next(formula_iter)
            box = formula.box
            if box is None or box.x is None:
                result.reasons.append(f"formula-box-missing:v{content}")
                return result
            latex = latex_index.lookup(
                page_index, (float(box.x), float(box.y), float(box.x2), float(box.y2))
            )
            if not latex:
                result.reasons.append(f"formula-latex-unmatched:v{content}")
                return result
            body_parts.append(f"${latex}$")
            matched += 1
        else:
            _, unit_text, unit_style = next(text_iter)
            if kind == "styled":
                result.style_runs += 1
                bold, italic = _style_flags(unit_style, page_font_map)
                body_parts.append(_style_command(bold, italic) % escape_latex(content))
            else:
                body_parts.append(escape_latex(content))

    result.formulas_matched = matched
    body = "".join(body_parts).strip()
    if not body:
        result.reasons.append("empty-body")
        return result
    # 兜底：绝不允许未处理的 {vN}、裸标记或 LaTeX 控制序列漏进 XeLaTeX。
    if FORMULA_PLACEHOLDER.search(body):
        result.reasons.append("unresolved-placeholder-in-body")
        return result
    if "<style" in body or re.search(r"</?[biue]", body, re.IGNORECASE):
        result.reasons.append("unhandled-markup-in-body")
        return result
    result.ok = True
    result.body = body
    result.plain_text = "".join(
        content for kind, content in segments if kind != "formula"
    )
    return result
