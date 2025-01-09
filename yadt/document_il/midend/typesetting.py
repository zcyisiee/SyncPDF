import pymupdf

from yadt.document_il import (
    Box,
    PdfCharacter,
    PdfLine,
    il_version_1,
    PdfParagraphComposition,
)


class Typesetting:
    def __init__(self, font_path: str):
        self.font = pymupdf.Font(fontfile=font_path)

    def create_line(self, chars: list[PdfCharacter]) -> PdfLine:
        assert chars

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
            size=max_y - min_y,  # 使用行的高度作为 size
            scale=chars[0].scale,
        )
        return line

    def process_toc_dots(
        self,
        text: str,
        noto_font: pymupdf.Font,
        current_font_size: float,
        max_width: float,
    ) -> str | None:
        """处理目录条目的点号

        Args:
            text: 原始文本
            noto_font: 字体
            current_font_size: 当前字体大小
            max_width: 最大可用宽度

        Returns:
            处理后的文本，如果点号数量不足则返回None
        """
        # 分割文本为标题和页码部分
        parts = text.rsplit(" ", 1)
        if len(parts) != 2:
            return None

        title, page_num = parts
        # 计算页码的宽度
        page_num_width = sum(
            noto_font.char_lengths(c, current_font_size)[0] for c in page_num
        )
        # 计算点号的宽度
        dot_width = noto_font.char_lengths(".", current_font_size)[0]
        # 计算标题部分的宽度
        title_width = sum(
            noto_font.char_lengths(c, current_font_size)[0] for c in title
        )

        # 计算需要的点号数量
        dots_needed = max(
            1, int((max_width - title_width - page_num_width) / dot_width)
        )

        # 如果点号数量太少，返回None
        if dots_needed < 5:
            return None

        dots = "." * dots_needed
        # 重新组合文本
        return f"{title}{dots}{page_num}"

    def try_typeset(
        self,
        scale: float,
        noto_font: pymupdf.Font,
        paragraph: il_version_1.PdfParagraph,
    ):
        text = paragraph.unicode
        current_font_size = paragraph.size * scale
        box = paragraph.box
        current_x = box.x
        current_y = box.y2 - current_font_size
        chars = []

        # 检查是否为目录条目，通过计算点号的数量
        dot_count = text.count(".")
        if dot_count >= 20:  # 如果包含至少20个点号，认为是目录条目
            # 计算每行可容纳的最大字符数
            max_width = box.x2 - box.x
            processed_text = self.process_toc_dots(
                text, noto_font, current_font_size, max_width
            )
            if processed_text is None:
                return None
            text = processed_text

        for char in text:
            char_width = noto_font.char_lengths(char, current_font_size)[0]

            if current_x + char_width > box.x2:
                current_x = box.x
                current_y -= current_font_size * 1.4

                # 检查是否超出底部边界
                if current_y < box.y or current_y + current_font_size > box.y2:
                    return None

            char_box = il_version_1.Box(
                x=current_x,
                y=current_y,
                x2=current_x + char_width,
                y2=current_y + current_font_size,
            )

            chars.append(
                il_version_1.PdfCharacter(
                    pdf_font_id="noto",
                    pdf_character_id=noto_font.has_glyph(ord(char)),
                    char_unicode=char,
                    box=char_box,
                    size=current_font_size,
                    graphic_state=paragraph.graphic_state,
                    scale=scale,
                )
            )

            current_x += char_width

        return chars

    def get_max_right_space(self, current_box: Box, page) -> float:
        """获取段落右侧最大可用空间

        Args:
            current_box: 当前段落的边界框
            page: 当前页面

        Returns:
            可以扩展到的最大x坐标
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
        if paragraph.pdf_paragraph_composition:
            return
        scale = 1.0
        # 尝试排版，如果失败则逐步缩小字号
        while scale >= scale_threshold:
            result = self.try_typeset(scale, noto_font, paragraph)
            if result is not None:
                paragraph.pdf_paragraph_composition = [
                    PdfParagraphComposition(pdf_character=result)
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
