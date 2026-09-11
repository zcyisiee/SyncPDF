"""行内公式保护：把 MinerU ``inline_equation`` span 变成受保护的 ``formula`` 区域。

背景（对应 .plan/minerU深度融合.md 板块 2）：MinerU 的行内公式 span 是显式信号，
比字符启发式（数学字体 / 特殊符号）更可靠。做法是**零侵入**地复用既有链路：

1. 读 ``provider_ir.json``（板块 1 产物），取每页 ``inline_equation`` span 的 bbox；
2. 把 bbox 转成 IL 坐标后追加为 ``class_name="formula"`` 的 ``PageLayout``；
3. 后续 ``ParagraphFinder`` 的 ``is_character_in_formula_layout`` 自然给这些字符
   赋 ``formula_layout_id``，``StylesAndFormulas`` 自然把它们聚成 ``PdfFormula``，
   ``ILTranslator`` 自然产出 ``{vN}`` 占位符。

为什么不直接设 ``char.formula_layout_id``：``ParagraphFinder`` 会对每个字符
**重新赋值**该字段，直接设置会被覆盖。追加布局区域才是受支持的入口。

插入位置：``LayoutParser.process`` 之后、``EnclosedMarkerFixer``/``ParagraphFinder``
之前（那时 ``page.pdf_character`` 还是全量，可同时产出对齐审计）。

本模块同时负责写 ``alignment.json``（对齐审计），因为对齐与保护共用同一份
provider IR 读取与页高换算。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from babeldoc.docvision.provider_ir import ProviderDocument
from babeldoc.docvision.provider_ir import ProviderPage
from babeldoc.format.pdf.document_il import il_version_1
from babeldoc.format.pdf.document_il.utils.provider_alignment import align_page
from babeldoc.format.pdf.document_il.utils.provider_alignment import (
    inline_equation_regions,
)

logger = logging.getLogger(__name__)

# 新区域与既有 formula 区域重叠率超过该值时不再重复追加。
DUPLICATE_REGION_IOU = 0.8
# 过小的区域（宽度或高度 < 1pt）视为噪声，不追加。
MIN_REGION_SIZE = 1.0


def provider_ir_path(provider_ir_dir, working_dir) -> Path | None:
    """定位 ``provider_ir.json``：显式目录优先，其次 working_dir 下的 agent 目录。"""
    candidates: list[Path] = []
    if provider_ir_dir:
        candidates.append(Path(provider_ir_dir) / "source" / "mineru" / "provider_ir.json")
    if working_dir:
        candidates.append(
            Path(working_dir) / "agent" / "source" / "mineru" / "provider_ir.json"
        )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def load_provider_document(provider_ir_dir, working_dir) -> ProviderDocument | None:
    """读取 provider IR；不存在或损坏时返回 None（native 路径静默跳过）。"""
    path = provider_ir_path(provider_ir_dir, working_dir)
    if path is None:
        return None
    try:
        return ProviderDocument.from_json(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("读取 provider IR 失败，跳过行内公式保护: %s", exc)
        return None


def _page_height(page: il_version_1.Page) -> float:
    if page.cropbox is not None and page.cropbox.box is not None:
        return float(page.cropbox.box.y2) - float(page.cropbox.box.y)
    return 0.0


def _overlap_ratio(box, other) -> float:
    """``box`` 与 ``other`` 的交集面积 / ``box`` 面积。"""
    area = max(0.0, box.x2 - box.x) * max(0.0, box.y2 - box.y)
    if area <= 0:
        return 0.0
    x0 = max(box.x, other.x)
    y0 = max(box.y, other.y)
    x1 = min(box.x2, other.x2)
    y1 = min(box.y2, other.y2)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    return (x1 - x0) * (y1 - y0) / area


def _existing_formula_regions(page: il_version_1.Page):
    for layout in page.page_layout or ():
        if layout.class_name in ("formula", "isolate_formula") and layout.box:
            yield layout


def protect_page(
    page: il_version_1.Page,
    provider_page: ProviderPage,
) -> list[dict]:
    """把一页的 ``inline_equation`` span 追加为 formula 布局区域。

    返回新追加区域的审计明细（span_id 缺失时用坐标描述）。
    """
    page_height = _page_height(page)
    if page_height <= 0:
        return []
    existing = list(_existing_formula_regions(page))
    # 页内可能没有任何布局区域（如 MinerU 只返回被丢弃块的页），
    # ``default=0`` 保证空页也能追加保护区域而不是抛 max() 空序列错误。
    next_id = max(
        (layout.id or 0 for layout in page.page_layout or ()),
        default=0,
    ) + 1
    added: list[dict] = []

    for region in inline_equation_regions(provider_page, page_height):
        width = region.x2 - region.x
        height = region.y2 - region.y
        if width < MIN_REGION_SIZE or height < MIN_REGION_SIZE:
            continue
        if any(_overlap_ratio(region, layout.box) > DUPLICATE_REGION_IOU for layout in existing):
            continue
        box = il_version_1.Box(
            x=region.x, y=region.y, x2=region.x2, y2=region.y2
        )
        layout = il_version_1.PageLayout(
            id=next_id,
            box=box,
            conf=1.0,
            class_name="formula",
        )
        page.page_layout.append(layout)
        existing.append(layout)
        added.append(
            {
                "page_index": page.page_number,
                "layout_id": next_id,
                "box": [round(region.x, 3), round(region.y, 3), round(region.x2, 3), round(region.y2, 3)],
            }
        )
        next_id += 1
    return added


class InlineMathProtector:
    """midend pass：行内公式保护 + 原生字符对齐审计。"""

    stage_name = "Protect Inline Math"

    def __init__(self, translate_config):
        self.translate_config = translate_config

    def process(self, docs: il_version_1.Document):
        provider_document = load_provider_document(
            getattr(self.translate_config, "provider_ir_dir", None),
            getattr(self.translate_config, "working_dir", None),
        )
        if provider_document is None:
            return docs

        by_index = {page.page_index: page for page in provider_document.pages}
        protected: list[dict] = []
        report_pages = []
        for page in docs.page:
            provider_page = by_index.get(page.page_number)
            if provider_page is None:
                continue
            protected.extend(self._protect(page, provider_page))
            report_pages.append(
                align_page(page, provider_page, _page_height(page))
            )

        report = {
            "version": 1,
            "summary": self._summary(report_pages, protected),
            "pages": [item.to_dict() for item in report_pages],
            "protected_inline_math": protected,
        }
        self._write_audit(report)
        return docs

    def _protect(self, page: il_version_1.Page, provider_page: ProviderPage):
        try:
            return protect_page(page, provider_page)
        except Exception:  # noqa: BLE001 - 保护是增强项，不应中断解析
            logger.warning(
                "行内公式保护失败 page=%s", provider_page.page_index, exc_info=True
            )
            return []

    @staticmethod
    def _summary(pages, protected) -> dict:
        total = sum(page.total_chars for page in pages)
        matched = sum(page.matched_chars for page in pages)
        inline_matches = [m for page in pages for m in page.inline_matches]
        return {
            "page_count": len(pages),
            "total_chars": total,
            "matched_chars": matched,
            "unmatched_chars": sum(page.unmatched_chars for page in pages),
            "ambiguous_chars": sum(page.ambiguous_chars for page in pages),
            "native_char_coverage": round(matched / total, 6) if total else 1.0,
            "inline_equation_total": len(inline_matches),
            "inline_equation_matched": sum(1 for m in inline_matches if m.matched),
            "span_text_mismatch": sum(page.span_text_mismatch for page in pages),
            "protected_inline_math": len(protected),
        }

    def _write_audit(self, report: dict) -> None:
        directory = getattr(self.translate_config, "provider_ir_dir", None)
        if not directory:
            working_dir = getattr(self.translate_config, "working_dir", None)
            directory = Path(working_dir) / "agent" if working_dir else None
        if not directory:
            return
        output_path = Path(directory) / "source" / "mineru" / "alignment.json"
        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = output_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            tmp.replace(output_path)
            logger.info("行内公式对齐审计已写入: %s", output_path)
        except OSError:
            logger.warning("写入 alignment.json 失败", exc_info=True)
