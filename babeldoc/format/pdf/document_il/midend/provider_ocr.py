"""Optional OCR text fusion for the MinerU provider.

MinerU returns considerably better text for PDFs whose embedded ToUnicode map is
missing or corrupt.  The layout adapter already persists that response as a
provider IR, but historically only its boxes were consumed by BabelDOC.  This
pass uses the IR as a *conservative* text source before paragraph discovery:

* only ``text`` spans are considered (formula/image/table spans are left alone);
* a span is applied only when its OCR text has exactly the same number of
  characters as the native characters mapped to its box; and
* formula spans are excluded even when boxes overlap a broad text span.

Keeping the one-character-to-one-glyph invariant means styles, advances and
links remain attached to the same ``PdfCharacter`` objects.  A mismatch is
recorded and skipped rather than guessed.  The pass is opt-in through
``TranslationConfig.mineru_use_ocr_text`` so existing jobs retain their exact
behaviour.
"""

from __future__ import annotations

import json
import logging
import unicodedata
from pathlib import Path
from typing import Any

from babeldoc.docvision.provider_ir import ProviderPage
from babeldoc.docvision.provider_ir import ProviderSpan
from babeldoc.format.pdf.document_il import il_version_1
from babeldoc.format.pdf.document_il.midend.inline_math_protector import (
    load_provider_document,
)
from babeldoc.format.pdf.document_il.utils.provider_alignment import align_page
from babeldoc.format.pdf.document_il.utils.provider_alignment import to_il_box

logger = logging.getLogger(__name__)

# MinerU uses these kinds for ordinary OCR text.  ``title`` etc. are block
# types, while spans themselves are normally ``text``; accepting the aliases
# makes replay files from older API versions useful too.
OCR_TEXT_KINDS = frozenset({"text", "title", "ref_text", "phonetic"})
FORMULA_KINDS = frozenset({"inline_equation", "interline_equation"})


def _page_height(page: il_version_1.Page) -> float:
    cropbox = getattr(page, "cropbox", None)
    box = getattr(cropbox, "box", None)
    if box is None:
        return 0.0
    return float(box.y2) - float(box.y)


def _clean_ocr_text(value: str) -> str:
    """Normalise harmless OCR whitespace while preserving character count."""
    # Newlines make a span impossible to represent on one PdfCharacter run.
    # Convert non-breaking spaces only; ordinary spaces are kept as-is.
    return (value or "").replace("\u00a0", " ").replace("\u2007", " ")


def _comparison_key(value: str) -> str:
    """Comparison key used to avoid rewriting identical native text."""
    value = unicodedata.normalize("NFKC", value or "")
    return "".join(value.split())


def _iter_spans(provider_page: ProviderPage):
    for block in provider_page.iter_blocks(recursive=True):
        for line in block.lines:
            yield from line.spans


def _span_native_indices(page, span: ProviderSpan, page_height: float) -> list[int]:
    """Return native glyphs covered by a span, including glyphs claimed by a
    more precise overlapping span (usually an inline formula).

    ``align_page`` intentionally assigns each glyph to one best span.  OCR text
    spans are often broad boxes that also contain a formula, however, so using
    only that assignment would make the OCR length appear inconsistent and
    discard the surrounding text.  Recomputing the geometric membership lets us
    pair the complete OCR string with the complete native run, then skip formula
    positions safely during mutation.
    """
    box = to_il_box(span.bbox, page_height)
    if box is None:
        return []
    indices: list[int] = []
    for index, char in enumerate(page.pdf_character or []):
        native_box = char.visual_bbox.box if char.visual_bbox is not None else char.box
        if native_box is None:
            continue
        cx = (float(native_box.x) + float(native_box.x2)) / 2
        cy = (float(native_box.y) + float(native_box.y2)) / 2
        if box.x <= cx <= box.x2 and box.y <= cy <= box.y2:
            indices.append(index)
    return indices


def _output_path(translate_config) -> Path | None:
    directory = getattr(translate_config, "provider_ir_dir", None)
    if not directory:
        working_dir = getattr(translate_config, "working_dir", None)
        directory = Path(working_dir) / "agent" if working_dir else None
    if not directory:
        return None
    return Path(directory) / "source" / "mineru" / "ocr_fusion.json"


