"""Verify the bbox -> LaTeX page -> PDF overlay coordinate round trip.

The hypothesis depends on three coordinate conversions being consistent:

1. BabelDOC IL boxes are y-up; MuPDF is y-down. ``y_mupdf = page_h - y_il``.
2. MinerU raw boxes are y-down already, while ``layout_geometry.json`` boxes are
   y-up (converted earlier in the pipeline).
3. The stamp must land exactly where the source paragraph sat.

This script proves (2) numerically by matching MinerU blocks to IL paragraphs and
fills the same text into both boxes, so a wrong flip would be visible as a stale
mirror position.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import fitz

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from latex_box import build_tex  # noqa: E402
from latex_box import compile_tex  # noqa: E402
from latex_box import overlay_box  # noqa: E402
from latex_box import strip_style_markup  # noqa: E402

ROOT = HERE.parents[2]
OUT = ROOT / "docs" / "layout-hypothesis" / "out"
WORK = ROOT / "docs" / "layout-hypothesis" / "work"

JOB = Path("/tmp/babeldoc-three-test.eH8NAn/2026-f1872")
PDF = JOB / "2026-f1872-paper" / "input.pdf"
MINERU = (
    Path.home()
    / ".cache/babeldoc/mineru-layout.v1/f15a0e00eed725df8ed53c5da37250bb2a09ac1dd4c7710013d6b0b8d5ac062e.json"
)


def main() -> None:
    geom = json.loads((JOB / "agent" / "layout_geometry.json").read_text())
    mineru = json.loads(MINERU.read_text())
    doc = fitz.open(PDF)
    page_h = doc[1].rect.height
    doc.close()

    para = next(
        p
        for p in geom["paragraphs"]
        if p["id"] == "P02-001"
    )
    il_box = para["src_box"]
    print(f"IL box (y-up)      : {il_box}")
    print(f"page height        : {page_h}")

    # Find the MinerU block that corresponds to the same visual region.
    page = mineru["pdf_info"][1]
    best = None
    il_as_down = [il_box[0], page_h - il_box[3], il_box[2], page_h - il_box[1]]
    for block in page["para_blocks"]:
        bb = block["bbox"]
        dist = sum(abs(a - b) for a, b in zip(bb, il_as_down, strict=False))
        if best is None or dist < best[0]:
            best = (dist, bb, block["type"])
    print(f"IL box (as y-down) : {[round(v, 3) for v in il_as_down]}")
    print(f"closest MinerU bbox: {[round(v, 3) for v in best[1]]} (type {best[2]}, L1 dist {best[0]:.2f})")

    # Round-trip the IL box through the overlay helper and confirm placement.
    translated = {}
    for line in (JOB / "agent" / "translated.jsonl").read_text().splitlines():
        if line.strip():
            obj = json.loads(line)
            translated[obj["id"]] = obj["target"]
    body = strip_style_markup(translated["P02-001"])
    w, h = il_box[2] - il_box[0], il_box[3] - il_box[1]
    fs = float(para["src_font_size"])
    stem = "coordcheck"
    rec = compile_tex(
        build_tex(body, w, h, fs, fs * 1.5),
        WORK / stem,
        stem,
        body=body,
        box_w=w,
        box_h=h,
        font_size=fs,
    )
    print(f"stamp page size    : {rec['page_rect']} vs requested [{round(w,3)}, {round(h,3)}]")

    out_pdf = OUT / "coordcheck.pdf"
    orec = overlay_box(PDF, out_pdf, 1, il_box, Path(rec["pdf"]), page_h, erase=True)
    print(f"overlay rect(mupdf): {orec['rect_mupdf']}")

    # The stamp's text should now be extractable inside the overlay rect.
    doc = fitz.open(out_pdf)
    page_obj = doc[1]
    rect = fitz.Rect(orec["rect_mupdf"])
    inside = page_obj.get_text("text", clip=rect)
    anywhere = page_obj.get_text()
    print(f"chars inside rect  : {len(inside.strip())}")
    print(f"chars page total   : {len(anywhere)}")
    probe = body[:8]
    print(f"probe {probe!r} inside rect: {probe in inside}")
    # Confirm the position matches by searching for the paragraph opening.
    hits = page_obj.search_for(body[:8])
    print(f"search_for({body[:8]!r}) -> {[[round(v,1) for v in r] for r in hits]}")
    doc.close()

    result = {
        "para_id": para["id"],
        "il_box_yup": il_box,
        "page_height": page_h,
        "il_box_as_y_down": [round(v, 3) for v in il_as_down],
        "closest_mineru_bbox": [round(v, 3) for v in best[1]],
        "mineru_type": best[2],
        "l1_distance": round(best[0], 2),
        "stamp_page_rect": rec["page_rect"],
        "requested_w_h": [round(w, 3), round(h, 3)],
        "overlay_rect_mupdf": orec["rect_mupdf"],
        "chars_inside_rect": len(inside.strip()),
        "search_hits": [[round(v, 1) for v in r] for r in hits],
        "probe_in_clip": probe in inside,
    }
    (OUT / "coordcheck.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )


if __name__ == "__main__":
    main()
