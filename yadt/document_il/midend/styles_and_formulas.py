import base64
import re
import unicodedata

from yadt.document_il.il_version_1 import Page, Document
from yadt.translation_config import TranslationConfig


class StylesAndFormulas:
    def __init__(self, translation_config: TranslationConfig):
        self.translation_config = translation_config

    def process(self, document: Document):
        for page in document.page:
            self.process_page(page)

    def process_page(self, page: Page):
        pass

    def is_formulas_font(self, font_name: str) -> bool:
        if self.translation_config.formular_font_pattern:
            pattern = self.translation_config.formular_font_pattern
        else:
            pattern = r"(CM[^R]|(MS|XY|MT|BL|RM|EU|LA|RS)[A-Z]|LINE|LCIRCLE|TeX-|rsfs|txsy|wasy|stmary|.*Mono|.*Code|.*Ital|.*Sym|.*Math)"

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
