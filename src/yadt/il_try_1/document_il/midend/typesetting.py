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
        )
        return line

    def typsetting_document(self, document: il_try_1.Document):
        for page in document.page:
            for paragraph in page.pdf_paragraph:
                paragraph.pdf_line = [
                    self.create_line(
                        self.render_paragraph_unicode_to_char(paragraph, noto),
                    )
                ]

    def render_paragraph_unicode_to_char(self, paragraph: il_try_1.PdfParagraph, noto_font: pymupdf.Font) -> list[il_try_1.PdfCharacter]:
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
                current_y -= font_size * 1.2  # 使用1.2倍行距

            # 创建字符的边界框
            char_box = il_try_1.Box(
                x=current_x,
                y=current_y,
                x2=current_x + char_width,
                y2=current_y + font_size,
            )

            # 创建PdfCharacter对象
            pdf_char = il_try_1.PdfCharacter(
                pdf_font_id="noto",
                pdf_character_id=noto_font.has_glyph(ord(char)),
                char_unicode=char,
                box=char_box,
                size=font_size,
                graphic_state=paragraph.graphic_state,
            )

            # 添加到结果列表
            chars.append(pdf_char)

            # 更新下一个字符的起始x坐标
            current_x += char_width

        return chars
