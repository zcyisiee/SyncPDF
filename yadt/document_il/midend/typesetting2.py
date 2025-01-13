import pymupdf

from yadt.document_il import (
    Box,
    PdfCharacter,
    PdfLine,
    PdfParagraph,
    PdfParagraphComposition,
    PdfStyle,
    il_version_1,
    PdfFormula,
)
from yadt.document_il.utils.layout_helper import (
    get_paragraph_length_except,
    get_paragraph_max_height,
    get_paragraph_unicode,
)


class TypesettingUnit:
    def __init__(
        self,
        char: PdfCharacter = None,
        formular: PdfFormula = None,
        unicode: str = None,
        font: pymupdf.Font = None,
        font_size: float = None,
        style: PdfStyle = None,
    ):
        assert (
            sum((x is not None for x in [char, formular, unicode])) == 1
        ), "Only one of chars and formular can be not None"
        self.char = char
        self.formular = formular
        self.unicode = unicode
        self.x = None
        self.y = None
        self.scale = None

        if unicode:
            assert font_size, \
                "Font size must be provided when unicode is provided"
            assert font, "Font must be provided when unicode is provided"
            assert style, "Style must be provided when unicode is provided"
            assert len(unicode) == 1, "Unicode must be a single character"

            self.font = font
            self.font_size = font_size
            self.style = style

    def passthrough(self) -> [PdfCharacter]:
        if self.char:
            return [self.char]
        elif self.formular:
            return self.formular.pdf_character
        elif self.unicode:
            raise ValueError("Cannot passthrough unicode")

    @property
    def can_passthrough(self):
        return self.unicode is None

    @property
    def box(self):
        if self.char:
            return self.char.box
        elif self.formular:
            return self.formular.box
        elif self.unicode:
            char_width = self.font.char_lengths(
                self.unicode, self.font_size)[0]
            return Box(0, 0, char_width, self.font_size)

    @property
    def width(self):
        return self.box.x2 - self.box.x

    @property
    def height(self):
        return self.box.y2 - self.box.y

    def relocate(self, x: float, y: float, scale: float) -> 'TypesettingUnit':
        """重定位并缩放排版单元

        Args:
            x: 新的 x 坐标
            y: 新的 y 坐标
            scale: 缩放因子

        Returns:
            新的排版单元
        """
        if self.char:
            # 创建新的字符对象
            new_char = PdfCharacter(
                pdf_character_id=self.char.pdf_character_id,
                char_unicode=self.char.char_unicode,
                box=Box(
                    x=x,
                    y=y,
                    x2=x + self.width * scale,
                    y2=y + self.height * scale,
                ),
                pdf_style=PdfStyle(
                    font_id=self.char.pdf_style.font_id,
                    font_size=self.char.pdf_style.font_size * scale,
                    graphic_state=self.char.pdf_style.graphic_state,
                ),
                scale=scale,
                vertical=self.char.vertical,
                advance=self.char.advance * scale if self.char.advance else None,
                x_offset=self.char.x_offset * scale if self.char.x_offset else None,
            )
            return TypesettingUnit(char=new_char)

        elif self.formular:
            # 创建新的公式对象，保持内部字符的相对位置
            new_chars = []
            min_x = min(char.box.x for char in self.formular.pdf_character)
            min_y = min(char.box.y for char in self.formular.pdf_character)

            for char in self.formular.pdf_character:
                # 计算相对位置
                rel_x = char.box.x - min_x
                rel_y = char.box.y - min_y

                # 创建新的字符对象
                new_char = PdfCharacter(
                    pdf_character_id=char.pdf_character_id,
                    char_unicode=char.char_unicode,
                    box=Box(
                        x=x + rel_x * scale + self.formular.x_offset,
                        y=y + rel_y * scale + self.formular.y_offset,
                        x2=x + rel_x * scale +
                        (char.box.x2 - char.box.x) *
                        scale + self.formular.x_offset,
                        y2=y + rel_y * scale +
                        (char.box.y2 - char.box.y) *
                        scale + self.formular.y_offset,
                    ),
                    pdf_style=PdfStyle(
                        font_id=char.pdf_style.font_id,
                        font_size=char.pdf_style.font_size * scale,
                        graphic_state=char.pdf_style.graphic_state,
                    ),
                    scale=scale,
                    vertical=char.vertical,
                    advance=char.advance * scale if char.advance else None,
                    x_offset=char.x_offset * scale if char.x_offset else None,
                )
                new_chars.append(new_char)

            new_formula = PdfFormula(
                box=Box(
                    x=x,
                    y=y,
                    x2=x + self.width * scale,
                    y2=y + self.height * scale,
                ),
                pdf_character=new_chars,
                x_offset=self.formular.x_offset * scale,
                y_offset=self.formular.y_offset * scale,
            )
            return TypesettingUnit(formular=new_formula)

        elif self.unicode:
            # 对于 Unicode 字符，我们存储新的位置信息
            new_unit = TypesettingUnit(
                unicode=self.unicode,
                font=self.font,
                font_size=self.font_size * scale,
                style=self.style,
            )
            new_unit.x = x
            new_unit.y = y
            new_unit.scale = scale
            return new_unit

    def render(self) -> [PdfCharacter]:
        """渲染排版单元为 PdfCharacter 列表

        Returns:
            PdfCharacter 列表
        """
        if self.can_passthrough:
            return self.passthrough()
        elif self.unicode:
            assert self.x is not None, \
                "x position must be set, should be set by `relocate`"
            assert self.y is not None, \
                "y position must be set, should be set by `relocate`"
            assert self.scale is not None, \
                "scale must be set, should be set by `relocate`"
            # 计算字符宽度
            char_width = self.width

            new_char = PdfCharacter(
                pdf_character_id=self.font.has_glyph(ord(self.unicode)),
                char_unicode=self.unicode,
                box=Box(
                    x=self.x,  # 使用存储的位置
                    y=self.y,
                    x2=self.x + char_width,
                    y2=self.y + self.font_size,
                ),
                pdf_style=PdfStyle(
                    font_id='noto',
                    font_size=self.font_size,
                    graphic_state=self.style.graphic_state,
                ),
                scale=self.scale,
                vertical=False,
                advance=char_width,
            )
            return [new_char]
        else:
            raise ValueError("Unknown typesetting unit")


