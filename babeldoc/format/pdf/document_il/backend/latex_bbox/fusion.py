"""译文文本与 MinerU 公式的 LaTeX 融合（Typesetting 之前执行）。

对应架构原则 2：译文中的公式以 ``{vN}`` 占位符存在，公式源码不在译文里。
本模块在 **Typesetting 之前**（composition 仍由 ``parse_translate_output``
按译文顺序回填、公式 box 仍是源坐标）把 ``paragraph.unicode`` 转成可安全
交给 XeLaTeX 的 body：

1. prose 逐字符 LaTeX 转义（``& % $ # _ { } ~ ^ \\`` 等特殊字符）；
2. ``<style id='N'>…</style>`` / ``<bN>…</bN>`` 富文本标记按 composition
   中对应样式 run 解析为 ``\\textbf{}``/``\\textit{}``；
3. ``{vN}`` 按出现顺序对应 composition 中第 N 个 ``PdfFormula`` 对象
   （parse_translate_output 按译文顺序回填公式），按四级分类处理
   （``classify_formula``，顺序 ``text → mineru → simple_math → fragment``）：

   - ``text``：原生字符全是普通文本字符且无矢量图形 → 按字面文本入 body
     （BabelDOC 启发式把引文号 ``[55]``、项目符号 ``•`` 聚成公式的场景）；
   - ``mineru``：公式字符落在**单个**受保护 formula 区域内，该区域由
     protector 从 MinerU ``inline_equation`` span 原样转换而来（精确盒同一性），
     且 span 文本与原生字符一致、未被其它公式复用 → 用 MinerU LaTeX 源码
     包裹 ``$...$``；
   - ``simple_math``：原生字符是可转写的 Unicode 数学（``unicode_math``）
     → 转写成 ``$...$``；
   - ``fragment``：其余 → 记录源 PDF 裁片段引用，由 overlay 裁区域嵌入
     （``\bdocfrag{...}``），绝不猜测公式。

4. 任何环节无法建立一一对应（占位符数量不符、顺序校验失败）→ 返回"不可替换"，
   走现有 ``PdfFormula`` 矢量路径，绝不猜测公式。
"""

from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from dataclasses import field

from babeldoc.format.pdf.document_il.backend.latex_bbox.unicode_math import (
    CID_PLACEHOLDER,
)
from babeldoc.format.pdf.document_il.backend.latex_bbox.unicode_math import math_latex
from babeldoc.format.pdf.document_il.utils.provider_alignment import (
    span_text_consistent,
)

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

#: ``text`` 级允许的原生字符（普通正文/引文号/项目符号等，含空格）。
TEXT_CLASS_CHARS = frozenset("[]()•·_-–—,.;:+=<>/%*| ")
#: 形如纯英文单词片段的原生字符（≥3 个连续字母）→ 判 ``text``（BabelDOC 把
#: 正文词片误聚成公式的场景，如 MinerU 把 ``ng stora`` 误判成行内公式）。
PROSE_WORD_FRAGMENT = re.compile(r"^(?=.*[A-Za-z]{3})[A-Za-z]+(?: [A-Za-z]+)*$")
#: 片段占位符：overlay 在编译前替换成 ``\raisebox{...}{\includegraphics...}``。
FRAGMENT_MACRO = "\\bdocfrag"
#: MinerU LaTeX 去命令后与原生字符的字母数字相容阈值（子序列占比）。
MIN_LATEX_COMPATIBILITY = 0.6


def escape_latex(text: str) -> str:
    """把 prose 转义成 LaTeX 字面文本（不会进入数学模式）。"""
    return "".join(LATEX_SPECIALS.get(ch, ch) for ch in text)


@dataclass(slots=True)
class FragmentRef:
    """一个需要从源 PDF 裁区域嵌入的公式片段。"""

    key: str
    page: int
    #: IL 坐标 box (x, y, x2, y2)。
    box: tuple[float, float, float, float]
    #: 源字号下的竖直基线偏移（``PdfFormula.y_offset``，IL 单位 pt）。
    y_offset: float
    height: float


