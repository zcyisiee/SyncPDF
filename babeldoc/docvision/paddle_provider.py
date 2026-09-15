"""Paddle block references aligned conservatively to authoritative PDF characters."""

from __future__ import annotations

import re
import unicodedata

from babeldoc.docvision.provider_ir import ProviderBlock
from babeldoc.docvision.provider_ir import ProviderLine
from babeldoc.docvision.provider_ir import ProviderPage
from babeldoc.docvision.provider_ir import ProviderSpan

_MATH = re.compile(
    r"\$\$([\s\S]+?)\$\$|(?<!\\)\$([^$\n]+?)(?<!\\)\$|\\\((.+?)\\\)|\\\[(.+?)\\\]"
)
_SYMBOLS = {
    "Omega": "Ω",
    "omega": "ω",
    "alpha": "α",
    "beta": "β",
    "gamma": "γ",
    "theta": "θ",
    "forall": "∀",
    "exists": "∃",
    "in": "∈",
    "notin": "∉",
    "neq": "≠",
    "leq": "≤",
    "geq": "≥",
    "times": "×",
    "cdot": "·",
    "cup": "∪",
    "cap": "∩",
    "ldots": "…",
    "cdots": "⋯",
    "infty": "∞",
}


def formula_text(latex: str) -> str | None:
    """Only flatten representations whose visible sequence is understood.

    Fractions, matrices, unknown commands and other complex structures use native
    PDF objects; they are never guessed or assigned a whole-paragraph math box.
    """
    text = latex.strip().strip("$")
    if any(
        text.count(a) != text.count(b) for a, b in [("(", ")"), ("[", "]")]
    ) or re.search(r"[A-Za-z]\.[A-Za-z]\.", text):
        return None
    text = re.sub(r"\\(?:left|right|quad|qquad)\b|\\[,;!]", "", text)
    text = re.sub(
        r"\\(?:mathrm|mathbf|mathit|mathcal|operatorname|text)\s*\{([^{}]*)\}",
        r"\1",
        text,
    )
    text = text.replace(r"\{", "{").replace(r"\}", "}").replace(r"\|", "|")
    for command, symbol in _SYMBOLS.items():
        text = re.sub(r"\\" + command + r"\b", lambda _, s=symbol: s, text)
    if "\\" in text or re.search(r"[A-Za-z]{3}", text):
        return None
    return _normalize(text)


def _normalize(text):
    return "".join(
        c
        for c in unicodedata.normalize("NFKC", text)
        if not c.isspace() and c not in "{}_^"
    )


def _char_box(char):
    return char.visual_bbox.box if char.visual_bbox is not None else char.box


def _native_in_box(page, bbox):
    height = page.cropbox.box.y2 - page.cropbox.box.y
    x0, y0, x1, y1 = bbox
    chars = []
    for char in page.pdf_character or []:
        box = _char_box(char)
        if box is None:
            continue
        x = (box.x + box.x2) / 2
        y = height - (box.y + box.y2) / 2
        if x0 <= x <= x1 and y0 <= y <= y1:
            chars.append(char)
    return chars


def _align_formula(latex, chars, page_height):
    needle = formula_text(latex)
    if not needle:
        return None, "unsupported_latex"
    text = ""
    owners = []
    for char in chars:
        value = _normalize(char.char_unicode or "")
        text += value
        owners.extend([char] * len(value))
    hits = []
    for match in re.finditer(re.escape(needle), text):
        start, end = match.span()
        # A variable inside a prose word is not an equation.
        if len(needle) == 1:
            before = owners[start - 1] if start else None
            after = owners[end] if end < len(owners) else None
            first, last = _char_box(owners[start]), _char_box(owners[end - 1])
            if (
                before
                and (before.char_unicode or "").isalnum()
                and first.x - _char_box(before).x2 < 1
            ):
                continue
            if (
                after
                and (after.char_unicode or "").isalnum()
                and _char_box(after).x - last.x2 < 1
            ):
                continue
        hits.append((start, end))
    if len(hits) != 1:
        return None, "ambiguous_native_match" if hits else "native_text_mismatch"
    start, end = hits[0]
    matched = owners[start:end]
    boxes = [_char_box(c) for c in matched]
    # A reference must not span unrelated lines or columns.
    sizes = [max(1, b.y2 - b.y) for b in boxes]
    if max(b.y2 for b in boxes) - min(b.y for b in boxes) > max(sizes) * 1.8:
        return None, "multiline_native_match"
    box = [
        min(b.x for b in boxes),
        page_height - max(b.y2 for b in boxes),
        max(b.x2 for b in boxes),
        page_height - min(b.y for b in boxes),
    ]
    return box, None


def build_provider_page(page, parsing_blocks, to_points):
    """Preserve block precision; add fine math spans only on unique native matches."""
    blocks = []
    alignment = []
    used_boxes = set()
    height = (
        page.cropbox.box.y2 - page.cropbox.box.y
        if getattr(page, "cropbox", None)
        else 0
    )
    for index, raw in enumerate(parsing_blocks):
        label = raw["block_label"]
        bbox = to_points(raw["block_bbox"])
        recognition = raw.get("recognition") or {}
        content = str(raw.get("block_content") or "")
        if recognition.get("status") == "native_fallback":
            content = ""
        block_id = f"p{page.page_number}-b{index}"
        score = raw.get("score")
        span = ProviderSpan(
            f"{block_id}-l0-s0",
            bbox,
            "formula_reference"
            if label in {"inline_formula", "display_formula"}
            else "text",
            content,
            score,
            page.page_number,
            metadata={"precision": "block", "source": "paddleocr-vl", "readonly": True},
        )
        lines = [ProviderLine(f"{block_id}-l0", bbox, [span])]
        chars = _native_in_box(page, bbox) if height else []
        formulas = (
            [content]
            if label in {"inline_formula", "display_formula"}
            else [
                next(g for g in m.groups() if g is not None)
                for m in _MATH.finditer(content)
            ]
        )
        for fi, latex in enumerate(formulas):
            box, reason = (
                _align_formula(latex, chars, height)
                if chars
                else (None, "no_native_text")
            )
            key = tuple(round(v, 3) for v in box) if box else None
            if key in used_boxes:
                box = None
                reason = "duplicate_native_region"
            record = {
                "page_index": page.page_number,
                "block_id": block_id,
                "latex": latex,
                "bbox": box,
                "status": "aligned" if box else "native_fallback",
                "reason": reason,
            }
            alignment.append(record)
            if box:
                used_boxes.add(key)
                formula = ProviderSpan(
                    f"{block_id}-math{fi}",
                    box,
                    "inline_equation",
                    latex,
                    score,
                    page.page_number,
                    metadata={
                        "precision": "native_characters",
                        "source": "paddleocr-vl",
                        "alignment": "unique_exact",
                        "readonly": True,
                        "recognition": recognition,
                    },
                )
                lines.append(ProviderLine(f"{block_id}-mathline{fi}", box, [formula]))
        order = raw.get("block_order")
        blocks.append(
            ProviderBlock(
                block_id,
                label,
                None,
                bbox,
                None,
                order,
                None,
                lines=lines,
                source="paddleocr-vl:block",
            )
        )
    blocks.sort(key=lambda b: (b.index is None, b.index or 0))
    return ProviderPage(
        page.page_number, blocks, [b.block_id for b in blocks if b.index is not None]
    ), alignment
