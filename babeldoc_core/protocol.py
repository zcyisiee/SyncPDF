"""Structured translation protocol and deterministic safety gates."""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import asdict
from dataclasses import dataclass
from dataclasses import field
from pathlib import Path
from typing import Any


ANCHOR_RE = re.compile(
    r"<!--\s*id\s*=\s*([A-Za-z0-9._:-]+)(?:\s+label\s*=\s*([^>]*?))?\s*-->"
)
PLACEHOLDER_RE = re.compile(
    r"(?:\{[^{}]+\}|<style\b[^>]*>|</style>|<formula\b[^>]*>|</formula>|"
    r"<link\b[^>]*>|</link>|<atom\b[^>]*>|</atom>)"
)
FORMULA_CONTENT_RE = re.compile(r"<formula\b[^>]*>(.*?)</formula>", re.DOTALL)


DEFAULT_SKIP_REASONS = {
    "author": "author",
    "author_info": "author",
    "figure_text": "figure",
    "figure": "figure",
    "table_text": "table",
    "table": "table",
    "reference": "reference",
    "references": "reference",
    "header": "header",
    "footer": "footer",
    "page_number": "page_number",
    "formula": "formula",
    "link": "link",
}


@dataclass(slots=True)
class SkipPolicy:
    """Explicit labels that remain source text during translation."""

    skip_labels: dict[str, str] = field(
        default_factory=lambda: dict(DEFAULT_SKIP_REASONS)
    )

    def should_translate(self, label: str | None) -> bool:
        return not label or label not in self.skip_labels

    def reason(self, label: str | None) -> str | None:
        return self.skip_labels.get(label or "")

    def decision(self, label: str | None) -> dict[str, Any]:
        """Return an auditable translation decision for a layout label."""
        reason = self.reason(label)
        return {
            "translate": reason is None,
            "reason": reason,
            "source_region": label or "text",
            "overridable": reason is not None,
        }

    def to_dict(self) -> dict[str, Any]:
        return {"skip_labels": dict(self.skip_labels)}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SkipPolicy":
        labels = value.get("skip_labels", value)
        return cls(skip_labels=dict(labels or DEFAULT_SKIP_REASONS))


@dataclass(slots=True)
class StyleAtom:
    atom_id: str
    font: str | None = None
    size: float | None = None
    color: str | None = None
    bold: bool = False
    italic: bool = False


@dataclass(slots=True)
class FormulaAtom:
    formula_id: str
    text: str
    bbox: list[float] | None = None
    recognized: bool = False
    render_mode: str = "original_object"
    adjacent_text_safe: bool = True

    @property
    def source_bbox(self) -> list[float] | None:
        return self.bbox


@dataclass(slots=True)
class DocumentBlock:
    block_id: str
    page: int = 1
    layout_label: str = "text"
    source_text: str = ""
    translated_text: str | None = None
    translate: bool = True
    reason: str | None = None
    styles: list[StyleAtom] = field(default_factory=list)
    formulas: list[FormulaAtom] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def target_text(self) -> str:
        return self.source_text if self.translated_text is None else self.translated_text

    @property
    def id(self) -> str:
        return self.block_id

    @property
    def source(self) -> str:
        return self.source_text

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DocumentBlock":
        value = dict(value)
        value["styles"] = [StyleAtom(**item) for item in value.get("styles", [])]
        value["formulas"] = [FormulaAtom(**item) for item in value.get("formulas", [])]
        return cls(**value)


@dataclass(slots=True)
class DocumentIR:
    blocks: list[DocumentBlock] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def paragraphs(self) -> list[DocumentBlock]:
        return self.blocks

    def to_dict(self) -> dict[str, Any]:
        return {"version": 1, "metadata": self.metadata, "blocks": [b.to_dict() for b in self.blocks]}

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DocumentIR":
        return cls(
            blocks=[DocumentBlock.from_dict(item) for item in value.get("blocks", [])],
            metadata=dict(value.get("metadata") or {}),
        )


class ProtocolViolation(ValueError):
    """Raised when a translation cannot safely be written back."""

    def __init__(self, violations: list[str], message: str | None = None):
        self.violations = violations
        super().__init__(message or "; ".join(violations))


def parse_markdown(text: str) -> dict[str, tuple[str, str | None]]:
    """Parse canonical anchor comments and return ``id -> (body, label)``."""

    matches = list(ANCHOR_RE.finditer(text))
    result: dict[str, tuple[str, str | None]] = {}
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        body = text[start:end].strip()
        result[match.group(1)] = (body, (match.group(2) or "").strip() or None)
    return result


def serialize_markdown(ir: DocumentIR) -> str:
    lines = ["<!-- babeldoc:translation-protocol v1 -->", ""]
    for block in ir.blocks:
        label = f" label={block.layout_label}" if block.layout_label else ""
        lines.extend([f"<!-- id={block.block_id}{label} -->", block.target_text.strip(), ""])
    return "\n".join(lines)


