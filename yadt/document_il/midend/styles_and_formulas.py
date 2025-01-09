import base64
import re
import unicodedata

from yadt.document_il.il_version_1 import (
    Box,
    Document,
    Page,
    PdfCharacter,
    PdfFormula,
    PdfLine,
    PdfParagraphComposition,
)
from yadt.document_il.utils.layout_helper import get_char_unicode_string
from yadt.translation_config import TranslationConfig


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

    def process_page_formulas(self, page: Page):
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
                            self.create_composition(current_chars, is_current_formula)
                        )
                        current_chars = []
                    new_compositions.append(composition)
                    continue

                line = composition.pdf_line
                for char in line.pdf_character:
                    is_formula = (
                        self.is_formulas_char(char.char_unicode)  # 公式字符
                        or char.pdf_font_id in formula_font_ids  # 公式字体
                        or char.vertical  # 垂直字体
                        or (
                            len(current_chars) > 0
                            and not get_char_unicode_string(current_chars).isspace()
                            # 角标字体，有 0.76 的角标和 0.799 的大写，这里用 0.79 取中，同时考虑首字母放大的情况
                            and char.size < current_chars[-1].size * 0.79
                        )
                    )

                    if is_formula != is_current_formula and current_chars:
                        # 字符类型发生切换，处理之前的字符
                        new_compositions.append(
                            self.create_composition(current_chars, is_current_formula)
                        )
                        current_chars = []
                    is_current_formula = is_formula

                    current_chars.append(char)

                # 处理行末的字符
                if current_chars:
                    new_compositions.append(
                        self.create_composition(current_chars, is_current_formula)
                    )
                    current_chars = []

            paragraph.pdf_paragraph_composition = new_compositions

    def process_page_offsets(self, page: Page):
        """计算公式的x和y偏移量"""
        if not page.pdf_paragraph:
            return

        for paragraph in page.pdf_paragraph:
            if not paragraph.pdf_paragraph_composition:
                continue

            # 计算该段落的行间距，用其80%作为容差
            line_spacing = self.calculate_line_spacing(paragraph)
            y_tolerance = line_spacing * 0.8

            for i, composition in enumerate(paragraph.pdf_paragraph_composition):
                if not composition.pdf_formula:
                    continue

                formula = composition.pdf_formula
                left_line = None
                right_line = None

                # 查找左边最近的同一行的文本
                for j in range(i - 1, -1, -1):
                    comp = paragraph.pdf_paragraph_composition[j]
                    if comp.pdf_line:
                        # 检查y坐标是否接近，判断是否在同一行
                        if abs(comp.pdf_line.box.y - formula.box.y) <= y_tolerance:
                            left_line = comp.pdf_line
                            break

                # 查找右边最近的同一行的文本
                for j in range(i + 1, len(paragraph.pdf_paragraph_composition)):
                    comp = paragraph.pdf_paragraph_composition[j]
                    if comp.pdf_line:
                        # 检查y坐标是否接近，判断是否在同一行
                        if abs(comp.pdf_line.box.y - formula.box.y) <= y_tolerance:
                            right_line = comp.pdf_line
                            break

                # 计算x偏移量（相对于左边文本）
                if left_line:
                    formula.x_offset = formula.box.x - left_line.box.x2
                else:
                    formula.x_offset = 0  # 如果左边没有文字，x_offset应该为0

                # 计算y偏移量
                if left_line:
                    # 使用底部坐标计算偏移量
                    formula.y_offset = formula.box.y - left_line.box.y
                elif right_line:
                    formula.y_offset = formula.box.y - right_line.box.y
                else:
                    formula.y_offset = 0

    def calculate_line_spacing(self, paragraph) -> float:
        """计算段落中的平均行间距"""
        if not paragraph.pdf_paragraph_composition:
            return 0.0

        # 收集所有文本行的y坐标
        line_y_positions = []
        for comp in paragraph.pdf_paragraph_composition:
            if comp.pdf_line:
                line_y_positions.append(comp.pdf_line.box.y)

        if len(line_y_positions) < 2:
            return 10.0  # 如果只有一行或没有行，返回一个默认值

        # 计算相邻行之间的y差值
        line_spacings = []
        for i in range(len(line_y_positions) - 1):
            spacing = abs(line_y_positions[i] - line_y_positions[i + 1])
            if spacing > 0:  # 忽略重叠的行
                line_spacings.append(spacing)

        if not line_spacings:
            return 10.0  # 如果没有有效的行间距，返回默认值

        # 使用中位数来避免异常值的影响
        median_spacing = sorted(line_spacings)[len(line_spacings) // 2]
        return median_spacing

    def process_page(self, page: Page):
        """处理页面，包括公式识别和偏移量计算"""
        self.process_page_formulas(page)
        self.process_page_offsets(page)

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
            pattern = (
                r"(CM[^RB]"
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
                r")"
            )

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
