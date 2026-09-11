import json
import logging
import math
from pathlib import Path

import cv2
import numpy as np
from pymupdf import Document

from babeldoc.format.pdf.document_il import il_version_1
from babeldoc.format.pdf.document_il.utils.style_helper import GREEN
from babeldoc.format.pdf.translation_config import TranslationConfig

logger = logging.getLogger(__name__)

LAYOUT_COVERAGE_ARTIFACT = "layout_coverage.json"
# 每页未覆盖字符在审计产物里保留的文本片段长度上限。
COVERAGE_PREVIEW_CHARS = 200


def _box_overlaps(box_a: il_version_1.Box, box_b: il_version_1.Box) -> bool:
    """两个 box 是否有正面积交（任一轴不相交则不算命中）。"""
    return (
        min(box_a.x2, box_b.x2) > max(box_a.x, box_b.x)
        and min(box_a.y2, box_b.y2) > max(box_a.y, box_b.y)
    )


def compute_layout_coverage(docs: il_version_1.Document) -> dict:
    """统计原生字符落入 layout 区域的比例（布局覆盖率门禁的判据）。

    口径：分母是该页全部原生字符（``page.pdf_character``）——必须在
    ParagraphFinder 之前统计，因为之后该列表只剩被跳过的字符。
    “命中”指字符的 visual_bbox 与任一 ``page_layout`` box 有正面积交。

    无字符页（扫描件/OCR workaround）分母为 0，coverage 记 1.0，不参与门禁。
    """
    pages = []
    total_chars = 0
    total_uncovered = 0
    for page in docs.page:
        layouts = [layout.box for layout in page.page_layout if layout.box]
        page_total = 0
        page_uncovered = 0
        uncovered_samples = []
        for char in page.pdf_character:
            char_box = char.visual_bbox.box if char.visual_bbox else char.box
            if char_box is None:
                continue
            page_total += 1
            if any(_box_overlaps(char_box, layout_box) for layout_box in layouts):
                continue
            page_uncovered += 1
            if len(uncovered_samples) < 5:
                uncovered_samples.append(
                    {
                        "box": [char_box.x, char_box.y, char_box.x2, char_box.y2],
                        "text": (char.char_unicode or "")[:8],
                    }
                )
        total_chars += page_total
        total_uncovered += page_uncovered
        pages.append(
            {
                "page_index": page.page_number,
                "total_chars": page_total,
                "uncovered_chars": page_uncovered,
                "coverage": (
                    1.0 if page_total == 0 else 1.0 - page_uncovered / page_total
                ),
                "uncovered_samples": uncovered_samples,
            }
        )
    return {
        "pages": pages,
        "global": {
            "total_chars": total_chars,
            "uncovered_chars": total_uncovered,
            "uncovered_ratio": (
                0.0 if total_chars == 0 else total_uncovered / total_chars
            ),
            "coverage": (
                1.0 if total_chars == 0 else 1.0 - total_uncovered / total_chars
            ),
        },
    }


def _uncovered_preview(docs: il_version_1.Document, report: dict) -> dict:
    """给门禁报告补每页未覆盖字符的文本片段（前 N 字符，便于人工定位）。"""
    # page_index 是 page_number，不一定等于列表下标（如 --pages 裁剪后），
    # 因此先建映射再取页。
    page_by_number = {page.page_number: page for page in docs.page}
    for page_report in report["pages"]:
        page = page_by_number.get(page_report["page_index"])
        if page is None:
            page_report["uncovered_text_preview"] = ""
            continue
        layouts = [layout.box for layout in page.page_layout if layout.box]
        preview = []
        for char in page.pdf_character:
            char_box = char.visual_bbox.box if char.visual_bbox else char.box
            if char_box is None:
                continue
            if any(_box_overlaps(char_box, layout_box) for layout_box in layouts):
                continue
            preview.append(char.char_unicode or "")
            if len(preview) >= COVERAGE_PREVIEW_CHARS:
                break
        page_report["uncovered_text_preview"] = "".join(preview)
    return report


