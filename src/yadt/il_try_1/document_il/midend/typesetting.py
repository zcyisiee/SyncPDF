import pymupdf

from yadt.il_try_1.document_il import Box, PdfCharacter, PdfLine, il_try_1

noto_path = r"/Users/aw/Downloads/GoNotoKurrent-Regular.ttf"
noto = pymupdf.Font(fontfile=noto_path)


class Typesetting:
    def __init__(self):
        pass

    def create_line(self, chars: list[PdfCharacter]) -> PdfLine:
        if not chars:
            return None

        # 计算行的边界框
        min_x = min(char.box.x for char in chars)
        min_y = min(char.box.y for char in chars)
        max_x = max(char.box.x2 for char in chars)
        max_y = max(char.box.y2 for char in chars)
        box = Box(min_x, min_y, max_x, max_y)

        graphic_state = chars[0].graphic_state
        # 创建行对象
        line = PdfLine(
            box=box,
            graphic_state=graphic_state,
            pdf_character=chars,
            unicode="".join(char.char_unicode for char in chars),
            size=max_y - min_y,  # 使用行的高度作为size
            scale=chars[0].scale,
        )
        return line

    def try_typeset(
        self, scale: float, noto_font: pymupdf.Font, paragraph: il_try_1.PdfParagraph
    ):
        text = paragraph.unicode
        current_font_size = paragraph.size * scale
        box = paragraph.box
        current_x = box.x
        current_y = box.y2 - current_font_size
        chars = []

        for char in text:
            char_width = noto_font.char_lengths(char, current_font_size)[0]

            if current_x + char_width > box.x2:
                current_x = box.x
                current_y -= current_font_size * 1.4

                # 检查是否超出底部边界
                if current_y < box.y or current_y + current_font_size > box.y2:
                    return None

            char_box = il_try_1.Box(
                x=current_x,
                y=current_y,
                x2=current_x + char_width,
                y2=current_y + current_font_size,
            )

            chars.append(
                il_try_1.PdfCharacter(
                    pdf_font_id="noto",
                    pdf_character_id=noto_font.has_glyph(ord(char)),
                    char_unicode=char,
                    box=char_box,
                    size=current_font_size,
                    graphic_state=paragraph.graphic_state,
                    scale=scale,
                )
            )

            current_x += char_width

        return chars

    def typsetting_document(self, document: il_try_1.Document):
        for page in document.page:
            for paragraph in page.pdf_paragraph:
                self.create_line(
                    self.render_paragraph_unicode_to_char(paragraph, noto),
                )

    def render_paragraph_unicode_to_char(
        self, paragraph: il_try_1.PdfParagraph, noto_font: pymupdf.Font
    ):
        scale = 1.0
        # 尝试排版，如果失败则逐步缩小字号
        while scale >= 0.4:
            result = self.try_typeset(scale, noto_font, paragraph)
            if result is not None:
                paragraph.pdf_line = [self.create_line(result)]
                paragraph.scale = scale
                return result
            if scale == 1.0:
                scale = 0.8
            else:
                scale -= 0.01
        raise ValueError(
            f"无法在保持最小缩放比0.4的情况下排版文本。当前文本：{paragraph.unicode}"
        )
