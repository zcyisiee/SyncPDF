import pymupdf

from yadt.document_il import (
    Box,
    PdfCharacter,
    PdfLine,
    PdfParagraph,
    PdfParagraphComposition,
    PdfStyle,
    il_version_1,
)
from yadt.document_il.utils.layout_helper import (
    get_paragraph_length_except,
    get_paragraph_max_height,
    get_paragraph_unicode,
)


class Typesetting:
    def __init__(self, font_path: str):
        self.font = pymupdf.Font(fontfile=font_path)

    def _typeset_pdf_character(
        self,
        char: il_version_1.PdfCharacter,
        current_x: float,
        current_y: float,
        scale: float,
    ) -> tuple[None, float] | tuple[PdfCharacter, float | None]:
        """排版一个已有的 PdfCharacter

        Args:
            char: 原始字符
            current_x: 当前 x 坐标

        Returns:
            新的 PdfCharacter 和新的 x 坐标
        """
        if char.char_unicode == " " and char.pdf_character_id is None:
            return None, current_x + (char.box.x2 - char.box.x) * scale
        new_char = il_version_1.PdfCharacter(
            pdf_character_id=char.pdf_character_id,
            char_unicode=char.char_unicode,
            box=il_version_1.Box(
                x=current_x,
                y=current_y,
                x2=current_x + (char.box.x2 - char.box.x) * scale,
                y2=current_y + (char.box.y2 - char.box.y) * scale,
            ),
            pdf_style=PdfStyle(
                font_id=char.pdf_style.font_id,
                font_size=char.pdf_style.font_size * scale,
                graphic_state=char.pdf_style.graphic_state,
            ),
            scale=scale,
            vertical=char.vertical,
            advance=char.advance * scale,
        )
        return new_char, new_char.box.x2

    def _typeset_unicode_char(
        self,
        char_unicode: str,
        x: float,
        y: float,
        font_size: float,
        font: pymupdf.Font,
        scale: float,
    ) -> tuple[il_version_1.PdfCharacter, float]:
        """排版一个 Unicode 字符

        Args:
            char_unicode: Unicode 字符
            x: x 坐标
            y: y 坐标
            font_size: 缩放前的字体大小
            font: 字体
            scale: 缩放比例
            style: 图形状态

        Returns:
            新的 PdfCharacter 和新的 x 坐标
        """
        font_size = font_size * scale
        char_width = font.char_lengths(char_unicode, font_size)[0]
        new_char = il_version_1.PdfCharacter(
            pdf_character_id=font.has_glyph(ord(char_unicode)),
            char_unicode=char_unicode,
            box=il_version_1.Box(
                x=x,
                y=y,
                x2=x + char_width,
                y2=y + font_size,
            ),
            pdf_style=PdfStyle(
                font_id="noto",
                font_size=font_size,
            ),
            scale=scale,
            vertical=False,
            advance=char_width,
        )
        return new_char, new_char.box.x2

    def _typeset_formula(
        self,
        formula: il_version_1.PdfFormula,
        noto_font: pymupdf.Font,
        x: float,
        y: float,
        scale: float,
    ) -> tuple[list[il_version_1.PdfCharacter], float]:
        """处理公式，保持公式内部字符的相对位置关系，整体缩放后放置到新位置

        Args:
            formula: 原始公式
            noto_font: 字体
            current_font_size: 当前字体大小
            x: 目标位置左下角x坐标
            y: 目标位置左下角y坐标
            scale: 缩放比例

        Returns:
            处理后的 PdfCharacter 列表
        """
        result_chars = []

        # 计算原始公式的边界框
        min_x = min(char.box.x for char in formula.pdf_character)
        min_y = min(char.box.y for char in formula.pdf_character)

        # 对每个字符进行缩放和位置调整
        for char in formula.pdf_character:
            if char.pdf_character_id is None:
                continue
            # 计算字符相对于公式左下角的偏移量
            relative_x = char.box.x - min_x
            relative_y = char.box.y - min_y  # PDF坐标系中y轴从下往上
            relative_width = char.box.x2 - char.box.x
            relative_height = char.box.y2 - char.box.y

            # 计算缩放后的新位置和大小
            new_x = x + relative_x * scale + formula.x_offset
            new_y = y + relative_y * scale + formula.y_offset
            new_width = relative_width * scale
            new_height = relative_height * scale

            # 创建新的字符对象
            new_char = il_version_1.PdfCharacter(
                char_unicode=char.char_unicode,
                box=il_version_1.Box(
                    x=new_x,
                    y=new_y,
                    x2=new_x + new_width,
                    y2=new_y + new_height,
                ),
                pdf_style=char.pdf_style,
                vertical=char.vertical,
                scale=scale,
                advance=char.advance,
                pdf_character_id=char.pdf_character_id,
            )
            result_chars.append(new_char)

        return result_chars, result_chars[-1].box.x2

    def process_toc_dots(
        self,
        paragraph: PdfParagraph,
        noto_font: pymupdf.Font,
        current_font_size: float,
        max_width: float,
        scale: float,
    ) -> list[il_version_1.PdfCharacter] | None:
        """处理目录条目的点号

        Args:
            paragraph: 原始段落
            noto_font: 字体
            current_font_size: 当前字体大小
            max_width: 最大可用宽度

        Returns:
            处理后的 PdfCharacter 列表，如果点号数量不足则返回 None
        """
        # 分割文本为标题和页码部分
        text = get_paragraph_unicode(paragraph)
        import re

        match = re.match(r"^(.*?)\s*\.+\s*(\d+)\s*$", text.strip())
        if not match:
            return None

        title, page_num = match.groups()
        title = title.strip()
        page_num = page_num.strip()

        # 计算除了点号以外的内容长度
        length_except_dots = get_paragraph_length_except(paragraph, ".", noto_font)

        # 计算点号的宽度
        dot_width = noto_font.char_lengths(".", current_font_size)[0]

        # 计算需要的点号数量
        dots_needed = max(1, int((max_width - length_except_dots) / dot_width))

        # 如果点号数量太少，返回 None
        if dots_needed < 5:
            return None

        result_chars = []
        current_x = 0

        # 复制标题部分的 PdfCharacter，遇到点号就停止
        for comp in paragraph.pdf_paragraph_composition:
            if comp.pdf_character:
                char = comp.pdf_character
                if char.char_unicode == ".":
                    break
                new_char, current_x = self._typeset_pdf_character(
                    char, current_x, paragraph.box.y, scale
                )
                if new_char:
                    result_chars.append(new_char)
            elif comp.pdf_same_style_characters:
                should_break = False
                for char in comp.pdf_same_style_characters.pdf_character:
                    if char.char_unicode == ".":
                        should_break = True
                        break
                    new_char, current_x = self._typeset_pdf_character(
                        char, current_x, paragraph.box.y, scale
                    )
                    if new_char:
                        result_chars.append(new_char)
                if should_break:
                    break
            elif comp.pdf_same_style_unicode_characters:
                should_break = False
                for char_unicode in comp.pdf_same_style_unicode_characters.unicode:
                    if char_unicode == ".":
                        should_break = True
                        break
                    new_char, current_x = self._typeset_unicode_char(
                        char_unicode,
                        current_x,
                        paragraph.box.y,
                        current_font_size,
                        noto_font,
                        scale,
                    )
                    result_chars.append(new_char)
                if should_break:
                    break
            elif comp.pdf_line:
                for char in comp.pdf_line.pdf_character:
                    if char.char_unicode == ".":
                        break
                    new_char, current_x = self._typeset_pdf_character(
                        char, current_x, paragraph.box.y, scale
                    )
                    if new_char:
                        result_chars.append(new_char)
            elif comp.pdf_formula:
                chars, current_x = self._typeset_formula(
                    comp.pdf_formula,
                    noto_font,
                    current_x,
                    paragraph.box.y,
                    scale,
                )
                result_chars.extend(chars)
            else:
                raise Exception(
                    "Unexpected composition type"
                    " in PdfParagraphComposition. "
                    "This type only appears in the IL "
                    "after the translation is completed."
                )

        # 添加点号
        for _ in range(dots_needed):
            new_char, current_x = self._typeset_unicode_char(
                ".", current_x, paragraph.box.y, current_font_size, noto_font, scale
            )
            result_chars.append(new_char)

        # TODO:想办法也透明传输页码？
        # 添加页码
        for char in page_num:
            new_char, current_x = self._typeset_unicode_char(
                char, current_x, paragraph.box.y, current_font_size, noto_font, scale
            )
            result_chars.append(new_char)

        return result_chars

    def try_typeset(
        self,
        paragraph: il_version_1.PdfParagraph,
        noto_font: pymupdf.Font,
        box: il_version_1.Box,
        scale: float,
    ) -> list[il_version_1.PdfCharacter] | None:
        """尝试对段落进行排版

        Args:
            paragraph: 要排版的段落
            noto_font: 字体
            box: 可用区域
            scale: 缩放比例

        Returns:
            排版后的字符列表，如果无法排版则返回 None
        """
        paragraph_max_height = get_paragraph_max_height(paragraph)
        result_chars = []
        current_x = box.x
        current_y = box.y2 - paragraph_max_height  # 从顶部开始排版
        line_height = paragraph_max_height * 1.4  # 行高为字体大小的1.4倍

        # 遍历段落中的所有组成部分
        for comp in paragraph.pdf_paragraph_composition:
            if comp is None:
                continue
            if comp.pdf_character:
                char = comp.pdf_character
                char_width = char.box.x2 - char.box.x
                # 检查是否需要换行
                if current_x + char_width * scale > box.x2:
                    current_x = box.x
                    current_y -= line_height
                    # 检查是否超出底部边界
                    if current_y < box.y:
                        return None
                new_char, current_x = self._typeset_pdf_character(
                    char, current_x, current_y, scale
                )
                if new_char:
                    result_chars.append(new_char)
            elif comp.pdf_same_style_characters:
                for char in comp.pdf_same_style_characters.pdf_character:
                    char_width = char.box.x2 - char.box.x
                    if current_x + char_width * scale > box.x2:
                        current_x = box.x
                        current_y -= line_height
                        if current_y < box.y:
                            return None
                    new_char, current_x = self._typeset_pdf_character(
                        char, current_x, current_y, scale
                    )
                    if new_char:
                        result_chars.append(new_char)
            elif comp.pdf_same_style_unicode_characters:
                for char_unicode in comp.pdf_same_style_unicode_characters.unicode:
                    font_size = (
                        comp.pdf_same_style_unicode_characters.pdf_style.font_size
                        * scale
                    )
                    char_width = noto_font.char_lengths(char_unicode, font_size)[0]
                    if current_x + char_width * scale > box.x2:
                        current_x = box.x
                        current_y -= line_height
                        if current_y < box.y:
                            return None
                    new_char, current_x = self._typeset_unicode_char(
                        char_unicode,
                        current_x,
                        current_y,
                        font_size,
                        noto_font,
                        scale,
                    )
                    result_chars.append(new_char)
            elif comp.pdf_line:
                for char in comp.pdf_line.pdf_character:
                    char_width = char.box.x2 - char.box.x
                    if current_x + char_width * scale > box.x2:
                        current_x = box.x
                        current_y -= line_height
                        if current_y < box.y:
                            return None
                    new_char, current_x = self._typeset_pdf_character(
                        char, current_x, current_y, scale
                    )
                    if new_char:
                        result_chars.append(new_char)
            elif comp.pdf_formula:
                # 计算公式宽度
                formula_width = (
                    comp.pdf_formula.box.x2 - comp.pdf_formula.box.x
                ) * scale
                # 如果公式太宽，换到下一行
                if current_x + formula_width > box.x2:
                    current_x = box.x
                    current_y -= line_height
                    if current_y < box.y:
                        return None
                chars, current_x = self._typeset_formula(
                    comp.pdf_formula, noto_font, current_x, current_y, scale
                )
                result_chars.extend(chars)
            else:
                raise Exception(
                    "Unexpected composition type"
                    " in PdfParagraphComposition. "
                    "This type only appears in the IL "
                    "after the translation is completed."
                )

        return result_chars

    def get_max_right_space(self, current_box: Box, page) -> float:
        """获取段落右侧最大可用空间

        Args:
            current_box: 当前段落的边界框
            page: 当前页面

        Returns:
            可以扩展到的最大 x 坐标
        """
        # TODO: try to find right margin of page
        # 获取页面的裁剪框作为初始最大限制
        max_x = page.cropbox.box.x2 * 0.9

        # 检查所有可能的阻挡元素
        for para in page.pdf_paragraph:
            if para.box == current_box:  # 跳过当前段落
                continue
            # 只考虑在当前段落右侧且有垂直重叠的元素
            if para.box.x > current_box.x and not (
                para.box.y >= current_box.y2 or para.box.y2 <= current_box.y
            ):
                max_x = min(max_x, para.box.x)

        # 检查图形
        for figure in page.pdf_figure:
            if figure.box.x > current_box.x and not (
                figure.box.y >= current_box.y2 or figure.box.y2 <= current_box.y
            ):
                max_x = min(max_x, figure.box.x)

        return max_x

    def typsetting_document(self, document: il_version_1.Document):
        for page in document.page:
            # 开始实际的渲染过程
            for paragraph in page.pdf_paragraph:
                try:
                    self.render_paragraph_unicode_to_char(paragraph, self.font, 0.67)
                except ValueError:
                    # 获取段落当前的边界框
                    current_box = paragraph.box
                    # 获取右侧最大可用空间
                    max_x = self.get_max_right_space(current_box, page)
                    # 只有当有额外空间时才扩展
                    if max_x > current_box.x2:
                        expanded_box = Box(
                            x=current_box.x,
                            y=current_box.y,
                            x2=max_x,  # 直接扩展到最大可用位置
                            y2=current_box.y2,
                        )
                        # 更新段落的边界框
                        paragraph.box = expanded_box

                        # 重新渲染
                        self.render_paragraph_unicode_to_char(paragraph, self.font, 0.1)

    def render_paragraph_unicode_to_char(
        self,
        paragraph: il_version_1.PdfParagraph,
        noto_font: pymupdf.Font,
        scale_threshold,
    ):
        if not paragraph.pdf_paragraph_composition:
            return
        scale = 1.0
        # 尝试排版，如果失败则逐步缩小字号
        while scale >= scale_threshold:
            result = self.try_typeset(paragraph, noto_font, paragraph.box, scale)
            if result is not None:
                paragraph.pdf_paragraph_composition = [
                    PdfParagraphComposition(pdf_character=char) for char in result
                ]
                paragraph.scale = scale
                return
            if scale == 1.0:
                scale = 0.8
            else:
                scale -= 0.01
        raise ValueError(
            f"无法在保持最小缩放比 {scale_threshold} 的情况下排版文本。当前文本：{paragraph.unicode}"
        )
