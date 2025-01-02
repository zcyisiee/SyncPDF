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
