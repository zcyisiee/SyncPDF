import os.path

import pymupdf

from yadt.const import get_cache_file_path
from yadt.document_il import PdfFont, il_version_1
from yadt.translation_config import TranslationConfig


class FontMapper:
    stage_name = "添加字体"

    def __init__(self, translation_config: TranslationConfig):
        self.font_names = [
            "source-han-serif-cn.ttf",
            "SourceHanSansSC-Regular.ttf",
            "source-han-serif-cn-bold.ttf",
            "SourceHanSansSC-Bold.ttf",
        ]
        self.fonts = {
            os.path.basename(file_name)
            .split(".")[0]
            .replace("-", "")
            .lower(): pymupdf.Font(fontfile=get_cache_file_path(file_name))
            for file_name in self.font_names
        }
        for k, v in self.fonts.items():
            v.font_id = k
        self.translation_config = translation_config
        self.base_font_path = translation_config.font
        self.fallback_font_path = get_cache_file_path("noto.ttf")
        self.base_font = pymupdf.Font(fontfile=self.base_font_path)
        self.fallback_font = pymupdf.Font(fontfile=self.fallback_font_path)

        self.kai_font_path = get_cache_file_path("LXGWWenKai-Regular.ttf")
        self.kai_font = pymupdf.Font(fontfile=self.kai_font_path)

        self.base_font.font_id = "base"
        self.fallback_font.font_id = "fallback"
        self.kai_font.font_id = "kai"

    def map(self, original_font: PdfFont, char_unicode: str):
        current_char = ord(char_unicode)
        if original_font.italic and self.kai_font.has_glyph(current_char):
            return self.kai_font
        for k, font in self.fonts.items():
            if not font.has_glyph(current_char):
                continue
            if original_font.bold != font.is_bold:
                continue
            # 不知道什么原因，思源黑体的 serif 属性为1，先workaround
            if original_font.serif == 1 and 'serif' not in font.font_id:
                continue
            if original_font.serif == 0 and 'serif' in font.font_id:
                continue
            return font
        if self.base_font.has_glyph(current_char):
            return self.base_font

        if self.fallback_font.has_glyph(current_char):
            return self.fallback_font

        raise Exception(f"Can't find font for {char_unicode}({current_char})")

    def add_font(self, doc_zh: pymupdf.Document, il: il_version_1.Document):
        font_list = [
            ("base", self.base_font_path),
            ("fallback", self.fallback_font_path),
            ("kai", self.kai_font_path),
        ]
        font_list.extend(
            [
                (
                    os.path.basename(file_name).split(".")[0].replace("-", "").lower(),
                    get_cache_file_path(file_name),
                )
                for file_name in self.font_names
            ]
        )
        font_id = {}
        with self.translation_config.progress_monitor.stage_start(
            self.stage_name, len(doc_zh)
        ) as pbar:
            for i, page in enumerate(doc_zh):
                if not self.translation_config.should_translate_page(i + 1):
                    pbar.advance()
                    continue
                for font in font_list:
                    font_id[font[0]] = page.insert_font(font[0], font[1])
                pbar.advance()
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

        # Create PdfFont for each font
        for page in il.page:
            for font_name, font_path in font_list:
                font = pymupdf.Font(fontfile=font_path)
                pdf_font_il = il_version_1.PdfFont(
                    name=font_name,
                    xref_id=font_id[font_name],
                    font_id=font_name,
                    encoding_length=2,
                    bold=font.is_bold,
                    italic=font.is_italic,
                    monospace=font.is_monospaced,
                    serif=font.is_serif,
                )
                page.pdf_font.append(pdf_font_il)