class ProviderOcrTextFusion:
    """Replace safely mappable native glyph text with MinerU OCR text."""

    stage_name = "Fuse MinerU OCR"

    def __init__(self, translate_config):
        self.translate_config = translate_config

    def process(self, docs: il_version_1.Document):
        if not getattr(self.translate_config, "mineru_use_ocr_text", False):
            return docs

        provider_document = load_provider_document(
            getattr(self.translate_config, "provider_ir_dir", None),
            getattr(self.translate_config, "working_dir", None),
        )
        if provider_document is None:
            logger.info("MinerU OCR text fusion enabled but provider IR is missing")
            return docs

        by_index = {page.page_index: page for page in provider_document.pages}
        pages_report: list[dict[str, Any]] = []
        totals = {"spans": 0, "applied": 0, "skipped": 0, "changed_chars": 0}
        for page in docs.page:
            provider_page = by_index.get(page.page_number)
            if provider_page is None:
                continue
            report = self._fuse_page(page, provider_page)
            pages_report.append(report)
            for key in totals:
                totals[key] += report.get(key, 0)

        self._write_report({"version": 1, "summary": totals, "pages": pages_report})
        return docs

    def _fuse_page(self, page: il_version_1.Page, provider_page: ProviderPage):
        chars = list(page.pdf_character or [])
        height = _page_height(page)
        if not chars or height <= 0:
            return {"page_index": page.page_number, "spans": 0, "applied": 0, "skipped": 0, "changed_chars": 0, "replacements": []}

        alignment = align_page(page, provider_page, height)
        span_by_id = {span.span_id: span for span in _iter_spans(provider_page)}
        # Any native glyph mapped to a formula span is protected from text OCR,
        # even if a large text span also contains it.
        formula_char_indices = {
            index
            for span_id, indices in alignment.span_to_chars.items()
            if (span_by_id.get(span_id) and span_by_id[span_id].kind in FORMULA_KINDS)
            for index in indices
        }
        report = {
            "page_index": page.page_number,
            "spans": 0,
            "applied": 0,
            "skipped": 0,
            "changed_chars": 0,
            "replacements": [],
        }
        for span_id, indices in alignment.span_to_chars.items():
            span = span_by_id.get(span_id)
            if span is None or span.kind not in OCR_TEXT_KINDS:
                continue
            report["spans"] += 1
            # Keep the provider/native positional correspondence before
            # filtering formula glyphs.  A broad OCR text span commonly covers
            # an inline equation as well; its text still has one character per
            # native glyph, so apply only the non-formula positions while
            # preserving the original offsets.  Requiring the OCR length to
            # match this complete mapping keeps the fusion conservative.
            # Include glyphs assigned to overlapping, more precise spans.  This
            # preserves positional correspondence for a broad text span that
            # encloses an inline formula.
            all_indices = sorted(
                set(indices)
                | set(_span_native_indices(page, span, height))
            )
            indices = [index for index in all_indices if index not in formula_char_indices]
            text = _clean_ocr_text(span.content)
            if not text or "\n" in text or "\r" in text or len(text) != len(all_indices):
                report["skipped"] += 1
                continue
            native = "".join(chars[index].char_unicode or "" for index in all_indices)
            if _comparison_key(native) == _comparison_key(text):
                # Avoid touching objects when OCR adds no information.
                continue
            old_text = native
            changed = 0
            for index, value in zip(all_indices, text, strict=True):
                if index in formula_char_indices:
                    continue
                if chars[index].char_unicode != value:
                    chars[index].char_unicode = value
                    changed += 1
            if changed:
                report["applied"] += 1
                report["changed_chars"] += changed
                if len(report["replacements"]) < 100:
                    report["replacements"].append(
                        {
                            "span_id": span.span_id,
                            "native": old_text,
                            "ocr": text,
                            "char_count": len(indices),
                        }
                    )
        return report

    def _write_report(self, report: dict[str, Any]) -> None:
        path = _output_path(self.translate_config)
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            temporary.replace(path)
        except OSError:
            logger.warning("写入 MinerU OCR 融合报告失败", exc_info=True)


__all__ = ["ProviderOcrTextFusion", "OCR_TEXT_KINDS", "FORMULA_KINDS"]
