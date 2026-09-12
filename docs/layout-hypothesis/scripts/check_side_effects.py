"""Check practical side effects of the LaTeX-overlay approach.

Questions the hypothesis reviewer will ask, each answered with a measurement:

1. Does erasing the box remove the *source* text from the text layer, or does the
   old content remain extractable underneath (doubled text)?
2. Are the CJK fonts embedded in the stamp and in the final PDF?
3. Is the overlaid text searchable/copyable as contiguous text?
4. What does the overlay cost in time and file size?
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


def fonts_of(page: fitz.Page) -> list[dict]:
    return [
        {"name": f[3], "type": f[2], "embedded": f[1] != ""}
        for f in page.get_fonts(full=True)
    ]


def check_side_effects() -> dict:
    geom = json.loads((JOB / "agent" / "layout_geometry.json").read_text())
    translated = {}
    for line in (JOB / "agent" / "translated.jsonl").read_text().splitlines():
        if line.strip():
            obj = json.loads(line)
            translated[obj["id"]] = obj["target"]

    para = next(p for p in geom["paragraphs"] if p["id"] == "P02-001")
    il_box = para["src_box"]
    body = strip_style_markup(translated["P02-001"])
    w, h = il_box[2] - il_box[0], il_box[3] - il_box[1]
    fs = float(para["src_font_size"])

    doc = fitz.open(PDF)
    page_h = doc[1].rect.height
    original_clip = doc[1].get_text("text", clip=fitz.Rect(
        il_box[0], page_h - il_box[3], il_box[2], page_h - il_box[1]
    ))
    doc.close()

    stem = "sideeffects"
    rec = compile_tex(
        build_tex(body, w, h, fs, fs * 1.5),
        WORK / stem,
        stem,
        body=body,
        box_w=w,
        box_h=h,
        font_size=fs,
    )

    stamp_doc = fitz.open(rec["pdf"])
    stamp_fonts = fonts_of(stamp_doc[0])
    stamp_bytes = Path(rec["pdf"]).stat().st_size
    stamp_doc.close()

    variants = {}
    for mode in ("none", "draw", "redact"):
        out_pdf = OUT / f"{stem}_{mode}.pdf"
        orec = overlay_box(
            PDF, out_pdf, 1, il_box, Path(rec["pdf"]), page_h, mode=mode
        )
        doc = fitz.open(out_pdf)
        page = doc[1]
        rect = fitz.Rect(orec["rect_mupdf"])
        clip_text = page.get_text("text", clip=rect)
        variants[mode] = {
            **orec,
            "file_size": Path(out_pdf).stat().st_size,
            "translated_first_8_in_clip": body[:8] in clip_text,
            "clip_contains_original_english": "inlining process" in clip_text,
            "n_chinese_fragments_in_clip": clip_text.count("从本质上讲"),
            "clip_chars": len(clip_text.strip()),
            "source_fonts": [
                f for f in fonts_of(page) if "source" in f["name"].lower()
            ],
            "all_fonts_embedded": all(f["embedded"] for f in fonts_of(page)),
            "n_fonts": len(fonts_of(page)),
            "search_hits_for_translation": len(page.search_for(body[:8])),
        }
        doc.close()

    result = {
        "para_id": para["id"],
        "il_box": il_box,
        "requested_w_h": [round(w, 3), round(h, 3)],
        "font_size": fs,
        "stamp_fonts": stamp_fonts,
        "stamp_bytes": stamp_bytes,
        "compile_seconds": rec["seconds"],
        "original_clip_chars": len(original_clip.strip()),
        "original_clip_has_chinese": "从本质上讲" in original_clip,
        "variants": variants,
    }
    (OUT / "side-effects.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return result


def main() -> None:
    result = check_side_effects()
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
