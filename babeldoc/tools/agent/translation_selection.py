"""Deterministic selection of paragraphs that may be sent to a translator.

The PDF layout detector is deliberately treated as a hint: native/fallback
parsing often labels author bands, figure text, and table cells as
``plain text`` or ``fallback_line``.  This module combines explicit labels,
layout-region geometry, and a small amount of document-level context so that
protected content never enters the translation prompt.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


CAPTION_LABELS = {"figure_caption", "table_caption", "code_caption"}
PROTECTED_LABELS = {
    "author",
    "author_info",
    "author_info_hybrid",
    "figure",
    "figure_text",
    "figure_text_hybrid",
    "image",
    "table",
    "table_text",
    "table_footnote",
    "table_cell",
    "table_cell_hybrid",
    "reference",
    "reference_content",
    "reference_hybrid",
    "header",
    "footer",
    "page_header",
    "page_footer",
    "page_number",
    "page_footnote",
    "aside_text",
    "abandon",
}
PROTECTED_REGION_LABELS = PROTECTED_LABELS | {
    "figure_title",
    "table_title",
    "chart_title",
}

_CANONICAL_RE = re.compile(r"<style\b[^>]*>|</style>|\{v\d+\}")
_URL_RE = re.compile(r"https?://\S+|\b(?:doi|isbn)\s*:\s*\S+", re.I)
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}")
_REFERENCE_HEADING_RE = re.compile(
    r"^(?:references?|bibliography|参考文献|文献)$", re.I
)
_APPENDIX_HEADING_RE = re.compile(
    r"^(?:append(?:ix|ices)|open science|artifact|supplement(?:ary)?|附录)", re.I
)
_AUTHOR_NOTE_RE = re.compile(
    r"corresponding author|the author worked|permission to make digital|"
    r"all rights reserved|copyright|permission is required|isbn\b",
    re.I,
)
_AFFILIATION_RE = re.compile(
    r"\b(?:university|institute|college|school|department|laborator(?:y|ies)|"
    r"faculty|research center|centre|gmbh)\b",
    re.I,
)


@dataclass(slots=True)
class SelectionDecision:
    translate: bool
    reason: str | None = None


@dataclass(slots=True)
class SelectionContext:
    """Document-level state used while iterating paragraphs in reading order."""

    references_started: bool = False


def normalize_label(label: str | None) -> str:
    """Canonical form for labels from MinerU, native fallback, or CLI flags.

    Case, surrounding whitespace, interior whitespace, and slashes are all
    folded so that ``"Figure/Table"``, ``"figure table"`` and
    ``"figure/table"`` compare equal.  This is the single normalization used
    for both paragraph labels and caller-supplied ``extra_labels``.
    """
    return re.sub(r"[\s/]+", "_", str(label or "").strip().lower())


def _norm_label(label: str | None) -> str:
    return normalize_label(label)


def protected_reason(label: str | None) -> str | None:
    """Return the normalized label when it is protected, else ``None``."""
    normalized = normalize_label(label)
    return normalized if normalized in PROTECTED_LABELS else None


def _text(paragraph) -> str:
    return _CANONICAL_RE.sub("", getattr(paragraph, "unicode", "") or "").strip()


def _area(box) -> float:
    if box is None:
        return 0.0
    try:
        return max(0.0, float(box.x2 - box.x) * float(box.y2 - box.y))
    except (AttributeError, TypeError, ValueError):
        return 0.0


def _intersection_ratio(inner, outer) -> float:
    """Return intersection / inner area, suitable for nested text boxes."""
    inner_area = _area(inner)
    if not inner_area or outer is None:
        return 0.0
    try:
        x1 = max(inner.x, outer.x)
        y1 = max(inner.y, outer.y)
        x2 = min(inner.x2, outer.x2)
        y2 = min(inner.y2, outer.y2)
        if x2 <= x1 or y2 <= y1:
            return 0.0
        return (x2 - x1) * (y2 - y1) / inner_area
    except (AttributeError, TypeError, ValueError):
        return 0.0


def _region_labels(paragraph, page) -> set[str]:
    labels: set[str] = set()
    pbox = getattr(paragraph, "box", None)
    if pbox is None:
        return labels
    for region in getattr(page, "page_layout", None) or ():
        label = _norm_label(getattr(region, "class_name", None))
        if label not in PROTECTED_REGION_LABELS:
            continue
        # A fallback line inside a table/figure is protected.  Require most
        # of its box to be inside the region; this avoids swallowing ordinary
        # prose next to a figure boundary.
        if _intersection_ratio(pbox, getattr(region, "box", None)) >= 0.55:
            labels.add(label)
    return labels


def _first_page_author_band(paragraph, page, paragraphs) -> bool:
    if getattr(page, "page_number", 0) != 0:
        return False
    label = _norm_label(getattr(paragraph, "layout_label", None))
    if label in CAPTION_LABELS or label in {"title", "doc_title", "paragraph_title"}:
        return False
    title_candidates = []
    abstract_candidates = []
    for candidate in paragraphs:
        ctext = _text(candidate)
        clabel = _norm_label(getattr(candidate, "layout_label", None))
        if clabel in {"title", "doc_title"} and ctext and not re.match(
            r"^(?:abstract|摘要)\b", ctext, re.I
        ):
            title_candidates.append(candidate)
        if re.match(r"^(?:abstract|摘要)\b", ctext, re.I):
            abstract_candidates.append(candidate)
    pbox = getattr(paragraph, "box", None)
    if pbox is None or not title_candidates or not abstract_candidates:
        return False
    title = max(title_candidates, key=lambda item: _area(getattr(item, "box", None)))
    abstract = max(
        abstract_candidates, key=lambda item: getattr(getattr(item, "box", None), "y2", 0)
    )
    title_box = getattr(title, "box", None)
    abstract_box = getattr(abstract, "box", None)
    if title_box is None or abstract_box is None:
        return False
    # PDF coordinates grow upward: the author band lies below the title and
    # above the abstract body.
    return pbox.y2 <= title_box.y + 2 and pbox.y >= abstract_box.y2 - 2


def _is_reference_heading(text: str) -> bool:
    return bool(_REFERENCE_HEADING_RE.match(re.sub(r"\s+", " ", text).strip()))


def _is_reference_boundary(text: str) -> bool:
    """Only these major headings may end a references section.

    Ordinary section headings (``"1. METHOD"``, ``"4. RESULTS"``) appear
    inside reference entries or are simply mis-detected; they must not resume
    translation.  An explicit appendix/open-science/artifact/supplementary
    boundary is the sole signal that a new translatable section started.
    """
    return bool(_APPENDIX_HEADING_RE.match(re.sub(r"\s+", " ", text).strip()))


def select_paragraph(
    paragraph,
    page,
    context: SelectionContext,
    paragraphs,
    extra_labels: set[str] | None = None,
) -> SelectionDecision:
    """Return the translation decision and a stable reason for skipping.

    ``paragraphs`` must be the paragraphs on the current page.  The caller
    should invoke this in page/paragraph reading order and reuse ``context``.
    """
    label = _norm_label(getattr(paragraph, "layout_label", None))
    text = _text(paragraph)
    normalized_extra = {normalize_label(item) for item in (extra_labels or ())}

    if _is_reference_heading(text):
        context.references_started = True
        return SelectionDecision(False, "references_heading")
    if context.references_started:
        if not _is_reference_boundary(text):
            return SelectionDecision(False, "references")
        # Explicit boundary heading: resume translation from here on.
        context.references_started = False

    if label in CAPTION_LABELS:
        return SelectionDecision(True)
    if label in PROTECTED_LABELS or label in normalized_extra:
        return SelectionDecision(False, label)
    if _region_labels(paragraph, page):
        return SelectionDecision(False, "protected_layout_region")
    if _first_page_author_band(paragraph, page, paragraphs):
        return SelectionDecision(False, "author_affiliation")

    crop = getattr(getattr(page, "cropbox", None), "box", None)
    if _AUTHOR_NOTE_RE.search(text):
        if crop is None or getattr(getattr(paragraph, "box", None), "y", 0) <= crop.y + 110:
            return SelectionDecision(False, "page_footnote")
    if _EMAIL_RE.fullmatch(text) or (text and _URL_RE.fullmatch(text)):
        return SelectionDecision(False, "link_or_contact")
    if crop is not None and getattr(getattr(paragraph, "box", None), "y", 0) <= crop.y + 45:
        # Tiny bottom-of-page legal/footer lines are not paper prose.  Keep
        # normal body paragraphs untouched even when they reach the bottom.
        if len(text) > 20 and (_AUTHOR_NOTE_RE.search(text) or _URL_RE.search(text)):
            return SelectionDecision(False, "page_footer")
    return SelectionDecision(True)


def select_page_paragraphs(
    page, context: SelectionContext, extra_labels: set[str] | None = None
):
    """Yield ``(paragraph, decision)`` for one page in current order."""
    paragraphs = list(getattr(page, "pdf_paragraph", None) or ())
    for paragraph in paragraphs:
        yield paragraph, select_paragraph(
            paragraph, page, context, paragraphs, extra_labels=extra_labels
        )
