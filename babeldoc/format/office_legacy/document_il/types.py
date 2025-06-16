from typing import Any

from pydantic import BaseModel


class ILDataPart(BaseModel):
    part_uri: str
    content_type: str
    xml_content: str


class ILDataRel(BaseModel):
    rId: str
    target: str
    type: str
    baseURI: str
    belongs_to: str
    external: bool


# Document Level
class ILDataDRun(BaseModel):
    type: str
    style: dict
    original_xml: str
    text: str | None = None


# Document Level
class ILDataDElement(BaseModel):
    part_uri: str
    element_type: str
    original_xml: str
    handled_xml: str | None = None
    element_id: str | None = None
    element_index: int | None = None
    context: Any | None = None


# Image data model
class ILDataImage(BaseModel):
    """文档中的图片数据"""

    rId: str
    image_type: str
    dimensions: dict | None = None
    content: str  # Base64 encoded image content
    position: dict | None = None


class ILDataDocument(BaseModel):
    namespaces: dict[str, str] = {}
    elements: list[ILDataDElement] = []


class ILDataMetadata(BaseModel):
    """文档元数据信息"""

    document_settings: dict | None = None
    styles: dict | None = None
    creation_time: str | None = None
    last_modified_time: str | None = None
    author: str | None = None
    application: str | None = None


class ILData(BaseModel):
    content_type: str
    parts: list[ILDataPart] = []
    rels: list[ILDataRel] = []
    document: ILDataDocument = ILDataDocument()
    metadata: ILDataMetadata | None = None
    context: Any | None = None


class ILDocxData(ILData):
    pass


class SharedString(BaseModel):
    """Model for a shared string entry"""

    text: str
    style_hash: str
    style: dict
    element_id: str
    translated_text: str | None = None


class DefinedName(BaseModel):
    """Model for a defined name entry"""

    name: str
    local_sheet_id: str | None = None
    formula: str
    element_id: str
    translated_name: str | None = None


class SheetName(BaseModel):
    """Model for a sheet name"""

    name: str
    sheet_id: str
    r_id: str
    element_id: str
    translated_name: str | None = None


class ILXlsxData(ILData):
    # Use structured models with forward references
    shared_strings: list[SharedString] = []
    sheet_names: list[SheetName] = []
    defined_names: list[DefinedName] = []

    shared_string_root: ILDataDElement | None = None
    shared_string_index_map: dict[str, int] = {}

    # Keep these for ease of lookup
    translated_sheet_names: dict[str, str] = {}
    translated_defined_names: dict[str, str] = {}
    translated_shared_strings: dict[str, str] = {}


class Slide(BaseModel):
    """Model for a slide"""

    slide_id: str
    r_id: str
    element_id: str
    translated_title: str | None = None
    translated_content: str | None = None


class SlideMaster(BaseModel):
    """Model for a slide master"""

    master_id: str
    r_id: str
    element_id: str
    translated_name: str | None = None


class SlideLayout(BaseModel):
    """Model for a slide layout"""

    layout_id: str
    r_id: str
    element_id: str
    translated_name: str | None = None


class ILPptxData(ILData):
    # Use structured models for PPTX-specific data
    slides: list[Slide] = []
    slide_masters: list[SlideMaster] = []
    slide_layouts: list[SlideLayout] = []

    # Keep these for backward compatibility and ease of lookup
    translated_slides: dict[str, str] = {}
    translated_slide_masters: dict[str, str] = {}
    translated_slide_layouts: dict[str, str] = {}


class Element(BaseModel):
    """Base element class for document content"""

    original_xml: str
    handled_xml: str = ""
    text: str | None = None
