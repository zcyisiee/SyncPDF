from yadt.il_try_1.document_il.il_try_1 import (
    Box,
    Page,
    PdfCharacter,
    PdfParagraph,
)


class Layout:
    def __init__(self, id, name):
        self.id = id
        self.name = name

    @staticmethod
    def is_newline(prev_char: PdfCharacter, curr_char: PdfCharacter) -> bool:
        # 如果没有前一个字符，不是换行
        if prev_char is None:
            return False

        # 获取两个字符的中心y坐标
        prev_y = (prev_char.box.y + prev_char.box.y2) / 2
        curr_y = (curr_char.box.y + curr_char.box.y2) / 2

        # 如果当前字符的y坐标明显低于前一个字符，说明换行了
        # 这里使用字符高度的一半作为阈值
        char_height = curr_char.box.y2 - curr_char.box.y
        return curr_y < prev_y - char_height / 2


class ParagraphFinder:
    def process(self, document):
        for page in document.page:
            self.process_page(page)

    def process_page(self, page: Page):
        # 第一步：根据layout创建paragraphs
        # 在这一步中，page.pdf_character中的字符会被移除
        paragraphs = self.create_paragraphs(page)
        page.pdf_paragraph = paragraphs
        
        # 第二步：处理段落中的空格和换行符
        for paragraph in paragraphs:
            self.process_paragraph_spacing(paragraph)
            self.update_paragraph_data(paragraph)

    def create_paragraphs(self, page: Page) -> list[PdfParagraph]:
        paragraphs: list[PdfParagraph] = []
        current_paragraph: PdfParagraph | None = None
        current_layout: Layout | None = None
        chars = page.pdf_character.copy()
        
        for char in chars:
            char_layout = self.get_layout(char, page)
            if not self.is_text_layout(char_layout):
                continue

            page.pdf_character.remove(char)
            if current_paragraph is None:
                current_paragraph = PdfParagraph(pdf_character=[char])
                current_layout = char_layout
                paragraphs.append(current_paragraph)
            else:
                if char_layout.id == current_layout.id:
                    current_paragraph.pdf_character.append(char)
                else:
                    current_paragraph = PdfParagraph(pdf_character=[char])
                    current_layout = char_layout
                    paragraphs.append(current_paragraph)

            self.update_paragraph_box(current_paragraph)
            
        return paragraphs

    def process_paragraph_spacing(self, paragraph: PdfParagraph):
        if not paragraph.pdf_character:
            return

        processed_chars = []
        for i in range(len(paragraph.pdf_character) - 1):
            current_char = paragraph.pdf_character[i]
            next_char = paragraph.pdf_character[i + 1]

            # 检查当前字符是否是换行
            if Layout.is_newline(current_char, next_char):
                # 如果当前字符是空格，跳过它
                if not current_char.char_unicode.isspace():
                    processed_chars.append(current_char)
            else:
                processed_chars.append(current_char)

        # 添加最后一个字符
        processed_chars.append(paragraph.pdf_character[-1])
        paragraph.pdf_character = processed_chars

    def update_paragraph_data(self, paragraph: PdfParagraph):
        paragraph.unicode = "".join(
            char.char_unicode for char in paragraph.pdf_character
        )
        self.update_paragraph_box(paragraph)

    def update_paragraph_box(self, paragraph: PdfParagraph):
        min_x = min(char.box.x for char in paragraph.pdf_character)
        min_y = min(char.box.y for char in paragraph.pdf_character)
        max_x = max(char.box.x2 for char in paragraph.pdf_character)
        max_y = max(char.box.y2 for char in paragraph.pdf_character)
        paragraph.box = Box(min_x, min_y, max_x, max_y)

    def is_text_layout(self, layout: Layout):
        return layout is not None and layout.name in [
            "plain text",
            "title",
            "abandon",
        ]

    def get_layout(
        self,
        char: PdfCharacter,
        page: Page,
    ):
        # current layouts
        # {
        #     "title",
        #     "plain text",
        #     "abandon",
        #     "figure",
        #     "figure_caption",
        #     "table",
        #     "table_caption",
        #     "table_footnote",
        #     "isolate_formula",
        #     "formula_caption",
        # }
        layout_priority = [
            "formula_caption",
            "isolate_formula",
            "table_footnote",
            "table_caption",
            "table",
            "figure_caption",
            "figure",
            "abandon",
            "plain text",
            "title",
        ]
        char_box = char.box
        char_x = (char_box.x + char_box.x2) / 2
        char_y = (char_box.y + char_box.y2) / 2

        # 按照优先级顺序检查每种布局
        matching_layouts = {}
        for layout in page.page_layout:
            layout_box = layout.box
            if (
                layout_box.x <= char_x <= layout_box.x2
                and layout_box.y <= char_y <= layout_box.y2
            ):
                matching_layouts[layout.class_name] = Layout(
                    layout.id, layout.class_name
                )

        # 按照优先级返回最高优先级的布局
        for layout_name in layout_priority:
            if layout_name in matching_layouts:
                return matching_layouts[layout_name]

        return None
