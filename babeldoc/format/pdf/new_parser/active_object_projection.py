from __future__ import annotations

from collections.abc import Callable

from babeldoc.format.pdf.new_parser.active_object_backend import create_active_literal
from babeldoc.format.pdf.new_parser.active_object_backend import create_active_stream
from babeldoc.format.pdf.new_parser.active_value_access import obj_ref_id
from babeldoc.format.pdf.new_parser.object_model import PdfObjectStream


def project_font_spec(
    spec: dict[object, object],
    *,
    resolve_indirect: Callable[[object], object] | None = None,
) -> dict[object, object]:
    resolved = _resolve_all(spec, resolve_indirect or (lambda value: value))
    projected = _project_value(resolved)
    if not isinstance(projected, dict):
        raise TypeError(f"Projected font spec must be dict, got {type(projected)}")
    return projected


def _resolve_all(value: object, resolve_indirect: Callable[[object], object]) -> object:
    ref_id = obj_ref_id(value)
    if ref_id is not None:
        resolved = resolve_indirect(value)
        if resolved is value:
            return value
        return _resolve_all(resolved, resolve_indirect)
    if isinstance(value, list):
        return [_resolve_all(item, resolve_indirect) for item in value]
    if isinstance(value, dict):
        return {
            key: _resolve_all(item, resolve_indirect) for key, item in value.items()
        }
    if isinstance(value, PdfObjectStream):
        return PdfObjectStream(
            attrs={
                key: _resolve_all(item, resolve_indirect)
                for key, item in value.attrs.items()
            },
            rawdata=value.rawdata,
            objid=value.objid,
            decoded=value.decoded,
        )
    return value


def _project_value(value: object) -> object:
    if isinstance(value, str):
        if value.isascii():
            return create_active_literal(value)
        return create_active_literal(value.encode("latin-1"))
    if isinstance(value, list):
        return [_project_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _project_value(item) for key, item in value.items()}
    if isinstance(value, PdfObjectStream):
        attrs = {
            key: _project_value(item)
            for key, item in _project_stream_attrs(value).items()
        }
        stream = create_active_stream(attrs, value.rawdata)
        if value.objid is not None:
            stream.set_objid(value.objid, 0)
        return stream
    return value


def _project_stream_attrs(value: PdfObjectStream) -> dict[object, object]:
    if not value.decoded:
        return value.attrs

    return {
        key: item
        for key, item in value.attrs.items()
        if key not in {"Filter", "DecodeParms", "F", "FDecodeParms", "DL", "Length"}
    }
