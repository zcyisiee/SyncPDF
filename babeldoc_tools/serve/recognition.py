"""Read-only provider block/span geometry, independent of translation selection."""

from __future__ import annotations

import math
from collections import Counter
from collections import defaultdict
from typing import Any


def _box(value: Any) -> dict[str, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    if any(
        isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v)
        for v in value
    ):
        return None
    if value[2] <= value[0] or value[3] <= value[1]:
        return None
    return dict(zip(("x0", "y0", "x1", "y1"), value, strict=True))


def provider_entities(payload: dict, paragraphs: list[dict]) -> list[dict]:
    """Preserve raw labels, nested/discarded blocks and spans in PDF top-left points.

    Only a unique paragraph substantially inside a block is a selection target.
    Spans are read-only and never acquire synthetic editable paragraph identities.
    """
    result: list[dict] = []
    seen: set[str] = set()
    paragraphs_by_page: dict[int, list[tuple[str, dict]]] = defaultdict(list)
    for paragraph in paragraphs:
        page = paragraph.get("page")
        identity = paragraph.get("id")
        raw_box = paragraph.get("box")
        if (
            not isinstance(page, int)
            or not isinstance(identity, str)
            or not isinstance(raw_box, dict)
        ):
            continue
        box = _box([raw_box.get(k) for k in ("x0", "y0", "x1", "y1")])
        if box is not None:
            paragraphs_by_page[page].append((identity, box))

    def add(raw: dict, page: int, kind: str, parent: str | None = None):
        key = raw.get("block_id" if kind == "block" else "span_id")
        box = _box(raw.get("bbox"))
        label = raw.get("type" if kind == "block" else "kind")
        if (
            not isinstance(key, str)
            or not key
            or box is None
            or not isinstance(label, str)
            or not label
        ):
            return None
        identity = f"provider:{kind}:{key}"
        if identity in seen:
            return identity
        seen.add(identity)
        target = None
        if kind == "block":
            candidates = []
            for paragraph_id, p in paragraphs_by_page[page]:
                area = (p["x1"] - p["x0"]) * (p["y1"] - p["y0"])
                overlap = max(
                    0, min(p["x1"], box["x1"]) - max(p["x0"], box["x0"])
                ) * max(0, min(p["y1"], box["y1"]) - max(p["y0"], box["y0"]))
                if overlap / area >= 0.8:
                    candidates.append(paragraph_id)
            if len(candidates) == 1:
                target = candidates[0]
        result.append(
            {
                "id": identity,
                "kind": kind,
                "label": label,
                "page": page,
                "box": box,
                "parent_id": parent,
                "paragraph_id": target,
            }
        )
        return identity

    def walk(blocks: Any, page: int, parent: str | None = None):
        if not isinstance(blocks, list):
            return
        for block in blocks:
            if not isinstance(block, dict):
                continue
            identity = add(block, page, "block", parent)
            lines = block.get("lines")
            for line in lines if isinstance(lines, list) else []:
                if not isinstance(line, dict):
                    continue
                spans = line.get("spans")
                for span in spans if isinstance(spans, list) else []:
                    if isinstance(span, dict):
                        # The IR has already repaired cross-page merged lines.
                        span_page = span.get("page_index", page - 1)
                        if (
                            isinstance(span_page, int)
                            and not isinstance(span_page, bool)
                            and span_page >= 0
                        ):
                            add(span, span_page + 1, "span", identity)
            walk(block.get("children"), page, identity)

    for page in payload["pages"]:
        if not isinstance(page, dict):
            continue
        index = page.get("page_index")
        if isinstance(index, int) and not isinstance(index, bool) and index >= 0:
            walk(page.get("blocks"), index + 1)
    # Small span frames stay visible above their containing block frames.
    return sorted(result, key=lambda row: row["kind"] == "span")


def label_inventory(rows: list[dict], key: str = "label") -> list[dict]:
    counts = Counter(
        row.get(key) if isinstance(row.get(key), str) else None for row in rows
    )
    return [
        {"label": label, "count": count}
        for label, count in sorted(counts.items(), key=lambda item: item[0] or "")
    ]
