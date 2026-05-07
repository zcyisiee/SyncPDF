from collections.abc import Generator

import babeldoc.format.pdf.document_il.il_version_1
import numpy as np
import pymupdf
from babeldoc.docvision.base_doclayout import YoloResult


class RapidOCRModel:
    """Compatibility no-op for the retired RapidOCR table text detector."""

    names = {0: "table_text"}

    @property
    def stride(self):
        return 32

    def predict(self, image, imgsz=800, batch_size=16, **kwargs):
        _ = image, imgsz, batch_size, kwargs
        return YoloResult(names=self.names, boxes=[])

    def handle_document(
        self,
        pages: list[babeldoc.format.pdf.document_il.il_version_1.Page],
        mupdf_doc: pymupdf.Document,
        translate_config,
        save_debug_image,
    ) -> Generator[
        tuple[babeldoc.format.pdf.document_il.il_version_1.Page, YoloResult], None, None
    ]:
        _ = mupdf_doc
        for page in pages:
            translate_config.raise_if_cancelled()
            yolo_result = YoloResult(names=self.names, boxes=[])
            if save_debug_image is not None:
                save_debug_image(
                    np.zeros((1, 1, 3), dtype=np.uint8),
                    yolo_result,
                    page.page_number + 1,
                )
            yield page, yolo_result
