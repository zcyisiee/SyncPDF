from dataclasses import dataclass
from dataclasses import field


@dataclass(slots=True)
class BaseOperations:
    class Meta:
        name = "baseOperations"

    value: str = field(
        default="",
        metadata={
            "required": True,
        },
    )


@dataclass(slots=True)
class Box:
    class Meta:
        name = "box"

    x: float | None = field(
        default=None,
        metadata={
            "type": "Attribute",
            "required": True,
        },
    )
    y: float | None = field(
        default=None,
        metadata={
            "type": "Attribute",
            "required": True,
        },
    )
    x2: float | None = field(
        default=None,
        metadata={
            "type": "Attribute",
            "required": True,
        },
    )
    y2: float | None = field(
        default=None,
        metadata={
            "type": "Attribute",
            "required": True,
        },
    )


@dataclass(slots=True)
class GraphicState:
    class Meta:
        name = "graphicState"

    linewidth: float | None = field(
        default=None,
        metadata={
            "type": "Attribute",
        },
    )
    dash: list[float] = field(
        default_factory=list,
        metadata={
            "type": "Attribute",
            "min_length": 1,
            "tokens": True,
        },
    )
    flatness: float | None = field(
        default=None,
        metadata={
            "type": "Attribute",
        },
    )
    intent: str | None = field(
        default=None,
        metadata={
            "type": "Attribute",
        },
    )
    linecap: int | None = field(
        default=None,
        metadata={
            "type": "Attribute",
        },
    )
    linejoin: int | None = field(
        default=None,
        metadata={
            "type": "Attribute",
        },
    )
    miterlimit: float | None = field(
        default=None,
        metadata={
            "type": "Attribute",
        },
    )
    ncolor: list[float] = field(
        default_factory=list,
        metadata={
            "type": "Attribute",
            "min_length": 1,
            "tokens": True,
        },
    )
    scolor: list[float] = field(
        default_factory=list,
        metadata={
            "type": "Attribute",
            "min_length": 1,
            "tokens": True,
        },
    )
    stroking_color_space_name: str | None = field(
        default=None,
        metadata={
            "name": "strokingColorSpaceName",
            "type": "Attribute",
        },
    )
    non_stroking_color_space_name: str | None = field(
        default=None,
        metadata={
            "name": "nonStrokingColorSpaceName",
            "type": "Attribute",
        },
    )
    passthrough_per_char_instruction: str | None = field(
        default=None,
        metadata={
            "name": "passthroughPerCharInstruction",
            "type": "Attribute",
        },
    )


@dataclass(slots=True)
class PdfFontCharBoundingBox:
    class Meta:
        name = "pdfFontCharBoundingBox"

    x: float | None = field(
        default=None,
        metadata={
            "type": "Attribute",
            "required": True,
        },
    )
    y: float | None = field(
        default=None,
        metadata={
            "type": "Attribute",
            "required": True,
        },
    )
    x2: float | None = field(
        default=None,
        metadata={
            "type": "Attribute",
            "required": True,
        },
    )
    y2: float | None = field(
        default=None,
        metadata={
            "type": "Attribute",
            "required": True,
        },
    )
    char_id: int | None = field(
        default=None,
        metadata={
            "type": "Attribute",
            "required": True,
        },
    )


@dataclass(slots=True)
class PdfFromImage:
    class Meta:
        name = "pdfFromImage"

    xref_id: int | None = field(
        default=None,
        metadata={
            "name": "xrefId",
            "type": "Attribute",
            "required": True,
        },
    )


@dataclass(slots=True)
class PdfInlineImage:
    class Meta:
        name = "pdfInlineImage"

    image_data: str | None = field(
        default=None,
        metadata={
            "name": "imageData",
            "type": "Attribute",
            "required": True,
        },
    )


@dataclass(slots=True)
class PdfPath:
    class Meta:
        name = "pdfPath"

    x: float | None = field(
        default=None,
        metadata={
            "type": "Attribute",
            "required": True,
        },
    )
    y: float | None = field(
        default=None,
        metadata={
            "type": "Attribute",
            "required": True,
        },
    )
    op: str | None = field(
        default=None,
        metadata={
            "type": "Attribute",
            "required": True,
        },
    )


