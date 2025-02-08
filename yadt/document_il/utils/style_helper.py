from yadt.document_il import il_version_1

def create_pdf_style(r,g,b, font_id="china-ss", font_size=6):
    """
    Create a PdfStyle object from RGB values.
    
    Args:
        r: Red component in range 0-255
        g: Green component in range 0-255
        b: Blue component in range 0-255
        font_id: Font identifier
        font_size: Font size
    
    Returns:
        PdfStyle object with the specified color
    """
    r, g, b = [x / 255.0 for x in (r, g, b)]
    return il_version_1.PdfStyle(
        font_id=font_id,
        font_size=font_size,
        graphic_state=il_version_1.GraphicState(
            passthrough_per_char_instruction=f'{r:.10f} {g:.10f} {b:.10f} rg'
        ),
    )

# Generate all color styles
RED = il_version_1.PdfStyle(
    font_id="china-ss",
    font_size=6,
    graphic_state=il_version_1.GraphicState(
        passthrough_per_char_instruction='1.0000000000 0.2313725490 0.1882352941 rg'
    )
)
ORANGE = il_version_1.PdfStyle(
    font_id="china-ss",
    font_size=6,
    graphic_state=il_version_1.GraphicState(
        passthrough_per_char_instruction='1.0000000000 0.5843137255 0.0000000000 rg'
    )
)
YELLOW = il_version_1.PdfStyle(
    font_id="china-ss",
    font_size=6,
    graphic_state=il_version_1.GraphicState(
        passthrough_per_char_instruction='1.0000000000 0.8000000000 0.0000000000 rg'
    )
)
GREEN = il_version_1.PdfStyle(
    font_id="china-ss",
    font_size=6,
    graphic_state=il_version_1.GraphicState(
        passthrough_per_char_instruction='0.2039215686 0.7803921569 0.3490196078 rg'
    )
)
MINT = il_version_1.PdfStyle(
    font_id="china-ss",
    font_size=6,
    graphic_state=il_version_1.GraphicState(
        passthrough_per_char_instruction='0.0000000000 0.7803921569 0.7450980392 rg'
    )
)
TEAL = il_version_1.PdfStyle(
    font_id="china-ss",
    font_size=6,
    graphic_state=il_version_1.GraphicState(
        passthrough_per_char_instruction='0.1882352941 0.6901960784 0.7803921569 rg'
    )
)
CYAN = il_version_1.PdfStyle(
    font_id="china-ss",
    font_size=6,
    graphic_state=il_version_1.GraphicState(
        passthrough_per_char_instruction='0.1960784314 0.6784313725 0.9019607843 rg'
    )
)
BLUE = il_version_1.PdfStyle(
    font_id="china-ss",
    font_size=6,
    graphic_state=il_version_1.GraphicState(
        passthrough_per_char_instruction='0.0000000000 0.4784313725 1.0000000000 rg'
    )
)
INDIGO = il_version_1.PdfStyle(
    font_id="china-ss",
    font_size=6,
    graphic_state=il_version_1.GraphicState(
        passthrough_per_char_instruction='0.3450980392 0.3372549020 0.8392156863 rg'
    )
)
PURPLE = il_version_1.PdfStyle(
    font_id="china-ss",
    font_size=6,
    graphic_state=il_version_1.GraphicState(
        passthrough_per_char_instruction='0.6862745098 0.3215686275 0.8705882353 rg'
    )
)
PINK = il_version_1.PdfStyle(
    font_id="china-ss",
    font_size=6,
    graphic_state=il_version_1.GraphicState(
        passthrough_per_char_instruction='1.0000000000 0.1764705882 0.3333333333 rg'
    )
)
BROWN = il_version_1.PdfStyle(
    font_id="china-ss",
    font_size=6,
    graphic_state=il_version_1.GraphicState(
        passthrough_per_char_instruction='0.6352941176 0.5176470588 0.3686274510 rg'
    )
)
