# pyright: reportImportCycles=false

"""Open Packaging Convention (OPC) objects related to package parts."""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING
from typing import cast

from babeldoc.format.office.document_il.opc.oxml import parse_xml_element
from babeldoc.format.office.document_il.opc.oxml import serialize_part_xml
from babeldoc.format.office.document_il.opc.packuri import PackURI
from babeldoc.format.office.document_il.opc.rel import Relationships
from babeldoc.format.office.document_il.opc.shared import cls_method_fn
from babeldoc.format.office.document_il.opc.shared import lazyproperty

if TYPE_CHECKING:
    from babeldoc.format.office.document_il.opc.oxml import BaseOxmlElement
    from babeldoc.format.office.document_il.opc.package import OpcPackage


class Part:
    """Base class for package parts.

    Provides common properties and methods, but intended to be subclassed in client code
    to implement specific part behaviors.
    """

    def __init__(
        self,
        partname: PackURI,
        content_type: str,
        blob: bytes | None = None,
        package: OpcPackage | None = None,
    ):
        super().__init__()
        self._partname = partname
        self._content_type = content_type
        self._blob = blob
        self._package = package

    def after_unmarshal(self):
        """Entry point for post-unmarshaling processing, for example to parse the part
        XML.

        May be overridden by subclasses without forwarding call to super.
        """
        # don't place any code here, just catch call if not overridden by
        # subclass
        pass

    def before_marshal(self):
        """Entry point for pre-serialization processing, for example to finalize part
        naming if necessary.

        May be overridden by subclasses without forwarding call to super.
        """
        # don't place any code here, just catch call if not overridden by
        # subclass
        pass

    @property
    def blob(self) -> bytes:
        """Contents of this package part as a sequence of bytes.

        May be text or binary. Intended to be overridden by subclasses. Default behavior
        is to return load blob.
        """
        return self._blob or b""

    @property
    def content_type(self):
        """Content type of this part."""
        return self._content_type

    def drop_rel(self, rid: str):
        """Remove the relationship identified by `rid` if its reference count is less
        than 2.

        Relationships with a reference count of 0 are implicit relationships.
        """
        if self._rel_ref_count(rid) < 2:
            del self.rels[rid]

    @classmethod
    def load(
        cls, partname: PackURI, content_type: str, blob: bytes, package: OpcPackage
    ):
        return cls(partname, content_type, blob, package)

    def load_rel(
        self, reltype: str, target: Part | str, rid: str, is_external: bool = False
    ):
        """Return newly added |_Relationship| instance of `reltype`.

        The new relationship relates the `target` part to this part with key `rid`.

        Target mode is set to ``RTM.EXTERNAL`` if `is_external` is |True|. Intended for
        use during load from a serialized package, where the rid is well-known. Other
        methods exist for adding a new relationship to a part when manipulating a part.
        """
        return self.rels.add_relationship(reltype, target, rid, is_external)

    @property
    def package(self):
        """|OpcPackage| instance this part belongs to."""
        return self._package

    @property
    def partname(self):
        """|PackURI| instance holding partname of this part, e.g.
        '/ppt/slides/slide1.xml'."""
        return self._partname

    @partname.setter
    def partname(self, partname: str):
        if not isinstance(partname, PackURI):
            tmpl = (
                f"partname must be instance of PackURI, got '{type(partname).__name__}'"
            )
            raise TypeError(tmpl)
        self._partname = partname

    def part_related_by(self, reltype: str) -> Part:
        """Return part to which this part has a relationship of `reltype`.

        Raises |KeyError| if no such relationship is found and |ValueError| if more than
        one such relationship is found. Provides ability to resolve implicitly related
        part, such as Slide -> SlideLayout.
        """
        return self.rels.part_with_reltype(reltype)

    def relate_to(
        self, target: Part | str, reltype: str, is_external: bool = False
    ) -> str:
        """Return rid key of relationship of `reltype` to `target`.

        The returned `rid` is from an existing relationship if there is one, otherwise a
        new relationship is created.
        """
        if is_external:
            return self.rels.get_or_add_ext_rel(reltype, cast(str, target))
        else:
            rel = self.rels.get_or_add(reltype, cast(Part, target))
            return rel.rid

    @property
    def related_parts(self):
        """Dictionary mapping related parts by rid, so child objects can resolve
        explicit relationships present in the part XML, e.g. sldIdLst to a specific
        |Slide| instance."""
        return self.rels.related_parts

    @lazyproperty
    def rels(self):
        """|Relationships| instance holding the relationships for this part."""
        # -- prevent breakage in `python-docx-template` by retaining legacy `._rels` attribute --
        self._rels = Relationships(self._partname.baseURI, self._partname)
        return self._rels

    def target_ref(self, rid: str) -> str:
        """Return URL contained in target ref of relationship identified by `rid`."""
        rel = self.rels[rid]
        return rel.target_ref

    def _rel_ref_count(self, rid: str) -> int:
        """Return the count of references in this part to the relationship identified by `rid`.

        Only an XML part can contain references, so this is 0 for `Part`.
        """
        return 0


