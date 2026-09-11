"""目录页与目录条目识别：把印刷目录（printed TOC）拆成条目级翻译单元。

背景（对应 .plan/minerU深度融合.md 板块 4）：目录页在 MinerU 里只是一个大的
``text`` block，``ParagraphFinder`` 里「连续 20 个点 → 切段」的既有逻辑虽然把
条目切成了独立的行 composition，但**整页条目仍是一个段落**：所有条目连同点引导
线、页码一起进入同一个翻译单元。译文因此无法保持「一条目一行、页码右对齐」的
结构，点引导线与页码会被当成正文翻译或错位。

本模块做两件事：

1. **页级判定**：标题命中 ``Contents`` / ``Table of Contents`` / ``目录``，或
   页内「行尾右对齐数字页码」的条目行占比 ≥ 0.6 且条数 ≥ 5 → 判定为目录页；
2. **条目切分与建模**：每条目录行切成 ``heading``（可翻译）/ ``leader``（点引导线）/
   ``page``（印刷页码）三部分，产出两个段落：
   - ``toc_entry``：标题段落（独立翻译单元，box 覆盖标题区域），
   - ``toc_entry_page``：页码段落（受保护，点引导线 + 页码字符原样 passthrough，
     因此页码位置与原文完全一致）。

条目总数、顺序、印刷页码、层级、缩进全部由程序保持；置信度低于阈值时不改结构，
只记录 ``toc_low_confidence``，避免误伤正常正文页。

插入位置：``ParagraphFinder`` 之后、``StylesAndFormulas`` 之前（需要段落结构，
且新建段落要经过样式/公式处理）。两条管线（``markdown_view._run_parse`` /
``workflow.extract``）都插入同一位置。
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path

from babeldoc.format.pdf.document_il import il_version_1
from babeldoc.format.pdf.document_il.midend.paragraph_finder import generate_base58_id
from babeldoc.format.pdf.document_il.utils.formular_helper import update_formula_data
from babeldoc.format.pdf.document_il.utils.layout_helper import get_char_unicode_string

logger = logging.getLogger(__name__)

# 目录页标题（``Contents`` / ``Table of Contents`` / ``目录``，允许中间空格）
TOC_TITLE_RE = re.compile(r"^\s*(?:contents|table\s+of\s+contents|目\s*录)\s*$", re.I)
# 条目页判定阈值
MIN_ENTRY_LINES = 5
ENTRY_RATIO_THRESHOLD = 0.6
# 条目切分置信度阈值（低于此值不做条目化）
CONFIDENCE_THRESHOLD = 0.6
# 印刷页码右边缘必须落在「页内文本右边界」内多少 pt 之内，才算目录页码
PAGE_LABEL_RIGHT_MARGIN_GAP = 8.0
# 判定「引导线」用的点号
_LEADER_CHARS = (".", " ")
# 编号模式：``1`` / ``2.1`` / ``B.2``
_NUMBER_RE = re.compile(r"^(\d+(?:\.\d+)*)")
_LETTER_RE = re.compile(r"^([A-Z])(?:\.(\d+(?:\.\d+)*))?")
# 无编号目录条目的缩进层级聚类容差（pt）
_INDENT_TOLERANCE = 2.0
# 审计产物（落在 ``<agent>/source/toc.json``）
TOC_ARTIFACT = "toc.json"


@dataclass(slots=True)
class TocEntry:
    """一条目录条目：标题 + 点引导线 + 印刷页码，三者字符全部保留。"""

    entry_index: int
    heading_text: str
    printed_page_label: str
    leader_kind: str  # dots | spaces | none
    indent_x0: float
    level: int
    heading_chars: list[il_version_1.PdfCharacter] = field(default_factory=list)
    leader_chars: list[il_version_1.PdfCharacter] = field(default_factory=list)
    page_chars: list[il_version_1.PdfCharacter] = field(default_factory=list)
    line_box: il_version_1.Box | None = None

    @property
    def leader_start_x(self) -> float | None:
        """引导线起点 x（= 标题区域右边界）；无引导线时为 None。"""
        if self.leader_chars:
            return min(char.visual_bbox.box.x for char in self.leader_chars)
        if self.page_chars:
            return min(char.visual_bbox.box.x for char in self.page_chars)
        return None

    def to_dict(self) -> dict:
        return {
            "entry_index": self.entry_index,
            "heading_text": self.heading_text,
            "printed_page_label": self.printed_page_label,
            "leader_kind": self.leader_kind,
            "indent_x0": round(self.indent_x0, 3),
            "level": self.level,
        }


@dataclass(slots=True)
class TocPageInfo:
    """一页的目录判定结果。"""

    page_index: int
    entries: list[TocEntry] = field(default_factory=list)
    candidate_lines: int = 0
    total_lines: int = 0
    title_heading: str | None = None
    low_confidence: bool = False

    @property
    def confidence(self) -> float:
        """条目切分置信度 = 已解析条目行 / 页内文本行。"""
        if self.total_lines <= 0:
            return 0.0
        return len(self.entries) / self.total_lines

    @property
    def is_toc(self) -> bool:
        return bool(self.entries) or self.title_heading is not None

    def to_dict(self) -> dict:
        return {
            "page_index": self.page_index,
            "is_toc": self.is_toc,
            "confidence": round(self.confidence, 4),
            "low_confidence": self.low_confidence,
            "candidate_lines": self.candidate_lines,
            "total_lines": self.total_lines,
            "title_heading": self.title_heading,
            "entries": [entry.to_dict() for entry in self.entries],
        }


# --------------------------------------------------------------------------- #
# 行切分
# --------------------------------------------------------------------------- #
@dataclass(slots=True)
class _SplitLine:
    heading_chars: list[il_version_1.PdfCharacter]
    leader_chars: list[il_version_1.PdfCharacter]
    page_chars: list[il_version_1.PdfCharacter]


def _box_x2(char: il_version_1.PdfCharacter) -> float:
    return float(char.visual_bbox.box.x2)


def _box_x(char: il_version_1.PdfCharacter) -> float:
    return float(char.visual_bbox.box.x)


def split_toc_line(
    chars: list[il_version_1.PdfCharacter],
    right_margin: float,
) -> _SplitLine | None:
    """把一行字符切成 (标题, 引导线, 页码)；不像目录条目时返回 None。

    判定顺序：
    1. 去掉行尾空白；
    2. 尾部数字 run 作为印刷页码，其右边缘必须贴近页内文本右边界
       （目录页码右对齐，正文行里偶然出现的数字通常不在边界上）；
    3. 向前吞掉 ``.`` / `` `` 作为引导线；
    4. 剩余部分必须是含字母、长度 ≥ 3 的标题。
    """
    body = list(chars)
    while body and not (body[-1].char_unicode or "").strip():
        body.pop()
    if len(body) < 3:
        return None

    index = len(body)
    while index > 0 and (body[index - 1].char_unicode or "").isdigit():
        index -= 1
    if index == len(body):  # 行尾不是数字 → 没有印刷页码
        return None
    page_chars = body[index:]
    if _box_x2(page_chars[-1]) < right_margin - PAGE_LABEL_RIGHT_MARGIN_GAP:
        return None

    leader_start = index
    while leader_start > 0 and (body[leader_start - 1].char_unicode or "") in _LEADER_CHARS:
        leader_start -= 1
    heading_chars = body[:leader_start]
    if len(heading_chars) < 3:
        return None
    heading_text = get_char_unicode_string(heading_chars)
    if not re.search(r"[A-Za-z]", heading_text):
        return None

    return _SplitLine(
        heading_chars=heading_chars,
        leader_chars=body[leader_start:index],
        page_chars=page_chars,
    )


def _leader_kind(leader_chars: list[il_version_1.PdfCharacter]) -> str:
    if not leader_chars:
        return "none"
    if any((char.char_unicode or "") == "." for char in leader_chars):
        return "dots"
    return "spaces"


def _entry_level(heading_text: str, indent_x0: float, indents: list[float]) -> int:
    """层级推断：优先编号模式，其次缩进聚类。"""
    number = _NUMBER_RE.match(heading_text)
    if number:
        return number.group(1).count(".") + 1
    letter = _LETTER_RE.match(heading_text)
    if letter:
        tail = letter.group(2)
        return 1 + (tail.count(".") + 1 if tail else 0)
    # 无编号：按缩进聚类（同级缩进相同）
    level = 1
    for indent in sorted({round(value, 1) for value in indents}):
        if indent < indent_x0 - _INDENT_TOLERANCE:
            level += 1
    return level


# --------------------------------------------------------------------------- #
# 页级检测
# --------------------------------------------------------------------------- #
def _iter_paragraph_lines(page: il_version_1.Page):
    """产出 (段落, composition 下标, 字符列表, 行 box)；只取 ``pdf_line``。"""
    for paragraph in page.pdf_paragraph:
        for index, composition in enumerate(paragraph.pdf_paragraph_composition):
            if composition.pdf_line and composition.pdf_line.pdf_character:
                yield (
                    paragraph,
                    index,
                    composition.pdf_line.pdf_character,
                    composition.pdf_line.box,
                )


def _page_right_margin(lines) -> float:
    xs = [float(line_box.x2) for _, _, _, line_box in lines if line_box]
    return max(xs) if xs else 0.0


def _toc_title(page: il_version_1.Page) -> str | None:
    """页面标题命中目录标题时返回标题文本。"""
    for paragraph in page.pdf_paragraph:
        text = (paragraph.unicode or "").strip()
        if text and TOC_TITLE_RE.match(text):
            return text
    return None


def detect_toc_page(page: il_version_1.Page) -> TocPageInfo:
    """判定一页是否为目录页，并切出条目。"""
    info = TocPageInfo(page_index=page.page_number)
    # 页脚页码等已由选择层排除，这里不额外过滤
    lines = list(_iter_paragraph_lines(page))
    info.total_lines = len(lines)
    info.title_heading = _toc_title(page)
    if not lines:
        info.low_confidence = bool(info.title_heading)
        return info

    right_margin = _page_right_margin(lines)
    splits: list[tuple] = []
    for paragraph, index, chars, line_box in lines:
        split = split_toc_line(list(chars), right_margin)
        if split is not None:
            splits.append((paragraph, index, split, line_box))
    info.candidate_lines = len(splits)

    ratio = len(splits) / info.total_lines if info.total_lines else 0.0
    enough_entries = len(splits) >= MIN_ENTRY_LINES and ratio >= ENTRY_RATIO_THRESHOLD
    if not (info.title_heading or enough_entries):
        return info
    if ratio < CONFIDENCE_THRESHOLD:
        # 判为目录页但条目切分不可靠：不改结构，只记低置信度。
        info.low_confidence = True
        return info

    indents = [
        round(_box_x(split.heading_chars[0]), 3) for _, _, split, _ in splits
    ]
    for entry_index, (_, _, split, line_box) in enumerate(splits, start=1):
        heading_text = get_char_unicode_string(split.heading_chars)
        indent_x0 = round(_box_x(split.heading_chars[0]), 3)
        info.entries.append(
            TocEntry(
                entry_index=entry_index,
                heading_text=heading_text,
                printed_page_label="".join(
                    char.char_unicode or "" for char in split.page_chars
                ),
                leader_kind=_leader_kind(split.leader_chars),
                indent_x0=indent_x0,
                level=_entry_level(heading_text, indent_x0, indents),
                heading_chars=split.heading_chars,
                leader_chars=split.leader_chars,
                page_chars=split.page_chars,
                line_box=line_box,
            )
        )
    return info


def detect_toc_pages(docs: il_version_1.Document) -> list[TocPageInfo]:
    """整篇检测；只返回 ``is_toc`` 或低置信度的页。"""
    infos = []
    for page in docs.page:
        info = detect_toc_page(page)
        if info.is_toc or info.low_confidence:
            infos.append(info)
    return infos


# --------------------------------------------------------------------------- #
# 条目化改写
# --------------------------------------------------------------------------- #
def _formula_marker_id(page: il_version_1.Page) -> int:
    """给受保护字符用的 ``formula_layout_id`` 哨兵值。

    ``StylesAndFormulas.process_translatable_formulas`` 会把「无 formula_layout_id
    且只含数字/逗号/空格/点」的公式转回普通文本；目录的点引导线与页码正是这种
    形状。这里给它们一个非零标记，保证公式身份被保留（真源是源字符本身，标记只
    用于拒绝降级，不新增布局区域）。
    """
    return max((layout.id or 0 for layout in page.page_layout or ()), default=0) + 1


def _paragraph_box_from_chars(
    chars: list[il_version_1.PdfCharacter],
    y0: float | None = None,
    y1: float | None = None,
    x2: float | None = None,
) -> il_version_1.Box:
    boxes = [char.visual_bbox.box for char in chars]
    return il_version_1.Box(
        x=min(box.x for box in boxes),
        y=y0 if y0 is not None else min(box.y for box in boxes),
        x2=x2 if x2 is not None else max(box.x2 for box in boxes),
        y2=y1 if y1 is not None else max(box.y2 for box in boxes),
    )


def build_entry_paragraphs(
    page: il_version_1.Page,  # noqa: ARG001 - 保持 midend 签名对称，预留给页级缓存
    entry: TocEntry,
    marker_id: int,
) -> list[il_version_1.PdfParagraph]:
    """一条目录条目 → [标题段落, 页码段落]。"""
    heading_style = entry.heading_chars[0].pdf_style
    line_y0 = entry.line_box.y if entry.line_box else None
    line_y1 = entry.line_box.y2 if entry.line_box else None

    # 标题段落：box 右边界取引导线起点（避免译文压到点引导线上），
    # 可用宽度过窄时退回整行 box（此时依赖排版缩放兜底）。
    heading_x2 = entry.leader_start_x
    if heading_x2 is None or heading_x2 - entry.indent_x0 < 60:
        heading_x2 = entry.line_box.x2 if entry.line_box else None
    heading_paragraph = il_version_1.PdfParagraph(
        box=_paragraph_box_from_chars(
            entry.heading_chars, y0=line_y0, y1=line_y1, x2=heading_x2
        ),
        pdf_style=heading_style,
        pdf_paragraph_composition=[
            il_version_1.PdfParagraphComposition(
                pdf_line=il_version_1.PdfLine(
                    pdf_character=list(entry.heading_chars)
                )
            )
        ],
        unicode=entry.heading_text,
        vertical=False,
        first_line_indent=False,
        debug_id=generate_base58_id(),
        layout_label="toc_entry",
        xobj_id=entry.heading_chars[0].xobj_id,
    )

    page_chars = list(entry.leader_chars) + list(entry.page_chars)
    if not page_chars:
        return [heading_paragraph]

    # 点引导线 + 页码保持原字符（含位置），作为受保护公式原样 passthrough。
    marker = marker_id
    for char in page_chars:
        char.formula_layout_id = marker
    formula = il_version_1.PdfFormula(
        pdf_character=page_chars,
        line_id=None,
        x_offset=0,
        y_offset=0,
        x_advance=0,
    )
    update_formula_data(formula)
    page_paragraph = il_version_1.PdfParagraph(
        box=_paragraph_box_from_chars(page_chars, y0=line_y0, y1=line_y1),
        pdf_style=page_chars[0].pdf_style,
        pdf_paragraph_composition=[
            il_version_1.PdfParagraphComposition(pdf_formula=formula)
        ],
        unicode=entry.printed_page_label,
        vertical=False,
        first_line_indent=False,
        debug_id=generate_base58_id(),
        layout_label="toc_entry_page",
        xobj_id=page_chars[0].xobj_id,
    )
    return [heading_paragraph, page_paragraph]


def _line_matches_box(line_box, target) -> bool:
    return (
        line_box is not None
        and target is not None
        and abs(float(line_box.y) - float(target.y)) <= 1.5
        and abs(float(line_box.y2) - float(target.y2)) <= 1.5
    )


def apply_toc_entries(
    docs: il_version_1.Document,
    toc_pages: list[TocPageInfo],
) -> dict:
    """把目录页的条目段落替换为条目级段落。返回统计 dict。

    只替换「整段所有行都是目录条目」的段落——混合内容（正文 + 条目）不做任何
    改动并记入 ``skipped_pages``，避免误伤正文页。
    """
    stats: dict = {
        "pages": 0,
        "entries": 0,
        "replaced_paragraphs": 0,
        "skipped_pages": [],
    }
    by_page = {info.page_index: info for info in toc_pages}
    for page in docs.page:
        info = by_page.get(page.page_number)
        if info is None or not info.entries:
            continue
        # 条目 → 所属段落：条目行的 y 区间与段落内 ``pdf_line`` 的 y 区间一致。
        paragraph_entries: dict[int, list[TocEntry]] = {}
        for entry in info.entries:
            for index, paragraph in enumerate(page.pdf_paragraph):
                if any(
                    composition.pdf_line
                    and _line_matches_box(composition.pdf_line.box, entry.line_box)
                    for composition in paragraph.pdf_paragraph_composition
                ):
                    paragraph_entries.setdefault(index, []).append(entry)
                    break
        # 安全性：只在「该段所有行都是目录条目」时才替换。
        replaceable: dict[int, list[TocEntry]] = {}
        for index, entries in paragraph_entries.items():
            paragraph = page.pdf_paragraph[index]
            line_count = sum(
                1
                for composition in paragraph.pdf_paragraph_composition
                if composition.pdf_line
            )
            if len(entries) == line_count:
                replaceable[index] = entries
        if not replaceable:
            stats["skipped_pages"].append(
                {"page_index": page.page_number, "reason": "no_pure_entry_paragraph"}
            )
            continue

        marker_id = _formula_marker_id(page)
        new_paragraphs: list[il_version_1.PdfParagraph] = []
        for index, paragraph in enumerate(page.pdf_paragraph):
            entries = replaceable.get(index)
            if entries is None:
                new_paragraphs.append(paragraph)
                continue
            stats["replaced_paragraphs"] += 1
            for entry in entries:
                new_paragraphs.extend(build_entry_paragraphs(page, entry, marker_id))
        page.pdf_paragraph = new_paragraphs
        stats["pages"] += 1
        stats["entries"] += sum(len(entries) for entries in replaceable.values())
    return stats


# --------------------------------------------------------------------------- #
# midend pass
# --------------------------------------------------------------------------- #
class TocDetector:
    """midend pass：目录页条目化（``ParagraphFinder`` 之后、公式样式之前）。"""

    stage_name = "Detect TOC Entries"

    def __init__(self, translate_config):
        self.translate_config = translate_config

    def process(self, docs: il_version_1.Document):
        try:
            toc_pages = detect_toc_pages(docs)
        except Exception:  # noqa: BLE001 - 目录识别是增强项，不阻断解析
            logger.warning("目录页识别失败", exc_info=True)
            return docs
        if not toc_pages:
            return docs

        stats = {"pages": 0, "entries": 0, "replaced_paragraphs": 0, "skipped_pages": []}
        try:
            stats = apply_toc_entries(docs, toc_pages)
        except Exception:  # noqa: BLE001
            logger.warning("目录条目化失败", exc_info=True)

        warnings = [
            f"toc_low_confidence: page {info.page_index} "
            f"confidence={info.confidence:.2f} candidates={info.candidate_lines}/"
            f"{info.total_lines}"
            for info in toc_pages
            if info.low_confidence
        ]
        if warnings:
            bucket = getattr(self.translate_config, "layout_warnings", None)
            if isinstance(bucket, list):
                bucket.extend(warnings)
            logger.warning("; ".join(warnings))

        self._write_audit(toc_pages, stats, warnings)
        return docs

    def _audit_dir(self) -> Path | None:
        directory = getattr(self.translate_config, "provider_ir_dir", None)
        if directory:
            return Path(directory)
        working_dir = getattr(self.translate_config, "working_dir", None)
        if working_dir:
            return Path(working_dir) / "agent"
        return None

    def _write_audit(self, toc_pages, stats, warnings) -> None:
        directory = self._audit_dir()
        if directory is None:
            return
        output_path = directory / "source" / TOC_ARTIFACT
        report = {
            "version": 1,
            "summary": {
                "toc_pages": len([info for info in toc_pages if info.entries]),
                "entries": stats.get("entries", 0),
                "replaced_paragraphs": stats.get("replaced_paragraphs", 0),
                "low_confidence_pages": [
                    info.page_index for info in toc_pages if info.low_confidence
                ],
                "warnings": warnings,
                "skipped_pages": stats.get("skipped_pages", []),
            },
            "pages": [info.to_dict() for info in toc_pages],
        }
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = output_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(output_path)
            logger.info("目录条目审计已写入: %s", output_path)
        except OSError:
            logger.warning("写入 toc.json 失败", exc_info=True)