@dataclass(slots=True)
class Cropbox:
    class Meta:
        name = "cropbox"

    box: Box | None = field(
        default=None,
        metadata={
            "type": "Element",
            "required": True,
        },
    )


@dataclass(slots=True)
class Mediabox:
    class Meta:
        name = "mediabox"

    box: Box | None = field(
        default=None,
        metadata={
            "type": "Element",
            "required": True,
        },
    )


@dataclass(slots=True)
class PageLayout:
    class Meta:
        name = "pageLayout"

    box: Box | None = field(
        default=None,
        metadata={
            "type": "Element",
            "required": True,
        },
    )
    id: int | None = field(
        default=None,
        metadata={
            "type": "Attribute",
            "required": True,
        },
    )
    conf: float | None = field(
        default=None,
        metadata={
            "type": "Attribute",
            "required": True,
        },
    )
    class_name: str | None = field(
        default=None,
        metadata={
            "type": "Attribute",
            "required": True,
        },
    )


@dataclass(slots=True)
class PdfCurve:
    class Meta:
        name = "pdfCurve"

    box: Box | None = field(
        default=None,
        metadata={
            "type": "Element",
            "required": True,
        },
    )
    graphic_state: GraphicState | None = field(
        default=None,
        metadata={
            "name": "graphicState",
            "type": "Element",
            "required": True,
        },
    )
    pdf_path: list[PdfPath] = field(
        default_factory=list,
        metadata={
            "name": "pdfPath",
            "type": "Element",
        },
    )
    debug_info: bool | None = field(
        default=None,
        metadata={
            "type": "Attribute",
        },
    )
    fill_background: bool | None = field(
        default=None,
        metadata={
            "type": "Attribute",
        },
    )
    stroke_path: bool | None = field(
        default=None,
        metadata={
            "type": "Attribute",
        },
    )
    evenodd: bool | None = field(
        default=None,
        metadata={
            "type": "Attribute",
        },
    )
    xobj_id: int | None = field(
        default=None,
        metadata={
            "name": "xobjId",
            "type": "Attribute",
        },
    )
    render_order: int | None = field(
        default=None,
        metadata={
            "name": "renderOrder",
            "type": "Attribute",
        },
    )


@dataclass(slots=True)
class PdfFigure:
    class Meta:
        name = "pdfFigure"

    box: Box | None = field(
        default=None,
        metadata={
            "type": "Element",
            "required": True,
        },
    )


@dataclass(slots=True)
class PdfFont:
    class Meta:
        name = "pdfFont"

    pdf_font_char_bounding_box: list[PdfFontCharBoundingBox] = field(
        default_factory=list,
        metadata={
            "name": "pdfFontCharBoundingBox",
            "type": "Element",
        },
    )
    name: str | None = field(
        default=None,
        metadata={
            "type": "Attribute",
            "required": True,
        },
    )
    font_id: str | None = field(
        default=None,
        metadata={
            "name": "fontId",
            "type": "Attribute",
            "required": True,
        },
    )
    xref_id: int | None = field(
        default=None,
        metadata={
            "name": "xrefId",
            "type": "Attribute",
            "required": True,
        },
    )
    encoding_length: int | None = field(
        default=None,
        metadata={
            "name": "encodingLength",
            "type": "Attribute",
            "required": True,
        },
    )
    bold: bool | None = field(
        default=None,
        metadata={
            "type": "Attribute",
        },
    )
    italic: bool | None = field(
        default=None,
        metadata={
            "type": "Attribute",
        },
    )
    monospace: bool | None = field(
        default=None,
        metadata={
            "type": "Attribute",
        },
    )
    serif: bool | None = field(
        default=None,
        metadata={
            "type": "Attribute",
        },
    )
    ascent: float | None = field(
        default=None,
        metadata={
            "type": "Attribute",
        },
    )
    descent: float | None = field(
        default=None,
        metadata={
            "type": "Attribute",
        },
    )


@dataclass(slots=True)
class PdfImageSubtype:
    class Meta:
        name = "pdfImageSubtype"

    pdf_inline_image: PdfInlineImage | None = field(
        default=None,
        metadata={
            "name": "pdfInlineImage",
            "type": "Element",
        },
    )
    pdf_from_image: PdfFromImage | None = field(
        default=None,
        metadata={
            "name": "pdfFromImage",
            "type": "Element",
        },
    )


