import base64
import re

import numpy as np
import pdfminer.pdfinterp
import pymupdf
from pdfminer.layout import LTChar, LTFigure
from pdfminer.pdffont import PDFCIDFont, PDFFont
from pdfminer.psparser import PSLiteral

from yadt.doclayout import DocLayoutModel
from yadt.document_il import il_version_1
from yadt.translation_config import TranslationConfig


class ILCreater:
    stage_name = "解析PDF并创建中间表示"

    def __init__(self, translation_config: TranslationConfig):
        self.progress = None
        self.current_page: il_version_1.Page = None
        self.mupdf: pymupdf.Document = None
        self.model = DocLayoutModel.load_available()
        self.docs = il_version_1.Document(page=[])
        self.stroking_color_space_name = None
        self.non_stroking_color_space_name = None
        self.passthrough_per_char_instruction: list[tuple[str, str]] = []
        self.translation_config = translation_config
        self.passthrough_per_char_instruction_stack: list[list[tuple[str, str]]] = []

    def is_passthrough_per_char_operation(self, operator: str):
        return re.match("^(sc|scn|g|rg|k|cs|gs)$", operator, re.IGNORECASE)

    def on_passthrough_per_char(self, operator: str, args: list[str]):
        args = [self.parse_arg(arg) for arg in args]
        for i, value in enumerate(self.passthrough_per_char_instruction.copy()):
            op, arg = value
            if op == operator:
                self.passthrough_per_char_instruction.remove(value)
                break
        self.passthrough_per_char_instruction.append((operator, " ".join(args)))
        pass

    def parse_arg(self, arg: str):
        if isinstance(arg,PSLiteral):
            return f'/{arg.name}'
        if not isinstance(arg, str):
            return str(arg)
        return arg

    def pop_passthrough_per_char_instruction(self):
        if self.passthrough_per_char_instruction_stack:
            self.passthrough_per_char_instruction = (
                self.passthrough_per_char_instruction_stack.pop()
            )

    def push_passthrough_per_char_instruction(self):
        self.passthrough_per_char_instruction_stack.append(
            self.passthrough_per_char_instruction.copy()
        )

    # pdf32000 page 171
    def on_stroking_color_space(self, color_space_name):
        self.stroking_color_space_name = color_space_name

    def on_non_stroking_color_space(self, color_space_name):
        self.non_stroking_color_space_name = color_space_name

    def on_new_stream(self):
        self.stroking_color_space_name = None
        self.non_stroking_color_space_name = None
        self.passthrough_per_char_instruction_stack = []
        self.passthrough_per_char_instruction = []

    def on_page_start(self):
        self.current_page = il_version_1.Page(
            pdf_font=[],
            pdf_character=[],
            page_layout=[],
            # currently don't support UserUnit page parameter
            # pdf32000 page 79
            unit="point",
        )
        self.current_page_font_name_id_map = {}
        self.docs.page.append(self.current_page)
    def on_page_end(self):
        self.progress.advance(1)
    def on_page_crop_box(
        self,
        x0: float | int,
        y0: float | int,
        x1: float | int,
        y1: float | int,
    ):
        box = il_version_1.Box(x=float(x0), y=float(y0), x2=float(x1), y2=float(y1))
        self.current_page.cropbox = il_version_1.Cropbox(box=box)

    def on_page_media_box(
        self,
        x0: float | int,
        y0: float | int,
        x1: float | int,
        y1: float | int,
    ):
        box = il_version_1.Box(x=float(x0), y=float(y0), x2=float(x1), y2=float(y1))
        self.current_page.mediabox = il_version_1.Mediabox(box=box)

    def on_page_number(self, page_number: int):
        assert isinstance(page_number, int)
        assert page_number >= 0
        self.current_page.page_number = page_number

        self.on_page_layout(page_number)

    def on_page_base_operation(self, operation: str):
        self.current_page.base_operations = il_version_1.BaseOperations(value=operation)

    def on_page_resource_font(self, font: PDFFont, xref_id: int, font_id: str):
        font_name = font.fontname
        if isinstance(font_name, bytes):
            try:
                font_name = font_name.decode("utf-8")
            except UnicodeDecodeError:
                font_name = "BASE64:" + base64.b64encode(font_name).decode("utf-8")
        encoding_length = 1
        if isinstance(font, PDFCIDFont):
            try:
                _, to_unicode_id = self.mupdf.xref_get_key(xref_id, "ToUnicode")
                to_unicode_bytes = self.mupdf.xref_stream(
                    int(to_unicode_id.split(" ")[0])
                )
                range = re.search(
                    b"begincodespacerange\n?.*<(\\d+?)>.*", to_unicode_bytes
                ).group(1)
                encoding_length = len(range) // 2
            except:
                if max(font.unicode_map.cid2unichr.keys()) > 255:
                    encoding_length = 2
                else:
                    encoding_length = 1
        try:
            mupdf_font = pymupdf.Font(fontbuffer=self.mupdf.extract_font(xref_id)[3])
            bold = mupdf_font.is_bold
            italic = mupdf_font.is_italic
            monospaced = mupdf_font.is_monospaced
            serif = mupdf_font.is_serif
        except:
            bold = None
            italic = None
            monospaced = None
            serif = None
        il_font_metadata = il_version_1.PdfFont(
            name=font_name,
            xref_id=xref_id,
            font_id=font_id,
            encoding_length=encoding_length,
            bold=bold,
            italic=italic,
            monospace=monospaced,
            serif=serif,
        )
        self.current_page_font_name_id_map[font_name] = font_id
        self.current_page.pdf_font.append(il_font_metadata)

    def create_graphic_state(self, gs: pdfminer.pdfinterp.PDFGraphicState):
        graphic_state = il_version_1.GraphicState()
        for k, v in gs.__dict__.items():
            if v is None:
                continue
            if k in ["scolor", "ncolor"]:
                if isinstance(v, tuple):
                    v = list(v)
                else:
                    v = [v]
                setattr(graphic_state, k, v)
                continue
            if k == "linewidth":
                graphic_state.linewidth = float(v)
                continue
            continue
            raise NotImplementedError

        graphic_state.stroking_color_space_name = self.stroking_color_space_name
        graphic_state.non_stroking_color_space_name = self.non_stroking_color_space_name

        graphic_state.passthrough_per_char_instruction = " ".join(
            f"{arg} {op}" for op, arg in gs.passthrough_instruction
        )

        return graphic_state

    def on_lt_char(self, char: LTChar):
        gs = self.create_graphic_state(char.graphicstate)
        bbox = il_version_1.Box(char.bbox[0], char.bbox[1], char.bbox[2], char.bbox[3])

        font_name = char.font.fontname
        if isinstance(font_name, bytes):
            try:
                font_name = font_name.decode("utf-8")
            except UnicodeDecodeError:
                font_name = "BASE64:" + base64.b64encode(font_name).decode("utf-8")
        font_id = self.current_page_font_name_id_map[font_name]
        char_id = char.cid
        char_unicode = char.get_text()
        advance = char.adv
        if char.matrix[0] == 0 and char.matrix[3] == 0:
            vertical = True
        else:
            vertical = False
        pdf_style = il_version_1.PdfStyle(
            font_id=font_id,
            font_size=char.size,
            graphic_state=gs,
        )
        pdf_char = il_version_1.PdfCharacter(
            box=bbox,
            pdf_character_id=char_id,
            advance=advance,
            char_unicode=char_unicode,
            vertical=vertical,
            pdf_style=pdf_style,
        )
        self.current_page.pdf_character.append(pdf_char)

    def on_page_layout(self, page_number):
        if self.translation_config.should_translate_page(page_number + 1) is False:
            return
        pix = self.mupdf[page_number].get_pixmap()
        image = np.fromstring(pix.samples, np.uint8).reshape(pix.height, pix.width, 3)[
            :, :, ::-1
        ]
        h, w = pix.height, pix.width
        layouts = self.model.predict(image, imgsz=int(pix.height / 32) * 32)[0]
        id = 0
        for layout in layouts.boxes:
            id += 1
            # Convert the coordinate system from the picture coordinate system to the il coordinate system
            x0, y0, x1, y1 = layout.xyxy
            x0, y0, x1, y1 = (
                np.clip(int(x0 - 1), 0, w - 1),
                np.clip(int(h - y1 - 1), 0, h - 1),
                np.clip(int(x1 + 1), 0, w - 1),
                np.clip(int(h - y0 + 1), 0, h - 1),
            )
            page_layout = il_version_1.PageLayout(
                id=id,
                box=il_version_1.Box(x0.item(), y0.item(), x1.item(), y1.item()),
                conf=layout.conf.item(),
                class_name=layouts.names[layout.cls],
            )
            self.current_page.page_layout.append(page_layout)

    def create_il(self):
        pages = [
            page
            for page in self.docs.page
            if self.translation_config.should_translate_page(page.page_number + 1)
        ]
        self.docs.page = pages
        return self.docs

    def on_total_pages(self, total_pages: int):
        assert isinstance(total_pages, int)
        assert total_pages > 0
        self.docs.total_pages = total_pages
        total = 0
        for page in range(total_pages):
            if self.translation_config.should_translate_page(page + 1) is False:
                continue
            total += 1
        self.progress = self.translation_config.progress_monitor.stage_start(
            self.stage_name, total
        )

    def on_pdf_figure(self, figure: LTFigure):
        box = il_version_1.Box(
            figure.bbox[0], figure.bbox[1], figure.bbox[2], figure.bbox[3]
        )
        self.current_page.pdf_figure.append(il_version_1.PdfFigure(box=box))
