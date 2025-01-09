import math

from yadt.document_il.il_version_1 import PdfCharacter


def get_char_unicode_string(char: [PdfCharacter]) -> str:
    return "".join(char.char_unicode for char in char)


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
    """判断两个GraphicState是否相同"""
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