@dataclass(slots=True)
class PdfRectangle:
    class Meta:
        name = "pdfRectangle"

    box: Box | None = field(
        default=None,
        metadata={
            "type": "Element",
            "required": True,
        },
    )
    graphic_state: GraphicState | None = field(
        default=None,
        metadata={
            "name": "graphicState",
            "type": "Element",
            "required": True,
        },
    )
    debug_info: bool | None = field(
        default=None,
        metadata={
            "type": "Attribute",
        },
    )
    fill_background: bool | None = field(
        default=None,
        metadata={
            "type": "Attribute",
        },
    )
    xobj_id: int | None = field(
        default=None,
        metadata={
            "name": "xobjId",
            "type": "Attribute",
        },
    )
    line_width: float | None = field(
        default=None,
        metadata={
            "name": "lineWidth",
            "type": "Attribute",
        },
    )
    render_order: int | None = field(
        default=None,
        metadata={
            "name": "renderOrder",
            "type": "Attribute",
        },
    )


@dataclass(slots=True)
class PdfStyle:
    class Meta:
        name = "pdfStyle"

    graphic_state: GraphicState | None = field(
        default=None,
        metadata={
            "name": "graphicState",
            "type": "Element",
            "required": True,
        },
    )
    font_id: str | None = field(
        default=None,
        metadata={
            "type": "Attribute",
            "required": True,
        },
    )
    font_size: float | None = field(
        default=None,
        metadata={
            "type": "Attribute",
            "required": True,
        },
    )


@dataclass(slots=True)
class VisualBbox:
    class Meta:
        name = "visual_bbox"

    box: Box | None = field(
        default=None,
        metadata={
            "type": "Element",
            "required": True,
        },
    )


@dataclass(slots=True)
class PdfCharacter:
    class Meta:
        name = "pdfCharacter"

    pdf_style: PdfStyle | None = field(
        default=None,
        metadata={
            "name": "pdfStyle",
            "type": "Element",
            "required": True,
        },
    )
    box: Box | None = field(
        default=None,
        metadata={
            "type": "Element",
            "required": True,
        },
    )
    visual_bbox: VisualBbox | None = field(
        default=None,
        metadata={
            "type": "Element",
        },
    )
    vertical: bool | None = field(
        default=None,
        metadata={
            "type": "Attribute",
        },
    )
    scale: float | None = field(
        default=None,
        metadata={
            "type": "Attribute",
        },
    )
    pdf_character_id: int | None = field(
        default=None,
        metadata={
            "name": "pdfCharacterId",
            "type": "Attribute",
        },
    )
    char_unicode: str | None = field(
        default=None,
        metadata={
            "type": "Attribute",
            "required": True,
        },
    )
    advance: float | None = field(
        default=None,
        metadata={
            "type": "Attribute",
        },
    )
    xobj_id: int | None = field(
        default=None,
        metadata={
            "name": "xobjId",
            "type": "Attribute",
        },
    )
    debug_info: bool | None = field(
        default=None,
        metadata={
            "type": "Attribute",
        },
    )
    formula_layout_id: int | None = field(
        default=None,
        metadata={
            "type": "Attribute",
        },
    )
    render_order: int | None = field(
        default=None,
        metadata={
            "name": "renderOrder",
            "type": "Attribute",
        },
    )


@dataclass(slots=True)
class PdfImage:
    class Meta:
        name = "pdfImage"

    box: Box | None = field(
        default=None,
        metadata={
            "type": "Element",
            "required": True,
        },
    )
    pdf_image_subtype: PdfImageSubtype | None = field(
        default=None,
        metadata={
            "name": "pdfImageSubtype",
            "type": "Element",
            "required": True,
        },
    )
    xobj_id: int | None = field(
        default=None,
        metadata={
            "name": "xobjId",
            "type": "Attribute",
            "required": True,
        },
    )
    xref_id: int | None = field(
        default=None,
        metadata={
            "name": "xrefId",
            "type": "Attribute",
        },
    )
    render_order: int | None = field(
        default=None,
        metadata={
            "name": "renderOrder",
            "type": "Attribute",
            "required": True,
        },
    )
    image_type: str | None = field(
        default=None,
        metadata={
            "name": "imageType",
            "type": "Attribute",
            "required": True,
        },
    )


