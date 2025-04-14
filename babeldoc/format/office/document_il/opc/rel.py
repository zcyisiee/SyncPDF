"""Relationship-related objects."""

from __future__ import annotations

from typing import TYPE_CHECKING
from typing import Any
from typing import cast

from babeldoc.format.office.document_il.opc.oxml import CT_Relationships

if TYPE_CHECKING:
    from babeldoc.format.office.document_il.opc.part import Part


class Relationships(dict[str, "_Relationship"]):
    """Collection object for |_Relationship| instances, having list semantics."""

    def __init__(self, base_uri: str, belongs_to: str = None):
        super().__init__()
        self._base_uri = base_uri
        self._target_parts_by_rid: dict[str, Any] = {}

        if base_uri != "/" and belongs_to is None:
            raise ValueError(
                "belongs_to is required when base_uri is not PACKAGE_URI.base_uri"
            )

        self._belongs_to = belongs_to

    def add_relationship(
        self, reltype: str, target: Part | str, rid: str, is_external: bool = False
    ) -> _Relationship:
        """Return a newly added |_Relationship| instance."""
        rel = _Relationship(
            rid, reltype, target, self._base_uri, self._belongs_to, is_external
        )
        self[rid] = rel
        if not is_external:
            self._target_parts_by_rid[rid] = target
        return rel

    def get_or_add(self, reltype: str, target_part: Part) -> _Relationship:
        """Return relationship of `reltype` to `target_part`, newly added if not already
        present in collection."""
        rel = self._get_matching(reltype, target_part)
        if rel is None:
            rid = self._next_rid
            rel = self.add_relationship(reltype, target_part, rid)
        return rel

    def get_or_add_ext_rel(self, reltype: str, target_ref: str) -> str:
        """Return rid of external relationship of `reltype` to `target_ref`, newly added
        if not already present in collection."""
        rel = self._get_matching(reltype, target_ref, is_external=True)
        if rel is None:
            rid = self._next_rid
            rel = self.add_relationship(reltype, target_ref, rid, is_external=True)
        return rel.rid

    def part_with_reltype(self, reltype: str) -> Part:
        """Return target part of rel with matching `reltype`, raising |KeyError| if not
        found and |ValueError| if more than one matching relationship is found."""
        rel = self._get_rel_of_type(reltype)
        return rel.target_part

    @property
    def related_parts(self):
        """Dict mapping rids to target parts for all the internal relationships in the
        collection."""
        return self._target_parts_by_rid

    @property
    def xml(self) -> str:
        """Serialize this relationship collection into XML suitable for storage as a
        .rels file in an OPC package."""
        rels_elm = CT_Relationships.new()
        for rel in self.values():
            rels_elm.add_rel(rel.rid, rel.reltype, rel.target_ref, rel.is_external)
        return rels_elm.xml

    def _get_matching(
        self, reltype: str, target: Part | str, is_external: bool = False
    ) -> _Relationship | None:
        """Return relationship of matching `reltype`, `target`, and `is_external` from
        collection, or None if not found."""

        def matches(
            rel: _Relationship, reltype: str, target: Part | str, is_external: bool
        ):
            if rel.reltype != reltype:
                return False
            if rel.is_external != is_external:
                return False
            rel_target = rel.target_ref if rel.is_external else rel.target_part
            if rel_target != target:
                return False
            return True

        for rel in self.values():
            if matches(rel, reltype, target, is_external):
                return rel
        return None

    def _get_rel_of_type(self, reltype: str):
        """Return single relationship of type `reltype` from the collection.

        Raises |KeyError| if no matching relationship is found. Raises |ValueError| if
        more than one matching relationship is found.
        """
        matching = [rel for rel in self.values() if rel.reltype == reltype]
        if len(matching) == 0:
            tmpl = f"no relationship of type '{reltype}' in collection"
            raise KeyError(tmpl)
        if len(matching) > 1:
            tmpl = f"multiple relationships of type '{reltype}' in collection"
            raise ValueError(tmpl)
        return matching[0]

    @property
    def _next_rid(self) -> str:  # pyright: ignore[reportReturnType]
        """Next available rid in collection, starting from 'rId1' and making use of any
        gaps in numbering, e.g. 'rId2' for rids ['rId1', 'rId3']."""
        for n in range(1, len(self) + 2):
            rid_candidate = f"rId{n}"  # like 'rId19'
            if rid_candidate not in self:
                return rid_candidate


class _Relationship:
    """Value object for relationship to part."""

    def __init__(
        self,
        rid: str,
        reltype: str,
        target: Part | str,
        base_uri: str,
        belongs_to: str,
        external: bool = False,
    ):
        super().__init__()
        self._belongs_to = belongs_to
        self._rid = rid
        self._reltype = reltype
        self._target = target
        self._base_uri = base_uri
        self._is_external = bool(external)

    @property
    def is_external(self) -> bool:
        return self._is_external

    @property
    def reltype(self) -> str:
        return self._reltype

    @property
    def rid(self) -> str:
        return self._rid

    @property
    def target_part(self) -> Part:
        if self._is_external:
            raise ValueError(
                "target_part property on _Relationship is undef"
                "ined when target mode is External"
            )
        return cast("Part", self._target)

    @property
    def target_ref(self) -> str:
        if self._is_external:
            return cast(str, self._target)
        else:
            target = cast("Part", self._target)
            return target.partname.relative_ref(self._base_uri)
