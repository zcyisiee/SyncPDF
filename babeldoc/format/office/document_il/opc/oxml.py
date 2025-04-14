# pyright: reportPrivateUsage=false

"""Temporary stand-in for main oxml module.

This module came across with the PackageReader transplant. Probably much will get
replaced with objects from the pptx.oxml.core and then this module will either get
deleted or only hold the package related custom element classes.
"""

from __future__ import annotations

import datetime as dt
import re
from collections.abc import Callable
from typing import TYPE_CHECKING
from typing import Any
from typing import cast

from babeldoc.format.office.document_il.opc.constants import NAMESPACE as NS
from babeldoc.format.office.document_il.opc.constants import (
    RELATIONSHIP_TARGET_MODE as RTM,
)
from babeldoc.format.office.document_il.opc.xmlchemy import ZeroOrOne
from babeldoc.format.office.document_il.opc.xmlchemy import nsdecls
from babeldoc.format.office.document_il.opc.xmlchemy import qn
from babeldoc.format.office.document_il.opc.xmlchemy import serialize_for_reading
from lxml import etree

# configure XML parser
element_class_lookup = etree.ElementNamespaceClassLookup()
oxml_parser = etree.XMLParser(remove_blank_text=True, resolve_entities=False)
oxml_parser.set_element_class_lookup(element_class_lookup)

if TYPE_CHECKING:
    from lxml.etree import (
        _Element as etree_Element,  # pyright: ignore[reportPrivateUsage]
    )

nsmap = {
    "ct": NS.OPC_CONTENT_TYPES,
    "pr": NS.OPC_RELATIONSHIPS,
    "r": NS.OFC_RELATIONSHIPS,
}


# ===========================================================================
# functions
# ===========================================================================


def parse_xml(text: str) -> etree._Element:
    """`etree.fromstring()` replacement that uses oxml parser."""
    return etree.fromstring(text, oxml_parser)


def parse_xml_element(xml: str | bytes) -> BaseOxmlElement:
    """Root lxml element obtained by parsing XML character string `xml`.

    The custom parser is used, so custom element classes are produced for elements in
    `xml` that have them.
    """
    return cast("BaseOxmlElement", etree.fromstring(xml, oxml_parser))


def qn(tag):
    """Stands for "qualified name", a utility function to turn a namespace prefixed tag
    name into a Clark-notation qualified tag name for lxml.

    For
    example, ``qn('p:cSld')`` returns ``'{http://schemas.../main}cSld'``.
    """
    prefix, tagroot = tag.split(":")
    uri = nsmap[prefix]
    return "{%s}%s" % (uri, tagroot)


def serialize_part_xml(part_elm: etree._Element):
    """Serialize `part_elm` etree element to XML suitable for storage as an XML part.

    That is to say, no insignificant whitespace added for readability, and an
    appropriate XML declaration added with UTF-8 encoding specified.
    """
    return etree.tostring(part_elm, encoding="UTF-8", standalone=True)


def serialize_for_reading(element):
    """Serialize `element` to human-readable XML suitable for tests.

    No XML declaration.
    """
    return etree.tostring(element, encoding="unicode", pretty_print=True)


# ===========================================================================
# Custom element classes
# ===========================================================================


class BaseOxmlElement(etree.ElementBase):
    """Base class for all custom element classes, to add standardized behavior to all
    classes in one place."""

    @property
    def xml(self):
        """Return XML string for this element, suitable for testing purposes.

        Pretty printed for readability and without an XML declaration at the top.
        """
        return serialize_for_reading(self)


class CT_Default(BaseOxmlElement):
    """``<Default>`` element, specifying the default content type to be applied to a
    part with the specified extension."""

    @property
    def content_type(self):
        """String held in the ``ContentType`` attribute of this ``<Default>``
        element."""
        return self.get("ContentType")

    @property
    def extension(self):
        """String held in the ``Extension`` attribute of this ``<Default>`` element."""
        return self.get("Extension")

    @staticmethod
    def new(ext, content_type):
        """Return a new ``<Default>`` element with attributes set to parameter
        values."""
        xml = '<Default xmlns="%s"/>' % nsmap["ct"]
        default = parse_xml(xml)
        default.set("Extension", ext)
        default.set("ContentType", content_type)
        return default


