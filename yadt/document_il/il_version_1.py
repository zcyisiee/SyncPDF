from dataclasses import dataclass, field
from typing import Optional


@dataclass
class BaseOperations:
    class Meta:
        name = "baseOperations"

    value: str = field(
        default="",
        metadata={
            "required": True,
        },
    )


@dataclass
class Box:
    class Meta:
        name = "box"

    x: Optional[float] = field(
        default=None,
        metadata={
            "type": "Attribute",
            "required": True,
        },
    )
    y: Optional[float] = field(
        default=None,
        metadata={
            "type": "Attribute",
            "required": True,
        },
    )
    x2: Optional[float] = field(
        default=None,
        metadata={
            "type": "Attribute",
            "required": True,
        },
    )
    y2: Optional[float] = field(
        default=None,
        metadata={
            "type": "Attribute",
            "required": True,
        },
    )


@dataclass
class GraphicState:
    class Meta:
        name = "graphicState"

    linewidth: Optional[float] = field(
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
    flatness: Optional[float] = field(
        default=None,
        metadata={
            "type": "Attribute",
        },
    )
    intent: Optional[str] = field(
        default=None,
        metadata={
            "type": "Attribute",
        },
    )
    linecap: Optional[int] = field(
        default=None,
        metadata={
            "type": "Attribute",
        },
    )
    linejoin: Optional[int] = field(
        default=None,
        metadata={
            "type": "Attribute",
        },
    )
    miterlimit: Optional[float] = field(
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
    stroking_color_space_name: Optional[str] = field(
        default=None,
        metadata={
            "name": "strokingColorSpaceName",
            "type": "Attribute",
        },
    )
    non_stroking_color_space_name: Optional[str] = field(
        default=None,
        metadata={
            "name": "nonStrokingColorSpaceName",
            "type": "Attribute",
        },
    )
    passthrough_per_char_instruction: Optional[str] = field(
        default=None,
        metadata={
            "name": "passthroughPerCharInstruction",
            "type": "Attribute",
        },
    )


@dataclass
class PdfFont:
    class Meta:
        name = "pdfFont"

    name: Optional[str] = field(
        default=None,
        metadata={
            "type": "Attribute",
            "required": True,
        },
    )
    font_id: Optional[str] = field(
        default=None,
        metadata={
            "name": "fontId",
            "type": "Attribute",
            "required": True,
        },
    )
    xref_id: Optional[int] = field(
        default=None,
        metadata={
            "name": "xrefId",
            "type": "Attribute",
            "required": True,
        },
    )
    encoding_length: Optional[int] = field(
        default=None,
        metadata={
            "name": "encodingLength",
            "type": "Attribute",
            "required": True,
        },
    )
    bold: Optional[bool] = field(
        default=None,
        metadata={
            "type": "Attribute",
        },
    )
    italic: Optional[bool] = field(
        default=None,
        metadata={
            "type": "Attribute",
        },
    )
    monospace: Optional[bool] = field(
        default=None,
        metadata={
            "type": "Attribute",
        },
    )
    serif: Optional[bool] = field(
        default=None,
        metadata={
            "type": "Attribute",
        },
    )


@dataclass
class GraphicState1:
    class Meta:
        name = "GraphicState"

    graphic_state: Optional[GraphicState] = field(
        default=None,
        metadata={
            "name": "graphicState",
            "type": "Element",
            "required": True,
        },
    )


@dataclass
class Cropbox:
    class Meta:
        name = "cropbox"

    box: Optional[Box] = field(
        default=None,
        metadata={
            "type": "Element",
            "required": True,
        },
    )


@dataclass
class Mediabox:
    class Meta:
        name = "mediabox"

    box: Optional[Box] = field(
        default=None,
        metadata={
            "type": "Element",
            "required": True,
        },
    )


@dataclass
class PageLayout:
    class Meta:
        name = "pageLayout"

    box: Optional[Box] = field(
        default=None,
        metadata={
            "type": "Element",
            "required": True,
        },
    )
    id: Optional[int] = field(
        default=None,
        metadata={
            "type": "Attribute",
            "required": True,
        },
    )
    conf: Optional[float] = field(
        default=None,
        metadata={
            "type": "Attribute",
            "required": True,
        },
    )
    class_name: Optional[str] = field(
        default=None,
        metadata={
            "type": "Attribute",
            "required": True,
        },
    )


@dataclass
class PdfFigure:
    class Meta:
        name = "pdfFigure"

    box: Optional[Box] = field(
        default=None,
        metadata={
            "type": "Element",
            "required": True,
        },
    )


@dataclass
class PdfStyle(GraphicState1):
    class Meta:
        name = "pdfStyle"

    font_id: Optional[str] = field(
        default=None,
        metadata={
            "type": "Attribute",
            "required": True,
        },
    )
    font_size: Optional[float] = field(
        default=None,
        metadata={
            "type": "Attribute",
            "required": True,
        },
    )


@dataclass
class PdfCharacter:
    class Meta:
        name = "pdfCharacter"

    pdf_style: Optional[PdfStyle] = field(
        default=None,
        metadata={
            "name": "pdfStyle",
            "type": "Element",
            "required": True,
        },
    )
    box: Optional[Box] = field(
        default=None,
        metadata={
            "type": "Element",
            "required": True,
        },
    )
    vertical: Optional[bool] = field(
        default=None,
        metadata={
            "type": "Attribute",
        },
    )
    scale: Optional[float] = field(
        default=None,
        metadata={
            "type": "Attribute",
        },
    )
    pdf_character_id: Optional[int] = field(
        default=None,
        metadata={
            "name": "pdfCharacterId",
            "type": "Attribute",
        },
    )
    char_unicode: Optional[str] = field(
        default=None,
        metadata={
            "type": "Attribute",
            "required": True,
        },
    )
    advance: Optional[float] = field(
        default=None,
        metadata={
            "type": "Attribute",
        },
    )


@dataclass
class PdfSameStyleUnicodeCharacters:
    class Meta:
        name = "pdfSameStyleUnicodeCharacters"

    pdf_style: Optional[PdfStyle] = field(
        default=None,
        metadata={
            "name": "pdfStyle",
            "type": "Element",
        },
    )
    unicode: Optional[str] = field(
        default=None,
        metadata={
            "type": "Attribute",
            "required": True,
        },
    )


@dataclass
class PdfFormula:
    class Meta:
        name = "pdfFormula"

    box: Optional[Box] = field(
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
    x_offset: Optional[float] = field(
        default=None,
        metadata={
            "type": "Attribute",
            "required": True,
        },
    )
    y_offset: Optional[float] = field(
        default=None,
        metadata={
            "type": "Attribute",
            "required": True,
        },
    )


@dataclass
class PdfLine:
    class Meta:
        name = "pdfLine"

    box: Optional[Box] = field(
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


@dataclass
class PdfSameStyleCharacters:
    class Meta:
        name = "pdfSameStyleCharacters"

    box: Optional[Box] = field(
        default=None,
        metadata={
            "type": "Element",
            "required": True,
        },
    )
    pdf_style: Optional[PdfStyle] = field(
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


@dataclass
class PdfParagraphComposition:
    class Meta:
        name = "pdfParagraphComposition"

    pdf_line: Optional[PdfLine] = field(
        default=None,
        metadata={
            "name": "pdfLine",
            "type": "Element",
        },
    )
    pdf_formula: Optional[PdfFormula] = field(
        default=None,
        metadata={
            "name": "pdfFormula",
            "type": "Element",
        },
    )
    pdf_same_style_characters: Optional[PdfSameStyleCharacters] = field(
        default=None,
        metadata={
            "name": "pdfSameStyleCharacters",
            "type": "Element",
        },
    )
    pdf_character: Optional[PdfCharacter] = field(
        default=None,
        metadata={
            "name": "pdfCharacter",
            "type": "Element",
        },
    )
    pdf_same_style_unicode_characters: Optional[
        PdfSameStyleUnicodeCharacters
    ] = field(
        default=None,
        metadata={
            "name": "pdfSameStyleUnicodeCharacters",
            "type": "Element",
        },
    )


@dataclass
class PdfParagraph:
    class Meta:
        name = "pdfParagraph"

    box: Optional[Box] = field(
        default=None,
        metadata={
            "type": "Element",
            "required": True,
        },
    )
    pdf_style: Optional[PdfStyle] = field(
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
    unicode: Optional[str] = field(
        default=None,
        metadata={
            "type": "Attribute",
            "required": True,
        },
    )
    scale: Optional[float] = field(
        default=None,
        metadata={
            "type": "Attribute",
        },
    )
    vertical: Optional[bool] = field(
        default=None,
        metadata={
            "type": "Attribute",
        },
    )
    first_line_indent: Optional[bool] = field(
        default=None,
        metadata={
            "name": "FirstLineIndent",
            "type": "Attribute",
        },
    )


@dataclass
class Page:
    class Meta:
        name = "page"

    mediabox: Optional[Mediabox] = field(
        default=None,
        metadata={
            "type": "Element",
            "required": True,
        },
    )
    cropbox: Optional[Cropbox] = field(
        default=None,
        metadata={
            "type": "Element",
            "required": True,
        },
    )
    page_layout: list[PageLayout] = field(
        default_factory=list,
        metadata={
            "name": "pageLayout",
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
    base_operations: Optional[BaseOperations] = field(
        default=None,
        metadata={
            "name": "baseOperations",
            "type": "Element",
            "required": True,
        },
    )
    page_number: Optional[int] = field(
        default=None,
        metadata={
            "name": "pageNumber",
            "type": "Attribute",
            "required": True,
        },
    )
    unit: Optional[str] = field(
        default=None,
        metadata={
            "name": "Unit",
            "type": "Attribute",
            "required": True,
        },
    )


@dataclass
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
    total_pages: Optional[int] = field(
        default=None,
        metadata={
            "name": "totalPages",
            "type": "Attribute",
            "required": True,
        },
    )