@dataclass(slots=True)
class PdfSameStyleUnicodeCharacters:
    class Meta:
        name = "pdfSameStyleUnicodeCharacters"

    pdf_style: PdfStyle | None = field(
        default=None,
        metadata={
            "name": "pdfStyle",
            "type": "Element",
        },
    )
    unicode: str | None = field(
        default=None,
        metadata={
            "type": "Attribute",
            "required": True,
        },
    )
    debug_info: bool | None = field(
        default=None,
        metadata={
            "type": "Attribute",
        },
    )


@dataclass(slots=True)
class PdfXobject:
    class Meta:
        name = "pdfXobject"

    box: Box | None = field(
        default=None,
        metadata={
            "type": "Element",
            "required": True,
        },
    )
    pdf_font: list[PdfFont] = field(
        default_factory=list,
        metadata={
            "name": "pdfFont",
            "type": "Element",
        },
    )
    base_operations: BaseOperations | None = field(
        default=None,
        metadata={
            "name": "baseOperations",
            "type": "Element",
            "required": True,
        },
    )
    xobj_id: int | None = field(
        default=None,
        metadata={
            "name": "xobjId",
            "type": "Attribute",
            "required": True,
        },
    )
    xref_id: int | None = field(
        default=None,
        metadata={
            "name": "xrefId",
            "type": "Attribute",
            "required": True,
        },
    )


@dataclass(slots=True)
class PdfFormula:
    class Meta:
        name = "pdfFormula"

    box: Box | None = field(
        default=None,
        metadata={
            "type": "Element",
            "required": True,
        },
    )
    pdf_character: list[PdfCharacter] = field(
        default_factory=list,
        metadata={
            "name": "pdfCharacter",
            "type": "Element",
            "min_occurs": 1,
        },
    )
    x_offset: float | None = field(
        default=None,
        metadata={
            "type": "Attribute",
            "required": True,
        },
    )
    y_offset: float | None = field(
        default=None,
        metadata={
            "type": "Attribute",
            "required": True,
        },
    )
    x_advance: float | None = field(
        default=None,
        metadata={
            "type": "Attribute",
        },
    )


@dataclass(slots=True)
class PdfLine:
    class Meta:
        name = "pdfLine"

    box: Box | None = field(
        default=None,
        metadata={
            "type": "Element",
            "required": True,
        },
    )
    pdf_character: list[PdfCharacter] = field(
        default_factory=list,
        metadata={
            "name": "pdfCharacter",
            "type": "Element",
            "min_occurs": 1,
        },
    )
    render_order: int | None = field(
        default=None,
        metadata={
            "name": "renderOrder",
            "type": "Attribute",
        },
    )


@dataclass(slots=True)
class PdfSameStyleCharacters:
    class Meta:
        name = "pdfSameStyleCharacters"

    box: Box | None = field(
        default=None,
        metadata={
            "type": "Element",
            "required": True,
        },
    )
    pdf_style: PdfStyle | None = field(
        default=None,
        metadata={
            "name": "pdfStyle",
            "type": "Element",
            "required": True,
        },
    )
    pdf_character: list[PdfCharacter] = field(
        default_factory=list,
        metadata={
            "name": "pdfCharacter",
            "type": "Element",
            "min_occurs": 1,
        },
    )


@dataclass(slots=True)
class PdfParagraphComposition:
    class Meta:
        name = "pdfParagraphComposition"

    pdf_line: PdfLine | None = field(
        default=None,
        metadata={
            "name": "pdfLine",
            "type": "Element",
        },
    )
    pdf_formula: PdfFormula | None = field(
        default=None,
        metadata={
            "name": "pdfFormula",
            "type": "Element",
        },
    )
    pdf_same_style_characters: PdfSameStyleCharacters | None = field(
        default=None,
        metadata={
            "name": "pdfSameStyleCharacters",
            "type": "Element",
        },
    )
    pdf_character: PdfCharacter | None = field(
        default=None,
        metadata={
            "name": "pdfCharacter",
            "type": "Element",
        },
    )
    pdf_same_style_unicode_characters: PdfSameStyleUnicodeCharacters | None = field(
        default=None,
        metadata={
            "name": "pdfSameStyleUnicodeCharacters",
            "type": "Element",
        },
    )