class LayoutParser:
    stage_name = "Parse Page Layout"

    def __init__(self, translation_config: TranslationConfig):
        self.translation_config = translation_config
        self.model = translation_config.doc_layout_model

    def _save_debug_image(self, image: np.ndarray, layout, page_number: int):
        """Save debug image with drawn boxes if debug mode is enabled."""
        if not self.translation_config.debug:
            return

        debug_dir = Path(self.translation_config.get_working_file_path("ocr-box-image"))
        debug_dir.mkdir(parents=True, exist_ok=True)

        # Draw boxes on the image
        debug_image = image.copy()
        for box in layout.boxes:
            x0, y0, x1, y1 = box.xyxy
            cv2.rectangle(
                debug_image,
                (int(x0), int(y0)),
                (int(x1), int(y1)),
                (0, 255, 0),
                2,
            )
            # Add text label
            cv2.putText(
                debug_image,
                layout.names[box.cls],
                (int(x0), int(y0) - 5),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                (0, 255, 0),
                1,
            )
        img_bgr = cv2.cvtColor(debug_image, cv2.COLOR_RGB2BGR)

        # Save the image
        output_path = debug_dir / f"{page_number}.jpg"
        cv2.imwrite(str(output_path), img_bgr)

    def _save_debug_box_to_page(self, page: il_version_1.Page):
        """Save debug boxes and text labels to the PDF page."""
        if not self.translation_config.debug:
            return

        color = GREEN

        for layout in page.page_layout:
            # Create a rectangle box
            scale_factor = 1
            rect = il_version_1.PdfRectangle(
                box=il_version_1.Box(
                    x=layout.box.x,
                    y=layout.box.y,
                    x2=layout.box.x2,
                    y2=layout.box.y2,
                ),
                graphic_state=color,
                debug_info=True,
                line_width=0.4 * scale_factor,
            )
            page.pdf_rectangle.append(rect)

            # Create text label at top-left corner
            # Note: PDF coordinates are from bottom-left,
            # so we use y2 for top position
            style = il_version_1.PdfStyle(
                font_id="base",
                font_size=4 * scale_factor,
                graphic_state=color,
            )
            page.pdf_paragraph.append(
                il_version_1.PdfParagraph(
                    first_line_indent=False,
                    box=il_version_1.Box(
                        x=layout.box.x,
                        y=layout.box.y2,
                        x2=layout.box.x2,
                        y2=layout.box.y2 + 5,
                    ),
                    vertical=False,
                    pdf_style=style,
                    unicode=layout.class_name,
                    pdf_paragraph_composition=[
                        il_version_1.PdfParagraphComposition(
                            pdf_same_style_unicode_characters=il_version_1.PdfSameStyleUnicodeCharacters(
                                unicode=layout.class_name,
                                pdf_style=style,
                                debug_info=True,
                            ),
                        ),
                    ],
                    xobj_id=-1,
                ),
            )

    def process(self, docs: il_version_1.Document, mupdf_doc: Document):
        """Generate layouts for all pages that need to be translated."""
        # Get pages that need to be translated
        total = len(docs.page)
        with self.translation_config.progress_monitor.stage_start(
            self.stage_name,
            total * 2,
        ) as progress:
            # Process predictions for each page
            for page, layouts in self.model.handle_document(
                docs.page,
                mupdf_doc,
                self.translation_config,
                self._save_debug_image,
            ):
                page_layouts = []
                for layout in layouts.boxes:
                    # Convert coordinate system from picture to il
                    # system to the il coordinate system
                    x0, y0, x1, y1 = layout.xyxy
                    # pix = get_no_rotation_img(mupdf_doc[page.page_number])
                    # pix = mupdf_doc[page.page_number].get_pixmap()
                    # h, w = pix.height, pix.width
                    box = mupdf_doc[page.page_number].mediabox_size
                    b_h = math.ceil(box.y)
                    b_w = math.ceil(box.x)
                    # if b_h != h or b_w != w:
                    #     logger.warning(f"page {page.page_number} mediabox is not correct, b_h: {b_h}, h: {h}, b_w: {b_w}, w: {w}")
                    h, w = b_h, b_w
                    x0, y0, x1, y1 = (
                        np.clip(int(x0 - 1), 0, w - 1),
                        np.clip(int(h - y1 - 1), 0, h - 1),
                        np.clip(int(x1 + 1), 0, w - 1),
                        np.clip(int(h - y0 + 1), 0, h - 1),
                    )
                    page_layout = il_version_1.PageLayout(
                        id=len(page_layouts) + 1,
                        box=il_version_1.Box(
                            x0.item(),
                            y0.item(),
                            x1.item(),
                            y1.item(),
                        ),
                        conf=layout.conf.item(),
                        class_name=layouts.names[layout.cls],
                    )
                    page_layouts.append(page_layout)

                page.page_layout = page_layouts
                # self._save_debug_box_to_page(page)
                progress.advance(1)
            # 布局模型已完成全部页面的区域预测；这里只补进度（不再有字符聚类兜底）。
            for _ in docs.page:
                progress.advance(1)

        # 布局覆盖率门禁：未命中任何 layout 区域的原生字符超阈值时明确失败，
        # 避免“静默漏译”变成“静默交付”（替代已删除的 fallback_line 兜底）。
        coverage_report = compute_layout_coverage(docs)
        self._enforce_layout_coverage(docs, coverage_report)
        return docs

    def _coverage_report_path(self) -> Path:
        return Path(
            self.translation_config.get_working_file_path(LAYOUT_COVERAGE_ARTIFACT)
        )

    def _write_coverage_report(self, report: dict) -> Path:
        path = self._coverage_report_path()
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            logger.warning("Failed to write layout coverage report", exc_info=True)
        return path

    def _enforce_layout_coverage(
        self, docs: il_version_1.Document, report: dict
    ) -> None:
        """按阈值判定布局覆盖率；审计产物无论是否过门禁都落盘。"""
        threshold = float(
            getattr(self.translation_config, "layout_coverage_threshold", 0.005)
        )
        uncovered_ratio = report["global"]["uncovered_ratio"]
        report["threshold"] = threshold
        report["passed"] = uncovered_ratio <= threshold
        # 审计产物始终带未覆盖字符的 bbox 与文本片段（人工定位漏译源）。
        _uncovered_preview(docs, report)
        path = self._write_coverage_report(report)
        if report["passed"]:
            logger.info(
                "Layout coverage ok: uncovered=%d/%d (%.4f%%) threshold=%.4f%%",
                report["global"]["uncovered_chars"],
                report["global"]["total_chars"],
                uncovered_ratio * 100,
                threshold * 100,
            )
            return
        raise RuntimeError(
            "layout_coverage_gate: "
            f"未命中任何 layout 区域的原生字符占比 {uncovered_ratio:.4%} "
            f"超过阈值 {threshold:.4%} "
            f"（uncovered={report['global']['uncovered_chars']}/"
            f"{report['global']['total_chars']}）。"
            f"逐页明细与未覆盖文本片段见 {path}；"
            "可调大 --layout-coverage-threshold，或检查 MinerU 布局结果"
            "（--mineru-json 回放 / MINERU_API_TOKEN 重新解析）。"
        )
