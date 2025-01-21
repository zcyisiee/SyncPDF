import asyncio
import io
import os
import time
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
from yadt.document_il.midend.styles_and_formulas import StylesAndFormulas
from yadt.document_il.midend.typesetting import Typesetting
from yadt.document_il.translator.translator import set_translate_rate_limiter
from yadt.document_il.xml_converter import XMLConverter
from yadt.pdfinterp import PDFPageInterpreterEx

from yadt.document_il.frontend.il_creater import ILCreater
from yadt.document_il.backend.pdf_creater import PDFCreater
from yadt.translation_config import TranslationConfig
from yadt.progress_monitor import ProgressMonitor
from yadt.document_il.utils.fontmap import FontMapper
import logging

logger = logging.getLogger(__name__)

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
    il_creater: ILCreater = None,
    translation_config: TranslationConfig = None,
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
    # model = DocLayoutModel.load_available()

    assert device is not None
    assert il_creater is not None
    assert translation_config is not None
    obj_patch = {}
    interpreter = PDFPageInterpreterEx(rsrcmgr, device, obj_patch, il_creater)
    if pages:
        total_pages = len(pages)
    else:
        total_pages = doc_zh.page_count

    il_creater.on_total_pages(total_pages)

    parser = PDFParser(inf)
    doc = PDFDocument(parser)

    for pageno, page in enumerate(PDFPage.create_pages(doc)):
        if cancellation_event and cancellation_event.is_set():
            raise CancelledError("task cancelled")
        if pages and (pageno not in pages):
            continue
        page.pageno = pageno
        if not translation_config.should_translate_page(pageno + 1):
            continue
        # The current program no longer relies on
        # the following layout recognition results,
        # but in order to facilitate the migration of pdf2zh,
        # the relevant code is temporarily retained.
        # pix = doc_zh[page.pageno].get_pixmap()
        # image = np.fromstring(pix.samples, np.uint8).reshape(
        #     pix.height, pix.width, 3
        # )[:, :, ::-1]
        # page_layout = model.predict(
        #     image, imgsz=int(pix.height / 32) * 32)[0]
        # # kdtree 是不可能 kdtree 的，不如直接渲染成图片，用空间换时间
        # box = np.ones((pix.height, pix.width))
        # h, w = box.shape
        # vcls = ["abandon", "figure", "table",
        #         "isolate_formula", "formula_caption"]
        # for i, d in enumerate(page_layout.boxes):
        #     if page_layout.names[int(d.cls)] not in vcls:
        #         x0, y0, x1, y1 = d.xyxy.squeeze()
        #         x0, y0, x1, y1 = (
        #             np.clip(int(x0 - 1), 0, w - 1),
        #             np.clip(int(h - y1 - 1), 0, h - 1),
        #             np.clip(int(x1 + 1), 0, w - 1),
        #             np.clip(int(h - y0 + 1), 0, h - 1),
        #         )
        #         box[y0:y1, x0:x1] = i + 2
        # for i, d in enumerate(page_layout.boxes):
        #     if page_layout.names[int(d.cls)] in vcls:
        #         x0, y0, x1, y1 = d.xyxy.squeeze()
        #         x0, y0, x1, y1 = (
        #             np.clip(int(x0 - 1), 0, w - 1),
        #             np.clip(int(h - y1 - 1), 0, h - 1),
        #             np.clip(int(x1 + 1), 0, w - 1),
        #             np.clip(int(h - y0 + 1), 0, h - 1),
        #         )
        #         box[y0:y1, x0:x1] = 0
        # layout[page.pageno] = box
        # 新建一个 xref 存放新指令流
        page.page_xref = doc_zh.get_new_xref()  # hack 插入页面的新 xref
        doc_zh.update_object(page.page_xref, "<<>>")
        doc_zh.update_stream(page.page_xref, b"")
        doc_zh[page.pageno].set_contents(page.page_xref)
        ops_base = interpreter.process_page(page)
        il_creater.on_page_base_operation(ops_base)
        il_creater.on_page_end()

    device.close()


def translate(translation_config: TranslationConfig):
    with ProgressMonitor(
        translation_config,
        [
            ILCreater.stage_name,
            ParagraphFinder.stage_name,
            StylesAndFormulas.stage_name,
            ILTranslator.stage_name,
            Typesetting.stage_name,
            FontMapper.stage_name,
            PDFCreater.stage_name,
        ],
    ) as pm:
        translation_config.progress_monitor = pm
        original_pdf_path = translation_config.input_file
        logger.info(f"start to translate: {original_pdf_path}")
        start_time = time.time()

        doc_input = Document(original_pdf_path)
        if translation_config.debug:
            logger.debug("debug mode, save decompressed input pdf")
            output_path = translation_config.get_working_file_path(
                "input.decompressed.pdf"
            )
            doc_input.save(output_path, expand=True, pretty=True)

        # Continue with original processing
        temp_pdf_path = translation_config.get_working_file_path("input.pdf")

        doc_pdf2zh = Document(original_pdf_path)
        resfont = "china-ss"
        for page in doc_pdf2zh:
            page.insert_font(resfont, None)
        doc_pdf2zh.save(temp_pdf_path)

        il_creater = ILCreater(translation_config)
        il_creater.mupdf = doc_input

        xml_converter = XMLConverter()

        logger.debug(f"start parse il from {temp_pdf_path}")
        with open(temp_pdf_path, "rb") as f:
            start_parse_il(
                f,
                doc_zh=doc_pdf2zh,
                resfont=resfont,
                il_creater=il_creater,
                translation_config=translation_config,
            )
        logger.debug(f"finish parse il from {temp_pdf_path}")

        docs = il_creater.create_il()
        logger.debug(f"finish create il from {temp_pdf_path}")

        if translation_config.debug:
            xml_converter.write_json(
                docs, translation_config.get_working_file_path("create_il.debug.json")
            )

        ParagraphFinder(translation_config).process(docs)
        logger.debug(f"finish paragraph finder from {temp_pdf_path}")
        if translation_config.debug:
            xml_converter.write_json(
                docs, translation_config.get_working_file_path("paragraph_finder.json")
            )

        StylesAndFormulas(translation_config).process(docs)
        logger.debug(f"finish styles and formulas from {temp_pdf_path}")
        if translation_config.debug:
            xml_converter.write_json(
                docs,
                translation_config.get_working_file_path("styles_and_formulas.json"),
            )

        translate_engine = translation_config.translator
        # translate_engine.ignore_cache = True
        ILTranslator(translate_engine, translation_config).translate(docs)
        logger.debug(f"finish ILTranslator from {temp_pdf_path}")
        if translation_config.debug:
            xml_converter.write_json(
                docs, translation_config.get_working_file_path("il_translated.json")
            )

        Typesetting(translation_config).typsetting_document(docs)
        logger.debug(f"finish typsetting from {temp_pdf_path}")
        if translation_config.debug:
            xml_converter.write_json(
                docs, translation_config.get_working_file_path("typsetting.json")
            )

        # deepcopy
        # docs2 = xml_converter.deepcopy(docs)

        pdf_creater = PDFCreater(original_pdf_path, docs, translation_config)

        pdf_creater.write(translation_config)

        finish_time = time.time()

        logger.info(
            f"finish translate: {original_pdf_path}, cost: {finish_time - start_time} s"
        )