@dataclass(slots=True)
class FormulaClassification:
    """一个 ``PdfFormula`` 单元的分类结果。"""

    kind: str
    native_text: str = ""
    #: ``mineru``/``simple_math`` 的数学模式片段（不含 ``$``）。
    latex: str = ""
    #: 命中的 MinerU span id（``mineru`` 级）。
    span_id: str | None = None
    fragment: FragmentRef | None = None
    #: 降级原因（``mineru`` 级失败时的证据，供报告留痕）。
    reason: str = ""


@dataclass(slots=True)
class FuseResult:
    """一个段落的融合结果。"""

    ok: bool = False
    body: str = ""
    #: 期望可见文本（去标记 + text/mineru/simple_math 片段原文，fragment 除外），
    #: 用于检测贴片后是否丢字。
    plain_text: str = ""
    formula_count: int = 0
    formulas_matched: int = 0
    #: 逐公式分类（与 ``formula_count`` 同序）。
    formula_classes: list[str] = field(default_factory=list)
    #: 需要裁片段嵌入的引用（body 内已写入 ``\\bdocfrag{key}``）。
    fragments: list[FragmentRef] = field(default_factory=list)
    #: 降级/分类留痕（``kind:reason``），供 overlay 报告聚合。
    formula_notes: list[str] = field(default_factory=list)
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


@dataclass(slots=True)
class ProtectedFormula:
    """一个 IL formula 保护区与其 MinerU ``inline_equation`` span 的对应。"""

    span_id: str
    latex: str
    text: str


def _box_key(box) -> tuple[float, float, float, float]:
    return (
        round(float(box.x), 3),
        round(float(box.y), 3),
        round(float(box.x2), 3),
        round(float(box.y2), 3),
    )


def _alnum(text: str) -> str:
    """去掉 LaTeX 命令与花括号后保留字母数字（用于跨表示比对）。"""
    stripped = re.sub(r"\\[A-Za-z]+", " ", text or "")
    normalized = unicodedata.normalize("NFKC", stripped)
    return "".join(ch for ch in normalized if ch.isalnum()).lower()


def _subsequence_ratio(native: str, latex: str) -> float:
    """``latex`` 的字母数字在 ``native`` 里的**有序**子序列覆盖率（0..1）。

    用 ``difflib`` 匹配块长度之和度量：顺序错乱（如 OCR 把正文词认成公式）
    会显著拉低该值。
    """
    import difflib

    if not native:
        return 0.0
    matcher = difflib.SequenceMatcher(a=native, b=latex, autojunk=False)
    return sum(block.size for block in matcher.get_matching_blocks()) / len(native)


def _latex_compatible(native_text: str, latex: str) -> bool:
    """MinerU LaTeX 去命令后的字母数字与原生字符是否相容。"""
    native = _alnum(native_text)
    if not native:
        return False
    candidate = _alnum(latex)
    if not candidate:
        return False
    return _subsequence_ratio(native, candidate) >= MIN_LATEX_COMPATIBILITY


