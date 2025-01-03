from dataclasses import dataclass, field
from typing import Optional


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


@dataclass
class Box1:
    class Meta:
        name = "Box"

    box: Optional[Box] = field(
        default=None,
        metadata={
            "type": "Element",
            "required": True,
        },
    )


@dataclass
class Cropbox(Box1):
    class Meta:
        name = "cropbox"


@dataclass
class Mediabox(Box1):
    class Meta:
        name = "mediabox"


@dataclass
class PageLayout(Box1):
    class Meta:
        name = "pageLayout"

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
class PdfCharacter(Box1):
    class Meta:
        name = "pdfCharacter"

    pdf_font_id: Optional[str] = field(
        default=None,
        metadata={
            "name": "pdfFontId",
            "type": "Attribute",
            "required": True,
        },
    )
    pdf_character_id: Optional[int] = field(
        default=None,
        metadata={
            "name": "pdfCharacterId",
            "type": "Attribute",
            "required": True,
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
            "required": True,
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
    pdf_character: list[PdfCharacter] = field(
        default_factory=list,
        metadata={
            "name": "pdfCharacter",
            "type": "Element",
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
