import os.path
import re

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

        self.fontid2font = {f.font_id: f for f in self.fonts.values()}
        self.fontid2font["base"] = self.base_font
        self.fontid2font["fallback"] = self.fallback_font
        self.fontid2font["kai"] = self.kai_font
    def has_char(self, char_unicode: str):
        if len(char_unicode) != 1:
            return False
        current_char = ord(char_unicode)
        for font in self.fonts.values():
            if font.has_glyph(current_char):
                return True
        if self.base_font.has_glyph(current_char):
            return True
        if self.fallback_font.has_glyph(current_char):
            return True
        return False
    def map(self, original_font: PdfFont, char_unicode: str):
        current_char = ord(char_unicode)
        if isinstance(original_font, pymupdf.Font):
            bold = original_font.is_bold
            italic = original_font.is_italic
            monospaced = original_font.is_monospaced
            serif = original_font.is_serif
        elif isinstance(original_font, PdfFont):
            bold = original_font.bold
            italic = original_font.italic
            monospaced = original_font.monospace
            serif = original_font.serif
        else:
            raise Exception(f"Unknown font type: {type(original_font)}")
        if italic and self.kai_font.has_glyph(current_char):
            return self.kai_font
        for k, font in self.fonts.items():
            if not font.has_glyph(current_char):
                continue
            if bold != font.is_bold:
                continue
            # 不知道什么原因，思源黑体的 serif 属性为1，先workaround
            if serif == 1 and "serif" not in font.font_id:
                continue
            if serif == 0 and "serif" in font.font_id:
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
                    os.path.basename(file_name)
                    .split(".")[0]
                    .replace("-", "")
                    .lower(),
                    get_cache_file_path(file_name),
                )
                for file_name in self.font_names
            ]
        )
        font_id = {}

        for font in font_list:
            font_id[font[0]] = doc_zh[0].insert_font(font[0], font[1])
        xreflen = doc_zh.xref_length()
        with self.translation_config.progress_monitor.stage_start(
            self.stage_name, xreflen - 1
        ) as pbar:
            for xref in range(1, xreflen):
                pbar.advance(1)
                for label in ["Resources/", ""]:  # 可能是基于 xobj 的 res
                    try:  # xref 读写可能出错
                        font_res = doc_zh.xref_get_key(xref, f"{label}Font")
                        if font_res[0] == 'xref':
                            resource_xref_id = re.search("(\\d+) 0 R", font_res[1]).group(1)
                            xref = int(resource_xref_id)
                            font_res = doc_zh.xref_object(xref)
                            for font in font_list:
                                font_exist = doc_zh.xref_get_key(
                                    xref, f"{font[0]}"
                                )
                                if font_exist[0] == "null":
                                    doc_zh.xref_set_key(
                                        xref,
                                        f"{font[0]}",
                                        f"{font_id[font[0]]} 0 R",
                                    )
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
                for xobj in page.pdf_xobject:
                    xobj.pdf_font.append(pdf_font_il)