class FormulaLatexIndex:
    """IL formula 保护区 → MinerU ``inline_equation`` span 的索引。

    匹配是**精确盒同一性**：``inline_math_protector`` 把 span 的 IL bbox 原样
    追加为 ``class_name="formula"`` 的 ``PageLayout``，因此公式字符的
    ``formula_layout_id`` 所指区域的 box 与该 span 的 box 完全相等。同一 box
    命中多个 span（MinerU 重复产物）视为不确定，该区域不做 MinerU 级融合。

    与旧的 IoU/中心点就近匹配不同：这里不做任何几何近似，宁可降级
    （simple_math / fragment）也不用可能属于相邻区域的 LaTeX —— 根因 3
    的 B2Rrm 错误正是 IoU 就近取造成的。
    """

    def __init__(self, by_page: dict[int, dict[int, ProtectedFormula]]):
        self._by_page = by_page

    @property
    def total_spans(self) -> int:
        return sum(len(entries) for entries in self._by_page.values())

    @classmethod
    def from_documents(cls, docs, config) -> FormulaLatexIndex | None:
        """从 provider IR + IL 文档的 formula 保护区构建索引。

        页高用 IL cropbox；无 provider IR（native 路径）时返回 None。
        """
        from babeldoc.format.pdf.document_il.midend.inline_math_protector import (
            load_provider_document,
        )
        from babeldoc.format.pdf.document_il.utils.provider_alignment import (
            inline_equation_spans,
        )

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
        provider_pages = {page.page_index: page for page in provider.pages}
        by_page: dict[int, dict[int, ProtectedFormula]] = {}
        for page in docs.page:
            provider_page = provider_pages.get(page.page_number)
            height = page_heights.get(page.page_number)
            if provider_page is None or height is None or height <= 0:
                continue
            spans_by_box: dict[tuple, list] = {}
            for span, box in inline_equation_spans(provider_page, height):
                spans_by_box.setdefault(_box_key(box), []).append(span)
            mapping: dict[int, ProtectedFormula] = {}
            for layout in page.page_layout or []:
                if layout.class_name not in ("formula", "isolate_formula"):
                    continue
                if layout.box is None or layout.id is None:
                    continue
                hits = spans_by_box.get(_box_key(layout.box))
                if not hits or len(hits) != 1:
                    continue
                span = hits[0]
                latex = (span.content or "").strip()
                if not latex:
                    continue
                mapping[int(layout.id)] = ProtectedFormula(
                    span_id=span.span_id, latex=latex, text=span.content or ""
                )
            if mapping:
                by_page[page.page_number] = mapping
        return cls(by_page)

    def resolve(
        self,
        page_index: int,
        layout_ids: frozenset[int],
        used_spans: set[str],
    ) -> ProtectedFormula | None:
        """解析公式字符的 ``formula_layout_id`` 指向的保护区。

        要求：全部字符指向**同一个**保护区、该保护区对应唯一 MinerU span、
        span 未被同段其它公式复用。不满足返回 None（降级）。
        """
        if len(layout_ids) != 1:
            return None
        protected = (self._by_page.get(page_index) or {}).get(next(iter(layout_ids)))
        if protected is None:
            return None
        if protected.span_id in used_spans:
            return None
        return protected


def _native_text(formula) -> str:
    return "".join(char.char_unicode or "" for char in formula.pdf_character or [])


def _layout_ids(formula) -> frozenset[int]:
    return frozenset(
        char.formula_layout_id
        for char in formula.pdf_character or []
        if char.formula_layout_id
    )


def _is_text_native(native: str) -> bool:
    """原生字符是否全是普通文本字符（``text`` 级判定）。

    源解析器对未映射字形会给出 ``(cid:N)`` 占位串；它由纯 ASCII 组成，
    若不显式拒绝会被当正文字面写进成品 PDF。含此形态一律降级 fragment
    （裁源区域反而能画出正确字形）。
    """
    if CID_PLACEHOLDER.search(native):
        return False
    for ch in native:
        if ch.isspace() or ch in TEXT_CLASS_CHARS:
            continue
        if ch.isascii() and ch.isalnum():
            continue
        return False
    return True


def _is_prose_word_fragment(native: str) -> bool:
    """原生字符是否是纯英文单词片段（正文词被误聚成公式）。"""
    return bool(PROSE_WORD_FRAGMENT.match(native.strip()))


