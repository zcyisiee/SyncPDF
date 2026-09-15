import abc
from collections.abc import Generator

import pymupdf

from babeldoc.format.pdf.document_il.il_version_1 import Page


class YoloResult:
    """Helper class to store detection results from ONNX model."""

    def __init__(self, names, boxes=None, boxes_data=None, *, preserve_order=False):
        if boxes is not None:
            self.boxes = boxes
        else:
            assert boxes_data is not None
            self.boxes = [YoloBox(data=d) for d in boxes_data]
        # Most legacy providers rank regions by confidence.  Paddle's
        # PP-DocLayoutV3 additionally emits an explicit reading order; preserve
        # that order when requested so downstream paragraph clustering follows
        # the model's column/region sequence.
        if not preserve_order:
            self.boxes.sort(key=lambda x: x.conf, reverse=True)
        self.names = names


class YoloBox:
    """Helper class to store detection results from ONNX model."""

    def __init__(self, data=None, xyxy=None, conf=None, cls=None):
        if data is not None:
            self.xyxy = data[:4]
            self.conf = data[-2]
            self.cls = data[-1]
            return
        assert xyxy is not None and conf is not None and cls is not None
        self.xyxy = xyxy
        self.conf = conf
        self.cls = cls


class DocLayoutModel(abc.ABC):
    """布局模型基类。

    本地 ONNX 后端（`babeldoc.docvision.doclayout`）已移除：MinerU 是唯一布局
    后端。字符聚类兜底（`fallback_line`）随之删除，改由
    `LayoutParser` 的布局覆盖率门禁暴露未覆盖字符。
    """

    @property
    @abc.abstractmethod
    def stride(self) -> int:
        """Stride of the model input."""

    @abc.abstractmethod
    def handle_document(
        self,
        pages: list[Page],
        mupdf_doc: pymupdf.Document,
        translate_config,
        save_debug_image,
    ) -> Generator[tuple[Page, YoloResult], None, None]:
        """
        Handle a document.
        """
