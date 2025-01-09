import math
from typing import List, Union

from pymupdf import Font

from yadt.document_il.il_version_1 import PdfCharacter, PdfParagraph


class Layout:
    def __init__(self, id, name):
        self.id = id
        self.name = name

    @staticmethod
    def is_newline(prev_char: PdfCharacter, curr_char: PdfCharacter) -> bool:
        # 如果没有前一个字符，不是换行
        if prev_char is None:
            return False

        # 获取两个字符的中心 y 坐标
        prev_y = (prev_char.box.y + prev_char.box.y2) / 2
        curr_y = (curr_char.box.y + curr_char.box.y2) / 2

        # 如果当前字符的 y 坐标明显低于前一个字符，说明换行了
        # 这里使用字符高度的一半作为阈值
        char_height = curr_char.box.y2 - curr_char.box.y
        return curr_y < prev_y - char_height / 2


def get_paragraph_length_except(
    paragraph: PdfParagraph, except_chars: str, font: Font
) -> int:
    length = 0
    for composition in paragraph.pdf_paragraph_composition:
        if composition.pdf_character:
            length += (
                composition.pdf_character[0].box.x2
                - composition.pdf_character[0].box.x
            )
        elif composition.pdf_same_style_characters:
            for (
                pdf_char
            ) in composition.pdf_same_style_characters.pdf_character:
                if pdf_char.char_unicode in except_chars:
                    continue
                length += pdf_char.box.x2 - pdf_char.box.x
        elif composition.pdf_same_style_unicode_characters:
            for (
                char_unicode
            ) in composition.pdf_same_style_unicode_characters.unicode:
                if char_unicode in except_chars:
                    continue
                length += font.char_lengths(
                    char_unicode,
                    composition.pdf_same_style_unicode_characters.pdf_style.font_size,
                )[0]
        elif composition.pdf_line:
            for pdf_char in composition.pdf_line.pdf_character:
                if pdf_char.char_unicode in except_chars:
                    continue
                length += pdf_char.box.x2 - pdf_char.box.x
        elif composition.pdf_formula:
            length += (
                composition.pdf_formula.box.x2 - composition.pdf_formula.box.x
            )
        else:
            raise ValueError(
                f"Unknown composition type. "
                f"Composition: {composition}. "
                f"Paragraph: {paragraph}. "
            )
    return length


def get_paragraph_unicode(paragraph: PdfParagraph) -> str:
    chars = []
    for composition in paragraph.pdf_paragraph_composition:
        if composition.pdf_line:
            chars.extend(composition.pdf_line.pdf_character)
        elif composition.pdf_same_style_characters:
            chars.extend(composition.pdf_same_style_characters.pdf_character)
        elif composition.pdf_same_style_unicode_characters:
            chars.extend(composition.pdf_same_style_unicode_characters.unicode)
        elif composition.pdf_formula:
            chars.extend(composition.pdf_formula.pdf_character)
        elif composition.pdf_character:
            chars.append(composition.pdf_character)
        else:
            raise ValueError(
                f"Unknown composition type. "
                f"Composition: {composition}. "
                f"Paragraph: {paragraph}. "
            )
    return get_char_unicode_string(chars)


