import io

import pdfminer.pdfdocument
import pymupdf
from bitstring import Bits, BitStream
from pdfminer.pdffont import PDFFont
from pdfminer.pdfinterp import PDFResourceManager
from pdfminer.pdfpage import PDFPage
from pdfminer.pdfparser import PDFParser
from pdfminer.pdftypes import (
    PDFObjRef,
    dict_value,
    list_value,
    resolve1,
    stream_value,
)

from yadt.il_try_1.document_il import il_try_1


class PDFCreater:
    def __init__(self, original_pdf_path: str, document: il_try_1.Document):
        self.original_pdf_path = original_pdf_path
        self.docs = document

    def render_graphic_state(
        self, draw_op: BitStream, graphic_state: il_try_1.GraphicState
    ):
        if graphic_state is None:
            return
        if graphic_state.stroking_color_space_name:
            draw_op.append(
                f"/{graphic_state.stroking_color_space_name}"
                f" CS \n".encode()
            )
        if graphic_state.non_stroking_color_space_name:
            draw_op.append(
                f"/{graphic_state.non_stroking_color_space_name}"
                f" cs \n".encode()
            )
        if graphic_state.ncolor is not None:
            draw_op.append(
                f"{' '.join((str(x) for x in graphic_state.ncolor))
                   } sc \n".encode()
            )
        if graphic_state.scolor is not None:
            draw_op.append(
                f"{' '.join((str(x) for x in graphic_state.ncolor))
                   } SC \n".encode()
            )

    def render_paragraph_to_char(
        self, paragraph: il_try_1.PdfParagraph
    ) -> list[il_try_1.PdfCharacter]:
        chars = []
        for line in paragraph.pdf_line:
            chars.extend(line.pdf_character)
        if not chars and paragraph.unicode:
            raise Exception(
                "Unable to export paragraphs that have not yet been formatted"
            )
        return chars

    def add_font(self, doc_zh: pymupdf.Document, il: il_try_1.Document):
        noto_path = r"/Users/aw/Downloads/GoNotoKurrent-Regular.ttf"
        font_list = [
            ("noto", noto_path),
        ]
        font_id = {}
        for page in doc_zh:
            for font in font_list:
                font_id[font[0]] = page.insert_font(font[0], font[1])
        xreflen = doc_zh.xref_length()
        for xref in range(1, xreflen):
            for label in ["Resources/", ""]:  # 可能是基于 xobj 的 res
                try:  # xref 读写可能出错
                    font_res = doc_zh.xref_get_key(xref, f"{label}Font")
                    if font_res[0] == "dict":
                        for font in font_list:
                            font_exist = doc_zh.xref_get_key(
                                xref, f"{label}Font/{font[0]}"
                            )
                            if font_exist[0] == "null":
                                doc_zh.xref_set_key(
                                    xref,
                                    f"{label}Font/{font[0]}",
                                    f"{font_id[font[0]]} 0 R",
                                )
                except Exception:
                    pass
        # buffer = io.BytesIO()
        # doc_zh.save(buffer)
        # parser = PDFParser(buffer)
        # miner_doc = pdfminer.pdfdocument.PDFDocument(parser)
        # first_page = next(PDFPage.create_pages(miner_doc))
        # resources = first_page.resources
        # rsrcmgr = PDFResourceManager()
        # for k, v in dict_value(resources).items():
        #     # log.debug("Resource: %r: %r", k, v)
        #     if k == "Font":
        #         for fontid, spec in dict_value(v).items():
        #             objid = None
        #             if isinstance(spec, PDFObjRef):
        #                 objid = spec.objid
        #             spec = dict_value(spec)
        #             font = rsrcmgr.get_font(objid, spec)
        #             if fontid == "noto":
        #                 return font
        pdf_font_il = il_try_1.PdfFont(
            name="noto",
            xref_id=font_id["noto"],
            font_id="noto",
            encoding_length=2,
        )
        for page in il.page:
            page.pdf_font.append(pdf_font_il)

    def write(self, out_file: str):
        pdf = pymupdf.open(self.original_pdf_path)
        self.add_font(pdf, self.docs)
        for page in self.docs.page:
            encoding_length_map = {
                f.font_id: f.encoding_length for f in page.pdf_font
            }
            draw_op = BitStream()
            # q {ops_base}Q 1 0 0 1 {x0} {y0} cm {ops_new}
            draw_op.append(b"q ")
            draw_op.append(page.base_operations.value.encode())
            draw_op.append(b" Q ")
            draw_op.append(
                f"q Q 1 0 0 1 {page.cropbox.box.x} {
                    page.cropbox.box.y} cm \n".encode()
            )

            # 收集所有字符
            chars = []
            # 首先添加页面级别的字符
            if page.pdf_character:
                chars.extend(page.pdf_character)
            # 然后添加段落中的字符
            for paragraph in page.pdf_paragraph:
                chars.extend(self.render_paragraph_to_char(paragraph))

            # 渲染所有字符
            for char in chars:
                if char.char_unicode == "\n":
                    continue
                char_size = char.size
                draw_op.append(b"q ")
                self.render_graphic_state(draw_op, char.graphic_state)
                draw_op.append(
                    f"BT /{char.pdf_font_id} {char_size:f} Tf 1 0 0 1 {
                        char.box.x:f} {char.box.y:f} Tm ".encode()
                )

                encoding_length = encoding_length_map[char.pdf_font_id]
                # pdf32000-2008 page14:
                # As hexadecimal data enclosed in angle brackets < >
                # see 7.3.4.3, "Hexadecimal Strings."
                draw_op.append(
                    f"<{char.pdf_character_id:0{encoding_length*2}x}>".encode()
                )

                draw_op.append(b" Tj ET Q \n")

            op_container = pdf.get_new_xref()
            # Since this is a draw instruction container,
            # no additional information is needed
            pdf.update_object(op_container, "<<>>")
            pdf.update_stream(op_container, draw_op.tobytes())
            pdf[page.page_number].set_contents(op_container)
        pdf.save(out_file, expand=True, pretty=True)
        pdf.save(out_file + ".compressed.pdf", garbage=3, deflate=True)