def classify_formula(
    formula,
    page_index: int,
    latex_index: FormulaLatexIndex | None,
    used_spans: set[str] | None = None,
    fragment_key: str = "",
) -> FormulaClassification:
    """把 composition 里的一个 ``PdfFormula`` 单元分级。

    顺序硬约束：``text → mineru → simple_math → fragment``；任何不确定即降级。
    """
    native = _native_text(formula)
    has_graphics = bool(formula.pdf_curve) or bool(formula.pdf_form)
    if not has_graphics and _is_text_native(native):
        return FormulaClassification(kind="text", native_text=native)
    if _is_prose_word_fragment(native):
        # 纯英文词片：无论是否带矢量图形都按文本处理（B2Rrm 类误判）。
        return FormulaClassification(
            kind="text", native_text=native, reason="prose-word-fragment"
        )
    if latex_index is not None:
        protected = latex_index.resolve(
            page_index, _layout_ids(formula), used_spans if used_spans is not None else set()
        )
        if protected is not None:
            if not native:
                return FormulaClassification(
                    kind="fragment",
                    native_text=native,
                    reason="mineru-empty-native",
                    fragment=_fragment(formula, page_index, fragment_key),
                )
            if not span_text_consistent(protected.text, native):
                return FormulaClassification(
                    kind="fragment",
                    native_text=native,
                    span_id=protected.span_id,
                    reason="mineru-span-inconsistent",
                    fragment=_fragment(formula, page_index, fragment_key),
                )
            if not _latex_compatible(native, protected.latex):
                return FormulaClassification(
                    kind="fragment",
                    native_text=native,
                    span_id=protected.span_id,
                    reason="mineru-latex-incompatible",
                    fragment=_fragment(formula, page_index, fragment_key),
                )
            return FormulaClassification(
                kind="mineru",
                native_text=native,
                latex=protected.latex,
                span_id=protected.span_id,
            )
    transliterated = math_latex(native)
    if transliterated:
        return FormulaClassification(
            kind="simple_math", native_text=native, latex=transliterated
        )
    return FormulaClassification(
        kind="fragment",
        native_text=native,
        reason="untranslatable-native",
        fragment=_fragment(formula, page_index, fragment_key),
    )


#: fragment 占位符的正则（overlay 用它在编译前替换成内联图片）。
FRAGMENT_PATTERN = re.compile(re.escape(FRAGMENT_MACRO) + r"\{([^{}]+)\}")


def render_fragment_latex(ref: FragmentRef, path: str) -> str:
    """把片段引用渲染成内联图片 LaTeX（编译前由 overlay 调用）。

    与源文字号等高、按 ``PdfFormula.y_offset`` 竖直对齐基线；``bp`` 与
    bbox/stamp 同单位（架构：P1-3 单位统一）。
    """
    return (
        f"\\raisebox{{{ref.y_offset:.4f}bp}}{{"
        f"\\includegraphics[height={ref.height:.4f}bp]{{{path}}}}}"
    )


def _fragment(formula, page_index: int, key: str) -> FragmentRef | None:
    box = formula.box
    if box is None or None in (box.x, box.y, box.x2, box.y2):
        return None
    return FragmentRef(
        key=key,
        page=page_index,
        box=(float(box.x), float(box.y), float(box.x2), float(box.y2)),
        y_offset=float(formula.y_offset or 0.0),
        height=float(box.y2) - float(box.y),
    )


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
    plain_parts: list[str] = []
    text_iter = iter(text_units)
    formula_iter = iter(formula_units)
    matched = 0
    formula_index = 0
    used_spans: set[str] = set()
    for kind, content in segments:
        if kind == "formula":
            _, formula, _ = next(formula_iter)
            classification = classify_formula(
                formula,
                page_index,
                latex_index,
                used_spans,
                fragment_key=f"{paragraph.debug_id or 'para'}-{formula_index}",
            )
            formula_index += 1
            result.formula_classes.append(classification.kind)
            if classification.span_id:
                used_spans.add(classification.span_id)
            if classification.reason:
                result.formula_notes.append(
                    f"{classification.kind}:{classification.reason}"
                )
            if classification.kind == "text":
                # 启发式「公式」其实就是文本：按字面入 body（已转义）。
                body_parts.append(escape_latex(classification.native_text))
                plain_parts.append(classification.native_text)
            elif classification.kind in ("mineru", "simple_math"):
                body_parts.append(f"${classification.latex}$")
                plain_parts.append(classification.native_text)
                matched += 1
            else:
                fragment = classification.fragment
                if fragment is None:
                    result.reasons.append(f"fragment-box-missing:v{content}")
                    return result
                result.fragments.append(fragment)
                body_parts.append(f"{FRAGMENT_MACRO}{{{fragment.key}}}")
        else:
            _, unit_text, unit_style = next(text_iter)
            plain_parts.append(content)
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
    if FRAGMENT_MACRO not in body and result.fragments:
        result.reasons.append("fragment-macro-missing")
        return result
    result.ok = True
    result.body = body
    # 期望可见文本：译文（去标记）+ text/mineru/simple_math 片段原文；
    # fragment 是位图区域，文本抽取不到，故不纳入比较（P2-6）。
    result.plain_text = "".join(plain_parts)
    return result