def get_char_unicode_string(chars: List[Union[PdfCharacter, str]]) -> str:
    """
    将字符列表转换为 Unicode 字符串，根据字符间距自动插入空格。
    有些 PDF 不会显式编码空格，这时需要根据间距自动插入空格。

    Args:
        chars: 字符列表，可以是 PdfCharacter 对象或字符串

    Returns:
        str: 处理后的 Unicode 字符串
    """
    # 计算字符间距的中位数
    distances = []
    for i in range(len(chars) - 1):
        if not (
            isinstance(chars[i], PdfCharacter)
            and isinstance(chars[i + 1], PdfCharacter)
        ):
            continue
        distance = chars[i + 1].box.x - chars[i].box.x2
        if distance > 1:  # 只考虑正向距离
            distances.append(distance)

    # 去重后的距离
    distinct_distances = sorted(set(distances))

    if not distinct_distances:
        median_distance = 1
    elif len(distinct_distances) == 1:
        median_distance = distinct_distances[0]
    else:
        median_distance = distinct_distances[1]

    # 构建 unicode 字符串，根据间距插入空格
    unicode_chars = []
    for i in range(len(chars)):
        # 如果不是字符对象，直接添加，一般来说这个时候 chars[i] 是字符串
        if not isinstance(chars[i], PdfCharacter):
            unicode_chars.append(chars[i])
            continue
        unicode_chars.append(chars[i].char_unicode)

        # 如果是空格，跳过
        if chars[i].char_unicode == " ":
            continue

        # 如果两个字符都是 PdfCharacter，检查间距
        if i < len(chars) - 1 and isinstance(chars[i + 1], PdfCharacter):
            distance = chars[i + 1].box.x - chars[i].box.x2
            if (
                distance >= median_distance  # 间距大于中位数
                or Layout.is_newline(chars[i], chars[i + 1])  # 换行
            ):
                unicode_chars.append(" ")  # 添加空格

    return "".join(unicode_chars)


def get_paragraph_max_height(paragraph: PdfParagraph) -> float:
    """
    获取段落中最高的排版单元高度。

    Args:
        paragraph: PDF段落对象

    Returns:
        float: 最大高度值
    """
    max_height = 0.0
    for composition in paragraph.pdf_paragraph_composition:
        if composition.pdf_character:
            char_height = (
                composition.pdf_character[0].box.y2
                - composition.pdf_character[0].box.y
            )
            max_height = max(max_height, char_height)
        elif composition.pdf_same_style_characters:
            for (
                pdf_char
            ) in composition.pdf_same_style_characters.pdf_character:
                char_height = pdf_char.box.y2 - pdf_char.box.y
                max_height = max(max_height, char_height)
        elif composition.pdf_same_style_unicode_characters:
            # 对于纯Unicode字符，我们使用其样式中的字体大小作为高度估计
            font_size = composition.pdf_same_style_unicode_characters.pdf_style.font_size
            max_height = max(max_height, font_size)
        elif composition.pdf_line:
            for pdf_char in composition.pdf_line.pdf_character:
                char_height = pdf_char.box.y2 - pdf_char.box.y
                max_height = max(max_height, char_height)
        elif composition.pdf_formula:
            formula_height = (
                composition.pdf_formula.box.y2 - composition.pdf_formula.box.y
            )
            max_height = max(max_height, formula_height)
        else:
            raise ValueError(
                f"Unknown composition type. "
                f"Composition: {composition}. "
                f"Paragraph: {paragraph}. "
            )
    return max_height


def is_same_style(style1, style2) -> bool:
    """判断两个样式是否相同"""
    if style1 is None or style2 is None:
        return style1 is style2

    return (
        style1.font_id == style2.font_id
        and math.fabs(style1.font_size - style2.font_size) < 0.02
        and is_same_graphic_state(style1.graphic_state, style2.graphic_state)
    )


def is_same_graphic_state(state1, state2) -> bool:
    """判断两个 GraphicState 是否相同"""
    if state1 is None or state2 is None:
        return state1 is state2

    return (
        state1.linewidth == state2.linewidth
        and state1.dash == state2.dash
        and state1.flatness == state2.flatness
        and state1.intent == state2.intent
        and state1.linecap == state2.linecap
        and state1.linejoin == state2.linejoin
        and state1.miterlimit == state2.miterlimit
        and state1.ncolor == state2.ncolor
        and state1.scolor == state2.scolor
        and state1.stroking_color_space_name
        == state2.stroking_color_space_name
        and state1.non_stroking_color_space_name
        == state2.non_stroking_color_space_name
    )
