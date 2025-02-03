import numpy as np
from pymupdf import Document
from yadt.document_il import il_version_1
from yadt.translation_config import TranslationConfig
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
        # Get pages that need to be translated
        pages_to_translate = [
            page for page in docs.page
            if self.translation_config.should_translate_page(
                page.page_number + 1
            )
        ]
        total = len(pages_to_translate)
        self.progress = self.translation_config.progress_monitor.stage_start(
            self.stage_name, total)

        # Process pages in batches
        batch_size = 48
        for i in range(0, total, batch_size):
            batch_pages = pages_to_translate[i:i + batch_size]
            
            # Prepare batch images
            batch_images = []
            for page in batch_pages:
                pix = mupdf_doc[page.page_number].get_pixmap()
                image = np.fromstring(pix.samples, np.uint8).reshape(
                    pix.height, pix.width, 3
                )[:, :, ::-1]
                batch_images.append(image)
            
            # Get predictions for the batch
            layouts_batch = self.model.predict(batch_images,batch_size=batch_size)

            # Process predictions for each page
            for page, layouts in zip(batch_pages, layouts_batch):
                page_layouts = []
                for layout in layouts.boxes:
                    # Convert the coordinate system from the picture coordinate
                    # system to the il coordinate system
                    x0, y0, x1, y1 = layout.xyxy
                    pix = mupdf_doc[page.page_number].get_pixmap()
                    h, w = pix.height, pix.width
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