def extract_formula_atoms(text: str) -> list[FormulaAtom]:
    """Extract explicit formula atoms without interpreting PDF glyphs."""

    atoms: list[FormulaAtom] = []
    for index, match in enumerate(FORMULA_CONTENT_RE.finditer(text), 1):
        identifier = re.search(r"\bid\s*=\s*['\"]?([A-Za-z0-9._:-]+)", match.group(0))
        atoms.append(
            FormulaAtom(
                formula_id=identifier.group(1) if identifier else f"F-{index:03d}",
                text=match.group(1),
            )
        )
    return atoms


def extract_style_atoms(text: str) -> list[StyleAtom]:
    """Extract style marker IDs for multiset checks."""

    atoms: list[StyleAtom] = []
    for index, match in enumerate(re.finditer(r"<style\b[^>]*>", text), 1):
        identifier = re.search(r"\bid\s*=\s*['\"]?([A-Za-z0-9._:-]+)", match.group(0))
        atoms.append(StyleAtom(identifier.group(1) if identifier else f"S-{index:03d}"))
    return atoms


def _atoms(block: DocumentBlock) -> Counter[str]:
    values = [f"formula:{item.formula_id}" for item in block.formulas]
    values.extend(f"style:{item.atom_id}" for item in block.styles)
    values.extend(PLACEHOLDER_RE.findall(block.source_text))
    return Counter(values)


def atom_multiset(block: DocumentBlock) -> Counter[str]:
    """Return the deterministic multiset used by protocol diagnostics."""

    return _atoms(block)


def validate_translation(
    ir: DocumentIR,
    translated: str | dict[str, tuple[str, str | None]],
    *,
    require_all: bool = True,
) -> list[str]:
    """Return stable violations; an empty list is the protocol pass signal."""

    parsed = parse_markdown(translated) if isinstance(translated, str) else translated
    occurrences: dict[str, list[tuple[str, str | None]]] = {}
    if isinstance(translated, str):
        matches = list(ANCHOR_RE.finditer(translated))
        for index, match in enumerate(matches):
            end = matches[index + 1].start() if index + 1 < len(matches) else len(translated)
            occurrences.setdefault(match.group(1), []).append(
                (translated[match.end() : end].strip(), (match.group(2) or "").strip() or None)
            )
    expected_ids = [block.block_id for block in ir.blocks]
    actual_sequence = [match.group(1) for match in ANCHOR_RE.finditer(translated)] if isinstance(translated, str) else list(parsed)
    actual_ids = list(parsed)
    violations: list[str] = []
    if len(actual_sequence) != len(set(actual_sequence)):
        violations.append("duplicate_ids")
    expected_sequence = [item for item in expected_ids if item in parsed]
    if actual_sequence and actual_sequence != expected_sequence:
        violations.append("paragraph_order_mismatch")
    if require_all:
        for block_id in expected_ids:
            if block_id not in parsed:
                violations.append(f"missing_id:{block_id}")
    for block in ir.blocks:
        if block.block_id not in parsed:
            continue
        body, label = (occurrences.get(block.block_id) or [parsed[block.block_id]])[0]
        if label and block.layout_label and label != block.layout_label:
            violations.append(f"label_mismatch:{block.block_id}")
        if block.translate and not body.strip():
            violations.append(f"empty_translation:{block.block_id}")
        if not block.translate and body.strip() != block.source_text.strip():
            violations.append(f"skipped_changed:{block.block_id}")
        source_atoms = Counter(PLACEHOLDER_RE.findall(block.source_text))
        target_atoms = Counter(PLACEHOLDER_RE.findall(body))
        if source_atoms != target_atoms:
            violations.append(f"placeholder_mismatch:{block.block_id}")
        for formula in block.formulas:
            formula_values = FORMULA_CONTENT_RE.findall(body)
            if formula.text and formula.text not in formula_values:
                violations.append(f"formula_changed:{block.block_id}:{formula.formula_id}")
    if require_all and actual_ids and set(actual_ids) - set(expected_ids):
        violations.extend(f"unknown_id:{item}" for item in actual_ids if item not in expected_ids)
    return violations


class TranslationProtocol:
    """Convenience façade used by callers that prefer an object API."""

    def __init__(self, ir: DocumentIR):
        self.ir = ir

    def serialize(self) -> str:
        return serialize_markdown(self.ir)

    def parse(self, text: str) -> dict[str, tuple[str, str | None]]:
        return parse_markdown(text)

    def validate(self, text: str) -> list[str]:
        return validate_translation(self.ir, text)

    def require_valid(self, text: str) -> None:
        violations = self.validate(text)
        if violations:
            raise ProtocolViolation(violations)


def load_ir(path: str | Path) -> DocumentIR:
    return DocumentIR.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def dump_ir(path: str | Path, ir: DocumentIR) -> None:
    Path(path).write_text(json.dumps(ir.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