class CT_Override(BaseOxmlElement):
    """``<Override>`` element, specifying the content type to be applied for a part with
    the specified partname."""

    @property
    def content_type(self):
        """String held in the ``ContentType`` attribute of this ``<Override>``
        element."""
        return self.get("ContentType")

    @staticmethod
    def new(partname, content_type):
        """Return a new ``<Override>`` element with attributes set to parameter
        values."""
        xml = '<Override xmlns="%s"/>' % nsmap["ct"]
        override = parse_xml(xml)
        override.set("PartName", partname)
        override.set("ContentType", content_type)
        return override

    @property
    def partname(self):
        """String held in the ``PartName`` attribute of this ``<Override>`` element."""
        return self.get("PartName")


class CT_Relationship(BaseOxmlElement):
    """``<Relationship>`` element, representing a single relationship from a source to a
    target part."""

    @staticmethod
    def new(rId: str, reltype: str, target: str, target_mode: str = RTM.INTERNAL):
        """Return a new ``<Relationship>`` element."""
        xml = '<Relationship xmlns="%s"/>' % nsmap["pr"]
        relationship = parse_xml(xml)
        relationship.set("Id", rId)
        relationship.set("Type", reltype)
        relationship.set("Target", target)
        if target_mode == RTM.EXTERNAL:
            relationship.set("TargetMode", RTM.EXTERNAL)
        return relationship

    @property
    def rId(self):
        """String held in the ``Id`` attribute of this ``<Relationship>`` element."""
        return self.get("Id")

    @property
    def reltype(self):
        """String held in the ``Type`` attribute of this ``<Relationship>`` element."""
        return self.get("Type")

    @property
    def target_ref(self):
        """String held in the ``Target`` attribute of this ``<Relationship>``
        element."""
        return self.get("Target")

    @property
    def target_mode(self):
        """String held in the ``TargetMode`` attribute of this ``<Relationship>``
        element, either ``Internal`` or ``External``.

        Defaults to ``Internal``.
        """
        return self.get("TargetMode", RTM.INTERNAL)


class CT_Relationships(BaseOxmlElement):
    """``<Relationships>`` element, the root element in a .rels file."""

    def add_rel(self, rId: str, reltype: str, target: str, is_external: bool = False):
        """Add a child ``<Relationship>`` element with attributes set according to
        parameter values."""
        target_mode = RTM.EXTERNAL if is_external else RTM.INTERNAL
        relationship = CT_Relationship.new(rId, reltype, target, target_mode)
        self.append(relationship)

    @staticmethod
    def new() -> CT_Relationships:
        """Return a new ``<Relationships>`` element."""
        xml = '<Relationships xmlns="%s"/>' % nsmap["pr"]
        return cast(CT_Relationships, parse_xml(xml))

    @property
    def Relationship_lst(self):
        """Return a list containing all the ``<Relationship>`` child elements."""
        return self.findall(qn("pr:Relationship"))

    @property
    def xml(self):
        """Return XML string for this element, suitable for saving in a .rels stream,
        not pretty printed and with an XML declaration at the top."""
        return serialize_part_xml(self)


class CT_Types(BaseOxmlElement):
    """``<Types>`` element, the container element for Default and Override elements in
    [Content_Types].xml."""

    def add_default(self, ext, content_type):
        """Add a child ``<Default>`` element with attributes set to parameter values."""
        default = CT_Default.new(ext, content_type)
        self.append(default)

    def add_override(self, partname, content_type):
        """Add a child ``<Override>`` element with attributes set to parameter
        values."""
        override = CT_Override.new(partname, content_type)
        self.append(override)

    @property
    def defaults(self):
        return self.findall(qn("ct:Default"))

    @staticmethod
    def new():
        """Return a new ``<Types>`` element."""
        xml = '<Types xmlns="%s"/>' % nsmap["ct"]
        types = parse_xml(xml)
        return types

    @property
    def overrides(self):
        return self.findall(qn("ct:Override"))


