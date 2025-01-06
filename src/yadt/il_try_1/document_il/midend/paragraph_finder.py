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


class ParagraphFinder:
    def process(self, document):
        for page in document.page:
            self.process_page(page)

    def process_page(self, page: Page):
        paragraphs: [PdfParagraph] = []
        page.pdf_paragraph = paragraphs
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
        for paragraph in paragraphs:
            self.update_paragraph_data(paragraph)

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
