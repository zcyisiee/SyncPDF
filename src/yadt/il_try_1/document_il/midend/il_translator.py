import concurrent.futures

from tqdm import tqdm

from yadt.il_try_1.document_il.il_try_1 import Document, Page, PdfParagraph
from yadt.il_try_1.document_il.translator.translator import BaseTranslator


class ILTranslator:
    def __init__(self, translate_engine):
        self.translate_engine = translate_engine

    def translate(self, docs: Document):
        pbar = tqdm(total=0, desc="translate")
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            for page in docs.page:
                self.process_page(page, executor)

    def process_page(
        self,
        page: Page,
        executor: concurrent.futures.ThreadPoolExecutor,
        pbar: tqdm | None = None,
    ):
        for paragraph in page.pdf_paragraph:
            if pbar:
                pbar.total += 1
            executor.submit(self.translate_paragraph, paragraph, pbar)

    def translate_paragraph(
        self, paragraph: PdfParagraph, pbar: tqdm | None = None
    ):
        text = paragraph.unicode
        translated_text = self.translate_engine.translate(text)
        if pbar:
            pbar.update(1)
        if translated_text == text:
            return
        paragraph.unicode = translated_text

        # due to translation, lost all details
        paragraph.pdf_line = []