class CT_CoreProperties(BaseOxmlElement):
    """`<cp:coreProperties>` element, the root element of the Core Properties part.

    Stored as `/docProps/core.xml`. Implements many of the Dublin Core document metadata
    elements. String elements resolve to an empty string ("") if the element is not
    present in the XML. String elements are limited in length to 255 unicode characters.
    """

    get_or_add_revision: Callable[[], etree_Element]

    category = ZeroOrOne("cp:category", successors=())
    contentStatus = ZeroOrOne("cp:contentStatus", successors=())
    created = ZeroOrOne("dcterms:created", successors=())
    creator = ZeroOrOne("dc:creator", successors=())
    description = ZeroOrOne("dc:description", successors=())
    identifier = ZeroOrOne("dc:identifier", successors=())
    keywords = ZeroOrOne("cp:keywords", successors=())
    language = ZeroOrOne("dc:language", successors=())
    lastModifiedBy = ZeroOrOne("cp:lastModifiedBy", successors=())
    lastPrinted = ZeroOrOne("cp:lastPrinted", successors=())
    modified = ZeroOrOne("dcterms:modified", successors=())
    revision: etree_Element | None = ZeroOrOne(  # pyright: ignore[reportAssignmentType]
        "cp:revision", successors=()
    )
    subject = ZeroOrOne("dc:subject", successors=())
    title = ZeroOrOne("dc:title", successors=())
    version = ZeroOrOne("cp:version", successors=())

    _coreProperties_tmpl = "<cp:coreProperties %s/>\n" % nsdecls("cp", "dc", "dcterms")

    @classmethod
    def new(cls):
        """Return a new `<cp:coreProperties>` element."""
        xml = cls._coreProperties_tmpl
        coreProperties = parse_xml(xml)
        return coreProperties

    @property
    def author_text(self):
        """The text in the `dc:creator` child element."""
        return self._text_of_element("creator")

    @author_text.setter
    def author_text(self, value: str):
        self._set_element_text("creator", value)

    @property
    def category_text(self) -> str:
        return self._text_of_element("category")

    @category_text.setter
    def category_text(self, value: str):
        self._set_element_text("category", value)

    @property
    def comments_text(self) -> str:
        return self._text_of_element("description")

    @comments_text.setter
    def comments_text(self, value: str):
        self._set_element_text("description", value)

    @property
    def contentStatus_text(self):
        return self._text_of_element("contentStatus")

    @contentStatus_text.setter
    def contentStatus_text(self, value: str):
        self._set_element_text("contentStatus", value)

    @property
    def created_datetime(self):
        return self._datetime_of_element("created")

    @created_datetime.setter
    def created_datetime(self, value: dt.datetime):
        self._set_element_datetime("created", value)

    @property
    def identifier_text(self):
        return self._text_of_element("identifier")

    @identifier_text.setter
    def identifier_text(self, value: str):
        self._set_element_text("identifier", value)

    @property
    def keywords_text(self):
        return self._text_of_element("keywords")

    @keywords_text.setter
    def keywords_text(self, value: str):
        self._set_element_text("keywords", value)

    @property
    def language_text(self):
        return self._text_of_element("language")

    @language_text.setter
    def language_text(self, value: str):
        self._set_element_text("language", value)

    @property
    def lastModifiedBy_text(self):
        return self._text_of_element("lastModifiedBy")

    @lastModifiedBy_text.setter
    def lastModifiedBy_text(self, value: str):
        self._set_element_text("lastModifiedBy", value)

    @property
    def lastPrinted_datetime(self):
        return self._datetime_of_element("lastPrinted")

    @lastPrinted_datetime.setter
    def lastPrinted_datetime(self, value: dt.datetime):
        self._set_element_datetime("lastPrinted", value)

    @property
    def modified_datetime(self) -> dt.datetime | None:
        return self._datetime_of_element("modified")

    @modified_datetime.setter
    def modified_datetime(self, value: dt.datetime):
        self._set_element_datetime("modified", value)

    @property
    def revision_number(self):
        """Integer value of revision property."""
        revision = self.revision
        if revision is None:
            return 0
        revision_str = str(revision.text)
        try:
            revision = int(revision_str)
        except ValueError:
            # non-integer revision strings also resolve to 0
            revision = 0
        # as do negative integers
        if revision < 0:
            revision = 0
        return revision

    @revision_number.setter
    def revision_number(self, value: int):
        """Set revision property to string value of integer `value`."""
        if not isinstance(value, int) or value < 1:  # pyright: ignore[reportUnnecessaryIsInstance]
            tmpl = "revision property requires positive int, got '%s'"
            raise ValueError(tmpl % value)
        revision = self.get_or_add_revision()
        revision.text = str(value)

    @property
    def subject_text(self):
        return self._text_of_element("subject")

    @subject_text.setter
    def subject_text(self, value: str):
        self._set_element_text("subject", value)

    @property
    def title_text(self):
        return self._text_of_element("title")

    @title_text.setter
    def title_text(self, value: str):
        self._set_element_text("title", value)

    @property
    def version_text(self):
        return self._text_of_element("version")

    @version_text.setter
    def version_text(self, value: str):
        self._set_element_text("version", value)

    def _datetime_of_element(self, property_name: str) -> dt.datetime | None:
        element = getattr(self, property_name)
        if element is None:
            return None
        datetime_str = element.text
        try:
            return self._parse_W3CDTF_to_datetime(datetime_str)
        except ValueError:
            # invalid datetime strings are ignored
            return None

    def _get_or_add(self, prop_name: str) -> BaseOxmlElement:
        """Return element returned by "get_or_add_" method for `prop_name`."""
        get_or_add_method_name = "get_or_add_%s" % prop_name
        get_or_add_method = getattr(self, get_or_add_method_name)
        element = get_or_add_method()
        return element

    @classmethod
    def _offset_dt(cls, dt_: dt.datetime, offset_str: str) -> dt.datetime:
        """A |datetime| instance offset from `dt_` by timezone offset in `offset_str`.

        `offset_str` is like `"-07:00"`.
        """
        match = cls._offset_pattern.match(offset_str)
        if match is None:
            raise ValueError("'%s' is not a valid offset string" % offset_str)
        sign, hours_str, minutes_str = match.groups()
        sign_factor = -1 if sign == "+" else 1
        hours = int(hours_str) * sign_factor
        minutes = int(minutes_str) * sign_factor
        td = dt.timedelta(hours=hours, minutes=minutes)
        return dt_ + td

    _offset_pattern = re.compile(r"([+-])(\d\d):(\d\d)")

    @classmethod
    def _parse_W3CDTF_to_datetime(cls, w3cdtf_str: str) -> dt.datetime:
        # valid W3CDTF date cases:
        # yyyy e.g. "2003"
        # yyyy-mm e.g. "2003-12"
        # yyyy-mm-dd e.g. "2003-12-31"
        # UTC timezone e.g. "2003-12-31T10:14:55Z"
        # numeric timezone e.g. "2003-12-31T10:14:55-08:00"
        templates = (
            "%Y-%m-%dT%H:%M:%S",
            "%Y-%m-%d",
            "%Y-%m",
            "%Y",
        )
        # strptime isn't smart enough to parse literal timezone offsets like
        # "-07:30", so we have to do it ourselves
        parseable_part = w3cdtf_str[:19]
        offset_str = w3cdtf_str[19:]
        dt_ = None
        for tmpl in templates:
            try:
                dt_ = dt.datetime.strptime(parseable_part, tmpl)
            except ValueError:
                continue
        if dt_ is None:
            tmpl = "could not parse W3CDTF datetime string '%s'"
            raise ValueError(tmpl % w3cdtf_str)
        if len(offset_str) == 6:
            dt_ = cls._offset_dt(dt_, offset_str)
        return dt_.replace(tzinfo=dt.timezone.utc)

    def _set_element_datetime(self, prop_name: str, value: dt.datetime):
        """Set date/time value of child element having `prop_name` to `value`."""
        if not isinstance(value, dt.datetime):  # pyright: ignore[reportUnnecessaryIsInstance]
            tmpl = "property requires <type 'datetime.datetime'> object, got %s"
            raise ValueError(tmpl % type(value))
        element = self._get_or_add(prop_name)
        dt_str = value.strftime("%Y-%m-%dT%H:%M:%SZ")
        element.text = dt_str
        if prop_name in ("created", "modified"):
            # These two require an explicit "xsi:type="dcterms:W3CDTF""
            # attribute. The first and last line are a hack required to add
            # the xsi namespace to the root element rather than each child
            # element in which it is referenced
            self.set(qn("xsi:foo"), "bar")
            element.set(qn("xsi:type"), "dcterms:W3CDTF")
            del self.attrib[qn("xsi:foo")]

    def _set_element_text(self, prop_name: str, value: Any) -> None:
        """Set string value of `name` property to `value`."""
        if not isinstance(value, str):
            value = str(value)

        if len(value) > 255:
            tmpl = "exceeded 255 char limit for property, got:\n\n'%s'"
            raise ValueError(tmpl % value)
        element = self._get_or_add(prop_name)
        element.text = value

    def _text_of_element(self, property_name: str) -> str:
        """The text in the element matching `property_name`.

        The empty string if the element is not present or contains no text.
        """
        element = getattr(self, property_name)
        if element is None:
            return ""
        if element.text is None:
            return ""
        return element.text


ct_namespace = element_class_lookup.get_namespace(nsmap["ct"])
ct_namespace["Default"] = CT_Default
ct_namespace["Override"] = CT_Override
ct_namespace["Types"] = CT_Types

pr_namespace = element_class_lookup.get_namespace(nsmap["pr"])
pr_namespace["Relationship"] = CT_Relationship
pr_namespace["Relationships"] = CT_Relationships
