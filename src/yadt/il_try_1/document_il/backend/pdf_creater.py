import io

import pdfminer.pdfdocument
import pymupdf
from bitstring import Bits, BitStream

from yadt.il_try_1.document_il import il_try_1
from pdfminer.pdfparser import PDFParser
from pdfminer.pdfpage import PDFPage
from pdfminer.pdfinterp import PDFResourceManager

from pdfminer.pdftypes import (
    PDFObjRef,
    dict_value,
    list_value,
    resolve1,
    stream_value,
)
from pdfminer.pdffont import PDFFont


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
                f"/{graphic_state.stroking_color_space_name}" f" CS \n".encode()
            )
        if graphic_state.non_stroking_color_space_name:
            draw_op.append(
                f"/{graphic_state.non_stroking_color_space_name}" f" cs \n".encode()
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

    def render_paragraph_unicode_to_char(
        self, paragraph: il_try_1.PdfParagraph, noto_font: pymupdf.Font
    ) -> list[il_try_1.PdfCharacter]:
        text = paragraph.unicode
        font_size = paragraph.size * 0.6
        box = paragraph.box
        
        # 初始化结果列表和当前坐标
        chars = []
        current_x = box.x
        current_y = box.y
        line_width = box.x2 - box.x  # 计算可用行宽
        
        # 遍历文本中的每个字符
        for i, char in enumerate(text):
            # 获取字符的宽度
            char_width = noto_font.char_lengths(char, font_size)[0]
            
            # 检查是否需要换行
            if current_x + char_width > box.x2:
                # 换行：重置x坐标，增加y坐标
                current_x = box.x
                current_y += font_size * 1.2  # 使用1.2倍行距
            
            # 创建字符的边界框
            char_box = il_try_1.Box(
                x=current_x,
                y=current_y,
                x2=current_x + char_width,
                y2=current_y + font_size
            )
            
            # 创建PdfCharacter对象
            pdf_char = il_try_1.PdfCharacter(
                pdf_font_id='noto',
                pdf_character_id=noto_font.has_glyph(ord(char)),
                char_unicode=char,
                box=char_box,
                size=font_size,
                graphic_state=paragraph.graphic_state
            )
            
            # 添加到结果列表
            chars.append(pdf_char)
            
            # 更新下一个字符的起始x坐标
            current_x += char_width
        
        return chars

    def render_paragraph_to_char(
        self, paragraph: il_try_1.PdfParagraph, noto_font: pymupdf.Font
    ) -> list[il_try_1.PdfCharacter]:
        chars = []
        for line in paragraph.pdf_line:
            chars.extend(line.pdf_character)
        if not chars:
            return self.render_paragraph_unicode_to_char(paragraph, noto_font)
        return chars

    def add_font(self, doc_zh: pymupdf.Document):
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
        mu_noto = pymupdf.Font(fontfile=noto_path)
        return mu_noto

    def write(self, out_file: str):
        pdf = pymupdf.open(self.original_pdf_path)
        noto_font = self.add_font(pdf)
        for page in self.docs.page:
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
                chars.extend(self.render_paragraph_to_char(paragraph, noto_font))

            # 渲染所有字符
            for char in chars:
                if char.char_unicode == "\n":
                    continue
                char_size = char.size
                draw_op.append(b"q ")
                self.render_graphic_state(draw_op, char.graphic_state)
                draw_op.append(
                    f"BT /{char.pdf_font_id} {char_size:f} Tf 1 0 0 1 {
                        char.box.x:f} {char.box.y:f} Tm (".encode()
                )
                # pdf 32000 2008: 15
                # 7.3.4.2 Literal Strings
                if char.pdf_character_id in (
                    ord("\\"),
                    ord("("),
                    ord(")"),
                ):
                    draw_op.append(b"\\")
                char_bit_length = 8
                if char.pdf_character_id >= 2**16:
                    char_bit_length = 32
                elif char.pdf_character_id >= 2**8:
                    char_bit_length = 16
                char_bit_length=16
                draw_op.append(
                    Bits(uint=char.pdf_character_id, length=char_bit_length).tobytes()
                )

                draw_op.append(b") Tj ET Q \n")

            op_container = pdf.get_new_xref()
            # Since this is a draw instruction container,
            # no additional information is needed
            pdf.update_object(op_container, "<<>>")
            pdf.update_stream(op_container, draw_op.tobytes())
            pdf[page.page_number].set_contents(op_container)
        pdf.save(out_file, expand=True, pretty=True)
        pdf.save(out_file + ".compressed.pdf", garbage=3, deflate=True)
