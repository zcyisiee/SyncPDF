import base64
import re
import unicodedata

from yadt.document_il.il_version_1 import (
    Box,
    Document,
    Page,
    PdfFormula,
    PdfLine,
    PdfParagraphComposition,
    PdfCharacter,
)
from yadt.translation_config import TranslationConfig
from yadt.document_il.utils.layout_helper import get_char_unicode_string


class StylesAndFormulas:
    def __init__(self, translation_config: TranslationConfig):
        self.translation_config = translation_config

    def process(self, document: Document):
        for page in document.page:
            self.process_page(page)

    def update_line_data(self, line: PdfLine):
        min_x = min(char.box.x for char in line.pdf_character)
        min_y = min(char.box.y for char in line.pdf_character)
        max_x = max(char.box.x2 for char in line.pdf_character)
        max_y = max(char.box.y2 for char in line.pdf_character)
        line.box = Box(min_x, min_y, max_x, max_y)

    def process_page(self, page: Page):
        if not page.pdf_paragraph:
            return

        # 收集该页所有的公式字体ID
        formula_font_ids = set()
        for font in page.pdf_font:
            if self.is_formulas_font(font.name):
                formula_font_ids.add(font.font_id)

        for paragraph in page.pdf_paragraph:
            if not paragraph.pdf_paragraph_composition:
                continue

            new_compositions = []
            current_chars = []
            is_current_formula = False  # 当前是否在处理公式字符

            for composition in paragraph.pdf_paragraph_composition:
                if not composition.pdf_line:
                    if current_chars:
                        # 处理剩余字符
                        new_compositions.append(
                            self.create_composition(
                                current_chars, is_current_formula)
                        )
                        current_chars = []
                    new_compositions.append(composition)
                    continue

                line = composition.pdf_line
                for char in line.pdf_character:
                    is_formula = (
                        self.is_formulas_char(char.char_unicode)        # 公式字符
                        or char.pdf_font_id in formula_font_ids         # 公式字体
                        or char.vertical                                # 垂直字体
                        or (
                            current_chars
                            and not get_char_unicode_string(current_chars).isspace()
                            # 角标字体，有 0.76 的角标和 0.799 的大写，这里用 0.79 取中，同时考虑首字母放大的情况
                            and char.size < current_chars[-1].size * 0.79
                        )
                    )

                    if is_formula != is_current_formula and current_chars:
                        # 字符类型发生切换，处理之前的字符
                        new_compositions.append(
                            self.create_composition(
                                current_chars, is_current_formula)
                        )
                        current_chars = []
                        is_current_formula = is_formula

                    current_chars.append(char)

                # 处理行末的字符
                if current_chars:
                    new_compositions.append(
                        self.create_composition(
                            current_chars, is_current_formula)
                    )
                    current_chars = []

            paragraph.pdf_paragraph_composition = new_compositions

    def create_composition(
        self, chars: list[PdfCharacter], is_formula: bool
    ) -> PdfParagraphComposition:
        if is_formula:
            formula = PdfFormula(pdf_character=chars)
            self.update_formula_data(formula)
            return PdfParagraphComposition(pdf_formula=formula)
        else:
            new_line = PdfLine(pdf_character=chars)
            self.update_line_data(new_line)
            return PdfParagraphComposition(pdf_line=new_line)

    def update_formula_data(self, formula: PdfFormula):
        min_x = min(char.box.x for char in formula.pdf_character)
        min_y = min(char.box.y for char in formula.pdf_character)
        max_x = max(char.box.x2 for char in formula.pdf_character)
        max_y = max(char.box.y2 for char in formula.pdf_character)
        formula.box = Box(min_x, min_y, max_x, max_y)

    def is_formulas_font(self, font_name: str) -> bool:
        if self.translation_config.formular_font_pattern:
            pattern = self.translation_config.formular_font_pattern
        else:
            pattern = (r"(CM[^RB]"
                       r"|(MS|XY|MT|BL|RM|EU|LA|RS)[A-Z]"
                       r"|LINE"
                       r"|LCIRCLE"
                       r"|TeX-"
                       r"|rsfs"
                       r"|txsy"
                       r"|wasy"
                       r"|stmary"
                       r"|.*Mono"
                       r"|.*Code"
                       r"|.*Ital"
                       r"|.*Sym"
                       r"|.*Math"
                       r")")

        if font_name.startswith("BASE64:"):
            font_name_bytes = base64.b64decode(font_name[7:])
            font = font_name_bytes.split(b"+")[-1]
            pattern = pattern.encode()
        else:
            font = font_name.split("+")[-1]

        if re.match(pattern, font):
            return True

        return False

    def is_formulas_char(self, char: str) -> bool:
        if self.translation_config.formular_char_pattern:
            pattern = self.translation_config.formular_char_pattern
            if re.match(pattern, char):
                return True
        if (
            char
            and char != " "  # 非空格
            and (
                unicodedata.category(char[0])
                in [
                    "Lm",
                    "Mn",
                    "Sk",
                    "Sm",
                    "Zl",
                    "Zp",
                    "Zs",
                ]  # 文字修饰符、数学符号、分隔符号
                or ord(char[0]) in range(0x370, 0x400)  # 希腊字母
            )
        ):
            return True

        return False
