import numpy as np
from pymupdf import Document
from yadt.document_il import il_version_1
from yadt.translation_config import TranslationConfig
from yadt.doclayout import DocLayoutModel
import logging

logger = logging.getLogger(__name__)


class LayoutParser:
    stage_name = "解析页面布局"

    def __init__(self, translation_config: TranslationConfig):
        self.translation_config = translation_config
        self.model = translation_config.doc_layout_model
        self.progress = None

    def process(self, docs: il_version_1.Document, mupdf_doc: Document):
        """Generate layouts for all pages that need to be translated."""
        total = sum(1 for page in docs.page if self.translation_config.should_translate_page(
            page.page_number + 1))
        self.progress = self.translation_config.progress_monitor.stage_start(
            self.stage_name, total)

        for page in docs.page:
            if not self.translation_config.should_translate_page(page.page_number + 1):
                continue

            page_number = page.page_number
            pix = mupdf_doc[page_number].get_pixmap()
            image = np.fromstring(pix.samples, np.uint8).reshape(
                pix.height, pix.width, 3
            )[:, :, ::-1]
            h, w = pix.height, pix.width
            layouts = self.model.predict(
                image, imgsz=int(pix.height / 32) * 32)[0]

            page_layouts = []
            for layout in layouts.boxes:
                # Convert the coordinate system from the picture coordinate system to the il coordinate system
                x0, y0, x1, y1 = layout.xyxy
                x0, y0, x1, y1 = (
                    np.clip(int(x0 - 1), 0, w - 1),
                    np.clip(int(h - y1 - 1), 0, h - 1),
                    np.clip(int(x1 + 1), 0, w - 1),
                    np.clip(int(h - y0 + 1), 0, h - 1),
                )
                page_layout = il_version_1.PageLayout(
                    id=len(page_layouts) + 1,
                    box=il_version_1.Box(
                        x0.item(), y0.item(), x1.item(), y1.item()
                    ),
                    conf=layout.conf.item(),
                    class_name=layouts.names[layout.cls],
                )
                page_layouts.append(page_layout)

            page.page_layout = page_layouts
            self.progress.advance(1)

        return docs