class Typesetting:
    def __init__(self, font_path: str):
        self.font = pymupdf.Font(fontfile=font_path)

    def typsetting_document(self, document: il_version_1.Document):
        for page in document.page:
            self.render_page(page)

    def render_page(self, page: il_version_1.Page):
        # 开始实际的渲染过程
        for paragraph in page.pdf_paragraph:
            self.render_paragraph(paragraph, page)

    def render_paragraph(self, paragraph: il_version_1.PdfParagraph,
                         page: il_version_1.Page):
        typesetting_units = self.create_typesetting_units(paragraph)
        # 如果所有单元都可以直接传递，则直接传递
        if all(unit.can_passthrough for unit in typesetting_units):
            paragraph.scale = 1.0
            paragraph.pdf_paragraph_composition = (
                self.create_passthrough_composition(typesetting_units))
            return

        # 如果有单元无法直接传递，则进行重排版
        paragraph.pdf_paragraph_composition = []
        self.retypeset(paragraph, page, typesetting_units)

    def _layout_typesetting_units(self,
                                  typesetting_units: list[TypesettingUnit],
                                  box: Box,
                                  scale: float,
                                  line_spacing: float
                                  ) -> tuple[list[TypesettingUnit], bool]:
        """布局排版单元。

        Args:
            typesetting_units: 要布局的排版单元列表
            box: 布局边界框
            scale: 缩放因子
            line_spacing: 行间距

        Returns:
            tuple[list[TypesettingUnit], bool]: (已布局的排版单元列表, 是否所有单元都放得下)
        """
        # 计算平均行高
        avg_height = (sum(unit.height * scale for unit in typesetting_units)
                      / len(typesetting_units) if typesetting_units else 0)

        # 初始化位置为右上角，并减去一个平均行高
        current_x = box.x
        current_y = box.y2 - avg_height
        line_height = 0

        # 存储已排版的单元
        typeset_units = []
        all_units_fit = True

        # 遍历所有排版单元
        for unit in typesetting_units:
            # 计算当前单元在当前缩放下的尺寸
            unit_width = unit.width * scale
            unit_height = unit.height * scale

            # 如果当前行放不下这个元素，换行
            if current_x + unit_width > box.x2:
                # 换行
                current_x = box.x
                current_y -= line_height * line_spacing
                line_height = 0

                # 检查是否超出底部边界
                if current_y - unit_height < box.y:
                    all_units_fit = False
                    break

            # 更新当前行的最大高度
            line_height = max(line_height, unit_height)

            # 放置当前单元
            relocated_unit = unit.relocate(current_x, current_y, scale)
            typeset_units.append(relocated_unit)

            # 更新 x 坐标
            current_x += unit_width

        return typeset_units, all_units_fit

    def retypeset(self, paragraph: il_version_1.PdfParagraph,
                  page: il_version_1.Page,
                  typesetting_units: list[TypesettingUnit]):
        box = paragraph.box
        scale = 1.0
        line_spacing = 1.7  # 初始行距为1.7
        min_scale = 0.1  # 最小缩放因子
        min_line_spacing = 1.1  # 最小行距
        expand_space_flag = False

        while scale >= min_scale:
            # 尝试布局排版单元
            typeset_units, all_units_fit = self._layout_typesetting_units(
                typesetting_units, box, scale, line_spacing)

            # 如果所有单元都放得下，就完成排版
            if all_units_fit:
                # 将排版后的单元转换为段落组合
                paragraph.scale = scale
                paragraph.pdf_paragraph_composition = []
                for unit in typeset_units:
                    for char in unit.render():
                        paragraph.pdf_paragraph_composition.append(
                            PdfParagraphComposition(pdf_character=char)
                        )
                return

            if not expand_space_flag:
                # 如果尚未扩展空格，进行扩展
                max_x = self.get_max_right_space(box, page)
                # 只有当有额外空间时才扩展
                if max_x > box.x2:
                    expanded_box = Box(
                        x=box.x,
                        y=box.y,
                        x2=max_x,  # 直接扩展到最大可用位置
                        y2=box.y2,
                    )
                    # 更新段落的边界框
                    paragraph.box = expanded_box
                expand_space_flag = True
                continue

            # 如果当前行距大于最小行距，先减小行距
            if line_spacing > min_line_spacing:
                line_spacing -= 0.1
            else:
                # 行距已经最小，减小缩放因子
                scale -= 0.1
                line_spacing = 1.7  # 重置行距

    def create_typesetting_units(self,
                                 paragraph: il_version_1.PdfParagraph
                                 ) -> list[TypesettingUnit]:
        if not paragraph.pdf_paragraph_composition:
            return []
        result = []
        for composition in paragraph.pdf_paragraph_composition:
            if composition is None:
                continue
            if composition.pdf_line:
                result.extend([
                    TypesettingUnit(char=char)
                    for char in composition.pdf_line.pdf_character
                ])
            elif composition.pdf_character:
                result.append(
                    TypesettingUnit(char=composition.pdf_character)
                )
            elif composition.pdf_same_style_characters:
                result.extend([
                    TypesettingUnit(char=char)
                    for char in composition
                    .pdf_same_style_characters.pdf_character
                ])
            elif composition.pdf_same_style_unicode_characters:
                result.extend([
                    TypesettingUnit(
                        unicode=char_unicode,
                        font=self.font,
                        font_size=composition
                        .pdf_same_style_unicode_characters.pdf_style.font_size,
                        style=composition
                        .pdf_same_style_unicode_characters.pdf_style
                    )
                    for char_unicode in composition
                    .pdf_same_style_unicode_characters.unicode
                ])
            elif composition.pdf_formula:
                result.extend([
                    TypesettingUnit(formular=composition.pdf_formula)
                ])
            else:
                raise ValueError(
                    f"Unknown composition type. "
                    f"Composition: {composition}. "
                    f"Paragraph: {paragraph}. "
                )

        return result

    def create_passthrough_composition(self,
                                       typesetting_units: list[TypesettingUnit]
                                       ) -> list[PdfParagraphComposition]:
        """从排版单元创建直接传递的段落组合。

        Args:
            typesetting_units: 排版单元列表

        Returns:
            段落组合列表
        """
        composition = []
        for unit in typesetting_units:
            composition.extend([
                PdfParagraphComposition(pdf_character=char)
                for char in unit.passthrough()
            ])
        return composition

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
