import functools
import logging
import re
from pathlib import Path

import pymupdf
from yadt.const import get_cache_file_path
from yadt.document_il import PdfFont
from yadt.document_il import il_version_1
from yadt.translation_config import TranslationConfig

logger = logging.getLogger(__name__)


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
            Path(file_name).name.split(".")[0].replace("-", "").lower(): pymupdf.Font(
                fontfile=str(get_cache_file_path(file_name)),
            )
            for file_name in self.font_names
        }
        for k, v in self.fonts.items():
            v.font_id = k
        self.translation_config = translation_config
        self.base_font_path = translation_config.font
        self.fallback_font_path = get_cache_file_path("noto.ttf")
        self.base_font = pymupdf.Font(fontfile=str(self.base_font_path))
        self.fallback_font = pymupdf.Font(fontfile=str(self.fallback_font_path))

        self.kai_font_path = get_cache_file_path("LXGWWenKai-Regular.ttf")
        self.kai_font = pymupdf.Font(fontfile=str(self.kai_font_path))

        self.base_font.font_id = "base"
        self.fallback_font.font_id = "fallback"
        self.kai_font.font_id = "kai"

        # Set ascent and descent for base font
        self.base_font.ascent_fontmap = 1151
        self.base_font.descent_fontmap = -286

        # Set ascent and descent for fallback font
        self.fallback_font.ascent_fontmap = 1069
        self.fallback_font.descent_fontmap = -293

        # Set ascent and descent for kai font
        self.kai_font.ascent_fontmap = 928
        self.kai_font.descent_fontmap = -256

        self.fontid2font = {f.font_id: f for f in self.fonts.values()}
        self.fontid2font["base"] = self.base_font
        self.fontid2font["fallback"] = self.fallback_font
        self.fontid2font["kai"] = self.kai_font

        # Set ascent and descent for other fonts
        font_metrics = {
            "sourcehanserifcn": (1151, 0),
            "sourcehansansscregular": (1160, -288),
            "sourcehanserifcnbold": (1151, -286),
            "sourcehansansscbold": (1160, -288),
        }

        for font_id, (ascent, descent) in font_metrics.items():
            if font_id in self.fontid2font:
                self.fontid2font[font_id].ascent_fontmap = ascent
                self.fontid2font[font_id].descent_fontmap = descent

        for font in self.fontid2font.values():
            font.char_lengths = functools.lru_cache(maxsize=10240, typed=True)(
                font.char_lengths,
            )

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
            logger.error(
                f"Unknown font type: {type(original_font)}. "
                f"Original font: {original_font}. "
                f"Char unicode: {char_unicode}. ",
            )
            return None
        if italic and self.kai_font.has_glyph(current_char):
            return self.kai_font
        for _k, font in self.fonts.items():
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

        logger.error(
            f"Can't find font for {char_unicode}({current_char}). "
            f"Original font: {original_font}. "
            f"Char unicode: {char_unicode}. ",
        )
        return None

    def add_font(self, doc_zh: pymupdf.Document, il: il_version_1.Document):
        font_list = [
            ("base", self.base_font_path),
            ("fallback", self.fallback_font_path),
            ("kai", self.kai_font_path),
        ]
        font_list.extend(
            [
                (
                    Path(file_name).name.split(".")[0].replace("-", "").lower(),
                    get_cache_file_path(file_name),
                )
                for file_name in self.font_names
            ],
        )
        font_id = {}
        xreflen = doc_zh.xref_length()
        with self.translation_config.progress_monitor.stage_start(
            self.stage_name,
            xreflen - 1 + len(font_list) + len(il.page) + len(font_list),
        ) as pbar:
            for font in font_list:
                font_id[font[0]] = doc_zh[0].insert_font(font[0], font[1])
                pbar.advance(1)
            for xref in range(1, xreflen):
                pbar.advance(1)
                for label in ["Resources/", ""]:  # 可能是基于 xobj 的 res
                    try:  # xref 读写可能出错
                        font_res = doc_zh.xref_get_key(xref, f"{label}Font")
                        if font_res is None:
                            continue
                        target_key_prefix = f"{label}Font/"
                        if font_res[0] == "xref":
                            resource_xref_id = re.search(
                                "(\\d+) 0 R",
                                font_res[1],
                            ).group(1)
                            xref = int(resource_xref_id)
                            font_res = ("dict", doc_zh.xref_object(xref))
                            target_key_prefix = ""
                        if font_res[0] == "dict":
                            for font in font_list:
                                target_key = f"{target_key_prefix}{font[0]}"
                                font_exist = doc_zh.xref_get_key(xref, target_key)
                                if font_exist[0] == "null":
                                    doc_zh.xref_set_key(
                                        xref,
                                        target_key,
                                        f"{font_id[font[0]]} 0 R",
                                    )
                    except Exception:
                        pass

            # Create PdfFont for each font
            # 预先创建所有字体对象
            pdf_fonts = []
            for font_name, font_path in font_list:
                font = pymupdf.Font(fontfile=str(font_path))
                # Get descent_fontmap from fontid2font
                descent_fontmap = None
                if font_name in self.fontid2font:
                    mupdf_font = self.fontid2font[font_name]
                    if hasattr(mupdf_font, "descent_fontmap"):
                        descent_fontmap = mupdf_font.descent_fontmap
                    if hasattr(mupdf_font, "ascent_fontmap"):
                        ascent_fontmap = mupdf_font.ascent_fontmap

                pdf_fonts.append(
                    il_version_1.PdfFont(
                        name=font_name,
                        xref_id=font_id[font_name],
                        font_id=font_name,
                        encoding_length=2,
                        bold=font.is_bold,
                        italic=font.is_italic,
                        monospace=font.is_monospaced,
                        serif=font.is_serif,
                        descent=descent_fontmap,
                        ascent=ascent_fontmap,
                    ),
                )
                pbar.advance(1)

            # 批量添加字体到页面和XObject
            for page in il.page:
                page.pdf_font.extend(pdf_fonts)
                for xobj in page.pdf_xobject:
                    xobj.pdf_font.extend(pdf_fonts)
                pbar.advance(1)
