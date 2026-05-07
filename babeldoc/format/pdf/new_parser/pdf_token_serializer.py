from __future__ import annotations

from numbers import Real

from babeldoc.format.pdf.new_parser.object_model import ActiveLiteral
from babeldoc.format.pdf.new_parser.object_model import PdfIndirectRef
from babeldoc.format.pdf.new_parser.object_model import PdfObjectStream
from babeldoc.format.pdf.new_parser.tokenizer import PdfKeyword
from babeldoc.format.pdf.new_parser.tokenizer import PdfName
from babeldoc.format.pdf.new_parser.tokenizer import PdfString

PDF_KEYWORDS = {"true": True, "false": False, "null": None}


def serialize_pdf_token(value: object) -> str:
    """Serialize known parser PDF token objects for content-stream output."""
    if value is True:
        return "true"
    if value is False:
        return "false"
    if value is None:
        return "null"
    if isinstance(value, Real):
        if isinstance(value, int):
            return str(value)
        return f"{float(value):f}"
    if isinstance(value, str):
        return value
    if isinstance(value, bytes):
        return f"<{value.hex().upper()}>"
    if isinstance(value, PdfName):
        return f"/{value.value}"
    if isinstance(value, ActiveLiteral):
        return f"/{_decode_name(value.name)}"
    if isinstance(value, PdfKeyword):
        return value.value
    if _is_ps_literal(value):
        return f"/{_decode_name(value.name)}"
    if isinstance(value, PdfIndirectRef):
        return f"{value.objid} {value.generation} R"
    if isinstance(value, PdfString):
        return _serialize_pdf_string(value)
    if isinstance(value, list | tuple):
        return f"[{' '.join(serialize_pdf_token(item) for item in value)}]"
    if isinstance(value, dict):
        parts = []
        for key, item in value.items():
            key_name = _dict_key_name(key)
            parts.append(f"/{key_name} {serialize_pdf_token(item)}")
        return f"<< {' '.join(parts)} >>"
    if isinstance(value, PdfObjectStream):
        raise TypeError("PdfObjectStream cannot be serialized as a PDF token.")
    return str(value)


def normalize_pdf_token_value(value: object) -> object:
    """Return a JSON-friendly normalized representation for PDF token values."""
    if isinstance(value, PdfKeyword) and value.value in PDF_KEYWORDS:
        return PDF_KEYWORDS[value.value]
    if isinstance(value, PdfName | ActiveLiteral) or _is_ps_literal(value):
        return serialize_pdf_token(value)
    if isinstance(value, PdfIndirectRef | PdfString):
        return serialize_pdf_token(value)
    if isinstance(value, bytes):
        return serialize_pdf_token(value)
    if isinstance(value, list | tuple):
        return [normalize_pdf_token_value(item) for item in value]
    if isinstance(value, dict):
        return {
            _dict_key_name(key): normalize_pdf_token_value(item)
            for key, item in value.items()
        }
    return value


def _serialize_pdf_string(value: PdfString) -> str:
    if value.is_hex:
        return f"<{value.raw.hex().upper()}>"
    escaped = bytearray()
    escape_map = {
        0x08: b"\\b",
        0x09: b"\\t",
        0x0A: b"\\n",
        0x0C: b"\\f",
        0x0D: b"\\r",
        0x28: b"\\(",
        0x29: b"\\)",
        0x5C: b"\\\\",
    }
    for byte in value.raw:
        escaped.extend(escape_map.get(byte, bytes([byte])))
    return f"({escaped.decode('latin-1')})"


def _dict_key_name(value: object) -> str:
    if isinstance(value, PdfName):
        return value.value
    if isinstance(value, ActiveLiteral):
        return _decode_name(value.name)
    if _is_ps_literal(value):
        return _decode_name(value.name)
    if isinstance(value, str):
        return value[1:] if value.startswith("/") else value
    return str(value)


def _is_ps_literal(value: object) -> bool:
    return type(value).__name__ in {"PSLiteral", "PSKeyword"} and hasattr(value, "name")


def _decode_name(value: object) -> str:
    if isinstance(value, bytes):
        return value.decode("latin-1")
    return str(value)