class PartFactory:
    """Provides a way for client code to specify a subclass of |Part| to be constructed
    by |Unmarshaller| based on its content type and/or a custom callable.

    Setting ``PartFactory.part_class_selector`` to a callable object will cause that
    object to be called with the parameters ``content_type, reltype``, once for each
    part in the package. If the callable returns an object, it is used as the class for
    that part. If it returns |None|, part class selection falls back to the content type
    map defined in ``PartFactory.part_type_for``. If no class is returned from either of
    these, the class contained in ``PartFactory.default_part_type`` is used to construct
    the part, which is by default ``opc.package.Part``.
    """

    part_class_selector: Callable[[str, str], type[Part] | None] | None
    part_type_for: dict[str, type[Part]] = {}
    default_part_type = Part

    def __new__(
        cls,
        partname: PackURI,
        content_type: str,
        reltype: str,
        blob: bytes,
        package: OpcPackage,
    ):
        part_class: type[Part] | None = None
        if cls.part_class_selector is not None:
            part_class_selector = cls_method_fn(cls, "part_class_selector")
            part_class = part_class_selector(content_type, reltype)
        if part_class is None:
            part_class = cls._part_cls_for(content_type)
        return part_class.load(partname, content_type, blob, package)

    @classmethod
    def _part_cls_for(cls, content_type: str):
        """Return the custom part class registered for `content_type`, or the default
        part class if no custom class is registered for `content_type`."""
        if content_type in cls.part_type_for:
            return cls.part_type_for[content_type]
        return cls.default_part_type


def part_class_selector(content_type: str, reltype: str) -> type[Part] | None:
    """Select a part class based on content type and relationship type.

    This function is assigned to PartFactory.part_class_selector and is called
    for each part in the package. By default it returns None, causing the
    selection to fall back to the content type map.

    Args:
        content_type: The content type of the part
        reltype: The relationship type of the part

    Returns:
        A Part subclass or None if no specific class is selected
    """
    return None


PartFactory.part_class_selector = part_class_selector


class XmlPart(Part):
    """Base class for package parts containing an XML payload, which is most of them.

    Provides additional methods to the |Part| base class that take care of parsing and
    reserializing the XML payload and managing relationships to other parts.
    """

    def __init__(
        self,
        partname: PackURI,
        content_type: str,
        element: BaseOxmlElement,
        package: OpcPackage,
    ):
        super().__init__(partname, content_type, package=package)
        self._element = element

    @property
    def blob(self):
        return serialize_part_xml(self._element)

    @property
    def element(self):
        """The root XML element of this XML part."""
        return self._element

    @classmethod
    def load(
        cls, partname: PackURI, content_type: str, blob: bytes, package: OpcPackage
    ):
        element = parse_xml_element(blob)
        return cls(partname, content_type, element, package)

    @property
    def part(self):
        """Return |Part| subclass instance containing this XML element.

        This is a circular reference in the case where this XML element is the root
        element of the part, as it is in all current use cases. This is nonetheless
        a useful and reliable way to get the part from an XML element.
        """
        return self

    def _rel_ref_count(self, rid: str) -> int:
        """Return the count of references in this part's XML to the relationship
        identified by `rid`."""
        rids = cast("list[str]", self._element.xpath("//@r:id"))
        return len([_rid for _rid in rids if _rid == rid])
