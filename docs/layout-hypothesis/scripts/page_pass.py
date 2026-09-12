"""Measure a full-page LaTeX-overlay pass: cost and total fit.

The hypothesis has to work for a whole page, not just one paragraph. This script:

1. takes every translatable body paragraph on a real page;
2. compiles each into its own bbox-sized LaTeX page;
3. overlays all of them (redaction mode, so text is replaced not doubled);
4. reports per-paragraph fit, total wall time, and output sanity.

It deliberately runs on a *separate copy* of the input PDF and never touches the
product pipeline.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path

import fitz

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from latex_box import build_tex  # noqa: E402
from latex_box import compile_tex  # noqa: E402
from latex_box import count_render_lines  # noqa: E402
from latex_box import escape_latex  # noqa: E402
from latex_box import strip_style_markup  # noqa: E402

ROOT = HERE.parents[2]
OUT = ROOT / "docs" / "layout-hypothesis" / "out"
WORK = ROOT / "docs" / "layout-hypothesis" / "work" / "pagepass"

JOBS = {
    "2026-f1872": Path("/tmp/babeldoc-three-test.eH8NAn/2026-f1872"),
    "2312-04432": Path("/tmp/babeldoc-three-test.eH8NAn/2312-04432"),
}


def page_overlay(
    job_name: str, job: Path, page: int, mode: str = "redact", box_pad: float = 0.0
) -> dict:
    geom = json.loads((job / "agent" / "layout_geometry.json").read_text())
    translated = {}
    for line in (job / "agent" / "translated.jsonl").read_text().splitlines():
        if line.strip():
            obj = json.loads(line)
            translated[obj["id"]] = obj["target"]
    pdf = next(p for p in job.rglob("input.pdf"))

    doc = fitz.open(pdf)
    page_h = doc[page - 1].rect.height
    doc.close()

    targets = []
    for para in geom["paragraphs"]:
        if para.get("page") != page or not para.get("overridable"):
            continue
        if para.get("layout_label") not in {"plain text", "title", "section heading"}:
            continue
        if not para.get("src_font_size") or para["id"] not in translated:
            continue
        body = strip_style_markup(translated[para["id"]])
        if "{v" in body or len(body) < 10:
            continue
        targets.append((para, body))

    t0 = time.perf_counter()
    stamps = []
    for para, body in targets:
        box = para["src_box"]
        w, h = box[2] - box[0], box[3] - box[1]
        # Source boxes are tight glyph boxes; ``box_pad`` grows the LaTeX paper
        # vertically so ascender/descender metrics are not clipped.
        padded_h = h + box_pad
        fs = float(para["src_font_size"])
        stem = f"{job_name}_p{page}_{para['id']}_pad{box_pad}"
        rec = compile_tex(
            build_tex(escape_latex(body), w, padded_h, fs, fs * 1.4),
            WORK / stem,
            stem,
            body=escape_latex(body),
            box_w=w,
            box_h=padded_h,
            font_size=fs,
        )
        stamps.append(
            {
                "para_id": para["id"],
                "label": para.get("layout_label"),
                "box": box,
                "w": w,
                "h": h,
                "padded_h": padded_h,
                "font_size": fs,
                "fit": rec["fits"],
                "reason": rec["fits_reason"],
                "overfull": rec["overfull_hbox"],
                "lines_latex": rec.get("render_lines"),
                "lines_current": para.get("n_lines"),
                "seconds": rec["seconds"],
                "pdf": rec.get("pdf"),
            }
        )
    compile_seconds = time.perf_counter() - t0

    # Compose the overlay pass.
    t1 = time.perf_counter()
    doc = fitz.open(pdf)
    page_obj = doc[page - 1]
    for st in stamps:
        if not st["pdf"]:
            continue
        box = st["box"]
        pad = (st.get("padded_h", st["h"]) - st["h"]) / 2.0
        rect = fitz.Rect(box[0], page_h - box[3] - pad, box[2], page_h - box[1] + pad)
        if mode == "redact":
            page_obj.add_redact_annot(rect)
        elif mode == "draw":
            page_obj.draw_rect(rect, color=(1, 1, 1), fill=(1, 1, 1), overlay=True)
    if mode == "redact":
        page_obj.apply_redactions()
    for st in stamps:
        if not st["pdf"]:
            continue
        box = st["box"]
        pad = (st.get("padded_h", st["h"]) - st["h"]) / 2.0
        rect = fitz.Rect(box[0], page_h - box[3] - pad, box[2], page_h - box[1] + pad)
        stamp = fitz.open(st["pdf"])
        page_obj.show_pdf_page(rect, stamp, 0, keep_proportion=False, overlay=True)
        stamp.close()
    out_pdf = OUT / f"{job_name}_page{page}_{mode}_pad{box_pad}_overlay.pdf"
    doc.save(out_pdf, garbage=4, deflate=True)
    compose_seconds = time.perf_counter() - t1
    doc.close()

    check = fitz.open(out_pdf)
    n_pages = check.page_count
    txt = check[page - 1].get_text()
    n_lines_after = count_render_lines(check[page - 1])
    check.close()

    return {
        "job": job_name,
        "page": page,
        "mode": mode,
        "box_pad": box_pad,
        "n_paragraphs": len(stamps),
        "n_fit": sum(1 for s in stamps if s["fit"]),
        "n_overflow": sum(1 for s in stamps if not s["fit"]),
        "compile_seconds_total": round(compile_seconds, 3),
        "compose_seconds": round(compose_seconds, 3),
        "compile_seconds_per_paragraph": round(
            compile_seconds / len(stamps), 3
        )
        if stamps
        else None,
        "out_pdf": str(out_pdf),
        "out_bytes": out_pdf.stat().st_size,
        "page_count": n_pages,
        "chars_on_page": len(txt),
        "render_lines_after": n_lines_after,
        "paragraphs": stamps,
    }


def main() -> None:
    summary = {}
    for name, job in JOBS.items():
        for page in (2, 3):
            for mode in ("redact", "draw"):
                for pad in (0.0, 1.0):
                    key = f"{name}_p{page}_{mode}_pad{pad}"
                    summary[key] = page_overlay(name, job, page, mode=mode, box_pad=pad)
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "page-pass.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for key, s in summary.items():
        print(
            f"{key:34s} paras={s['n_paragraphs']:3d} fit={s['n_fit']:3d} "
            f"overflow={s['n_overflow']:2d} compile={s['compile_seconds_total']:6.2f}s "
            f"compose={s['compose_seconds']:.2f}s lines_after={s['render_lines_after']}"
        )


if __name__ == "__main__":
    main()
