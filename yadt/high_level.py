import asyncio
import io
import os
from asyncio import CancelledError
from typing import Any, BinaryIO, Optional

import numpy as np
import pymupdf
import tqdm
from pdfminer.pdfdocument import PDFDocument
from pdfminer.pdfinterp import PDFResourceManager
from pdfminer.pdfpage import PDFPage
from pdfminer.pdfparser import PDFParser
from pymupdf import Document, Font
from yadt.converter import TranslateConverter
from yadt.doclayout import DocLayoutModel
from yadt.document_il.midend.il_translator import ILTranslator
from yadt.document_il.midend.paragraph_finder import ParagraphFinder
from yadt.document_il.midend.typesetting import Typesetting
from yadt.document_il.translator.translator import (
    OpenAITranslator,
    set_translate_rate_limiter,
)
from yadt.document_il.xml_converter import XMLConverter
from yadt.pdfinterp import PDFPageInterpreterEx

from yadt.document_il.frontend.il_creater import ILCreater
from yadt.document_il.backend.pdf_creater import PDFCreater

model = DocLayoutModel.load_available()
resfont_map = {
    "zh-cn": "china-ss",
    "zh-tw": "china-ts",
    "zh-hans": "china-ss",
    "zh-hant": "china-ts",
    "zh": "china-ss",
    "ja": "japan-s",
    "ko": "korea-s",
}


def start_parse_il(
    inf: BinaryIO,
    pages: Optional[list[int]] = None,
    vfont: str = "",
    vchar: str = "",
    thread: int = 0,
    doc_zh: Document = None,
    lang_in: str = "",
    lang_out: str = "",
    service: str = "",
    resfont: str = "",
    noto: Font = None,
    cancellation_event: asyncio.Event = None,
    il_creater=None,
    **kwarg: Any,
) -> None:
    rsrcmgr = PDFResourceManager()
    layout = {}
    device = TranslateConverter(
        rsrcmgr,
        vfont,
        vchar,
        thread,
        layout,
        lang_in,
        lang_out,
        service,
        resfont,
        noto,
        kwarg.get("envs", {}),
        kwarg.get("prompt", []),
        il_creater=il_creater,
    )

    assert device is not None
    if il_creater is None:
        il_creater = ILCreater()
    obj_patch = {}
    interpreter = PDFPageInterpreterEx(rsrcmgr, device, obj_patch, il_creater)
    if pages:
        total_pages = len(pages)
    else:
        total_pages = doc_zh.page_count

    il_creater.on_total_pages(total_pages)

    parser = PDFParser(inf)
    doc = PDFDocument(parser)
    with tqdm.tqdm(total=total_pages, desc="parse pdf to il") as progress:
        for pageno, page in enumerate(PDFPage.create_pages(doc)):
            if cancellation_event and cancellation_event.is_set():
                raise CancelledError("task cancelled")
            if pages and (pageno not in pages):
                continue
            progress.update()
            page.pageno = pageno
            pix = doc_zh[page.pageno].get_pixmap()
            image = np.fromstring(pix.samples, np.uint8).reshape(
                pix.height, pix.width, 3
            )[:, :, ::-1]
            page_layout = model.predict(image, imgsz=int(pix.height / 32) * 32)[0]
            # kdtree 是不可能 kdtree 的，不如直接渲染成图片，用空间换时间
            box = np.ones((pix.height, pix.width))
            h, w = box.shape
            vcls = ["abandon", "figure", "table", "isolate_formula", "formula_caption"]
            for i, d in enumerate(page_layout.boxes):
                if page_layout.names[int(d.cls)] not in vcls:
                    x0, y0, x1, y1 = d.xyxy.squeeze()
                    x0, y0, x1, y1 = (
                        np.clip(int(x0 - 1), 0, w - 1),
                        np.clip(int(h - y1 - 1), 0, h - 1),
                        np.clip(int(x1 + 1), 0, w - 1),
                        np.clip(int(h - y0 + 1), 0, h - 1),
                    )
                    box[y0:y1, x0:x1] = i + 2
            for i, d in enumerate(page_layout.boxes):
                if page_layout.names[int(d.cls)] in vcls:
                    x0, y0, x1, y1 = d.xyxy.squeeze()
                    x0, y0, x1, y1 = (
                        np.clip(int(x0 - 1), 0, w - 1),
                        np.clip(int(h - y1 - 1), 0, h - 1),
                        np.clip(int(x1 + 1), 0, w - 1),
                        np.clip(int(h - y0 + 1), 0, h - 1),
                    )
                    box[y0:y1, x0:x1] = 0
            layout[page.pageno] = box
            # 新建一个 xref 存放新指令流
            page.page_xref = doc_zh.get_new_xref()  # hack 插入页面的新 xref
            doc_zh.update_object(page.page_xref, "<<>>")
            doc_zh.update_stream(page.page_xref, b"")
            doc_zh[page.pageno].set_contents(page.page_xref)
            ops_base = interpreter.process_page(page)
            il_creater.on_page_base_operation(ops_base)

    device.close()


def translate():
    resfont = "china-ss"
    print(os.getcwd())
    original_pdf_path = "../examples/pdf/il_try_1/这是一个测试文件.pdf"
    print(os.path.abspath(original_pdf_path))
    with open(original_pdf_path, "rb") as f:
        raw = f.read()
    doc_en = Document(stream=raw)

    # output_path = "../../../examples/pdf/il_try_1/这是一个测试文件.解压缩.pdf"
    # with open(output_path, "wb") as out_f:
    #     doc_en.save(out_f, expand=True, pretty=True)

    # Continue with original processing
    stream = io.BytesIO()
    doc_en.save(stream)
    doc_zh = Document(stream=stream)
    for page in doc_zh:
        page.insert_font(resfont, None)
    fp = io.BytesIO()
    doc_zh.save(fp)

    il_creater = ILCreater()

    il_creater.mupdf = doc_en
    start_parse_il(fp, doc_zh=doc_zh, resfont=resfont, il_creater=il_creater)

    docs = il_creater.create_il()
    ParagraphFinder().process(docs)

    set_translate_rate_limiter(50)
    translate_engine = OpenAITranslator("zh_cn", "en-us", "Qwen/Qwen2.5-72B-Instruct")
    # translate_engine.ignore_cache = True
    ILTranslator(translate_engine).translate(docs)

    Typesetting().typsetting_document(docs)
    xml_converter = XMLConverter()

    xml = xml_converter.to_xml(docs)

    with open("../examples/pdf/il_try_1/测试解析.xml", "w") as f:
        f.write(xml)

    with open("../examples/pdf/il_try_1/测试解析.xml", "r") as f:
        xml = f.read()
    docs2 = xml_converter.from_xml(xml)

    pdf_creater = PDFCreater(original_pdf_path, docs2)

    pdf_creater.write("../examples/pdf/il_try_1/测试还原.pdf")


if __name__ == "__main__":
    translate()