import base64

import pymupdf

from yadt.il_try_1.document_il import il_try_1


class PDFCreater:
    def __init__(self, document: il_try_1.Document):
        self.docs = document

    def write(self, out_file: str):
        doc = pymupdf.open()
        for page in self.docs.page:
            width = page.mediabox.box.x2 - page.mediabox.box.x
            height = page.mediabox.box.y2 - page.mediabox.box.y
            doc.new_page(width=width, height=height)
        doc.save(out_file)