@dataclass(slots=True)
class PdfParagraph:
    class Meta:
        name = "pdfParagraph"

    box: Box | None = field(
        default=None,
        metadata={
            "type": "Element",
            "required": True,
        },
    )
    pdf_style: PdfStyle | None = field(
        default=None,
        metadata={
            "name": "pdfStyle",
            "type": "Element",
            "required": True,
        },
    )
    pdf_paragraph_composition: list[PdfParagraphComposition] = field(
        default_factory=list,
        metadata={
            "name": "pdfParagraphComposition",
            "type": "Element",
        },
    )
    xobj_id: int | None = field(
        default=None,
        metadata={
            "name": "xobjId",
            "type": "Attribute",
        },
    )
    unicode: str | None = field(
        default=None,
        metadata={
            "type": "Attribute",
            "required": True,
        },
    )
    scale: float | None = field(
        default=None,
        metadata={
            "type": "Attribute",
        },
    )
    optimal_scale: float | None = field(
        default=None,
        metadata={
            "type": "Attribute",
        },
    )
    vertical: bool | None = field(
        default=None,
        metadata={
            "type": "Attribute",
        },
    )
    first_line_indent: bool | None = field(
        default=None,
        metadata={
            "name": "FirstLineIndent",
            "type": "Attribute",
        },
    )
    debug_id: str | None = field(
        default=None,
        metadata={
            "type": "Attribute",
        },
    )
    layout_label: str | None = field(
        default=None,
        metadata={
            "type": "Attribute",
        },
    )
    layout_id: int | None = field(
        default=None,
        metadata={
            "type": "Attribute",
        },
    )
    render_order: int | None = field(
        default=None,
        metadata={
            "name": "renderOrder",
            "type": "Attribute",
        },
    )


@dataclass(slots=True)
class Page:
    class Meta:
        name = "page"

    mediabox: Mediabox | None = field(
        default=None,
        metadata={
            "type": "Element",
            "required": True,
        },
    )
    cropbox: Cropbox | None = field(
        default=None,
        metadata={
            "type": "Element",
            "required": True,
        },
    )
    pdf_xobject: list[PdfXobject] = field(
        default_factory=list,
        metadata={
            "name": "pdfXobject",
            "type": "Element",
        },
    )
    page_layout: list[PageLayout] = field(
        default_factory=list,
        metadata={
            "name": "pageLayout",
            "type": "Element",
        },
    )
    pdf_rectangle: list[PdfRectangle] = field(
        default_factory=list,
        metadata={
            "name": "pdfRectangle",
            "type": "Element",
        },
    )
    pdf_font: list[PdfFont] = field(
        default_factory=list,
        metadata={
            "name": "pdfFont",
            "type": "Element",
        },
    )
    pdf_paragraph: list[PdfParagraph] = field(
        default_factory=list,
        metadata={
            "name": "pdfParagraph",
            "type": "Element",
        },
    )
    pdf_figure: list[PdfFigure] = field(
        default_factory=list,
        metadata={
            "name": "pdfFigure",
            "type": "Element",
        },
    )
    pdf_character: list[PdfCharacter] = field(
        default_factory=list,
        metadata={
            "name": "pdfCharacter",
            "type": "Element",
        },
    )
    pdf_curve: list[PdfCurve] = field(
        default_factory=list,
        metadata={
            "name": "pdfCurve",
            "type": "Element",
        },
    )
    pdf_image: list[PdfImage] = field(
        default_factory=list,
        metadata={
            "name": "pdfImage",
            "type": "Element",
        },
    )
    base_operations: BaseOperations | None = field(
        default=None,
        metadata={
            "name": "baseOperations",
            "type": "Element",
            "required": True,
        },
    )
    page_number: int | None = field(
        default=None,
        metadata={
            "name": "pageNumber",
            "type": "Attribute",
            "required": True,
        },
    )
    unit: str | None = field(
        default=None,
        metadata={
            "name": "Unit",
            "type": "Attribute",
            "required": True,
        },
    )


@dataclass(slots=True)
class Document:
    class Meta:
        name = "document"

    page: list[Page] = field(
        default_factory=list,
        metadata={
            "type": "Element",
            "min_occurs": 1,
        },
    )
    total_pages: int | None = field(
        default=None,
        metadata={
            "name": "totalPages",
            "type": "Attribute",
            "required": True,
        },
    )
