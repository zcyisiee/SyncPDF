"""Run the "LaTeX per bbox" experiment on real BabelDOC job artifacts.

Pipeline per case:
  1. take a real paragraph's bbox + real translated text from ``layout_geometry.json``
     / ``translated.jsonl`` (no LLM, no MinerU API);
  2. compile it with xelatex into a page whose paper size equals the bbox;
  3. measure whether the ink fits; if not, shrink the font size in bounded steps;
  4. overlay the compiled page into the original PDF (bbox-sized),
     after white-out, and report extractability;
  5. also run the same bbox with the *source* MinerU OCR text (formulas) to test
     the LaTeX-specific claim.

Outputs land under ``docs/layout-hypothesis/out`` and ``work``; product code is
never imported for mutation, only for path/invariant context.
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
from latex_box import count_latex_specials  # noqa: E402
from latex_box import count_render_lines  # noqa: E402
from latex_box import escape_latex  # noqa: E402
from latex_box import fit_by_shrink  # noqa: E402
from latex_box import overlay_box  # noqa: E402
from latex_box import render_png  # noqa: E402
from latex_box import strip_style_markup  # noqa: E402
from latex_box import write_json  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
OUT = ROOT / "docs" / "layout-hypothesis" / "out"
WORK = ROOT / "docs" / "layout-hypothesis" / "work"

JOBS = {
    "2026-f1872": {
        "job": Path("/tmp/babeldoc-three-test.eH8NAn/2026-f1872"),
        "pdf": Path("/tmp/babeldoc-three-test.eH8NAn/2026-f1872/2026-f1872-paper/input.pdf"),
        "mineru": Path.home()
        / ".cache/babeldoc/mineru-layout.v1/f15a0e00eed725df8ed53c5da37250bb2a09ac1dd4c7710013d6b0b8d5ac062e.json",
    },
    "2312-04432": {
        "job": Path("/tmp/babeldoc-three-test.eH8NAn/2312-04432"),
        "pdf": Path("/tmp/babeldoc-three-test.eH8NAn/2312-04432/2312.04432v2/input.pdf"),
        "mineru": Path.home()
        / ".cache/babeldoc/mineru-layout.v1/773068fa427a7c594c0bd97361aea23eb5bc68dc513548604c628029c3d0c4e9.json",
    },
}


def load_job(name: str) -> dict:
    spec = JOBS[name]
    geom = json.loads((spec["job"] / "agent" / "layout_geometry.json").read_text())
    translated = {}
    for line in (spec["job"] / "agent" / "translated.jsonl").read_text().splitlines():
        if line.strip():
            obj = json.loads(line)
            translated[obj["id"]] = obj["target"]
    return {"spec": spec, "geometry": geom, "translated": translated}


def pick_paragraphs(geom: dict, translated: dict, page: int = 2, limit: int = 3) -> list[dict]:
    """Body paragraphs on a page with real bbox, font size, line count and translation."""
    chosen = []
    for para in geom["paragraphs"]:
        if para.get("page") != page:
            continue
        if not para.get("overridable"):
            continue
        if para.get("layout_label") not in {"text", "fallback_line", "plain text"}:
            continue
        if not para.get("src_font_size") or not para.get("text"):
            continue
        if para["id"] not in translated:
            continue
        body = strip_style_markup(translated[para["id"]])
        if body.count("{v") > 0:
            continue  # formula placeholders need the pixel path, out of scope here
        if len(body) < 40:
            continue
        box = para["src_box"]
        para = dict(para)
        para["body"] = body
        para["box_w"] = box[2] - box[0]
        para["box_h"] = box[3] - box[1]
        chosen.append(para)
        if len(chosen) >= limit:
            break
    return chosen


def mineru_lines(mineru_path: Path, page_idx: int, limit: int = 400) -> tuple[list[dict], list[float]]:
    """Return MinerU lines (with span typing) plus the page size."""
    doc = json.loads(mineru_path.read_text())
    page = doc["pdf_info"][page_idx]
    lines = []
    for block in page["para_blocks"]:
        for line in block.get("lines") or []:
            spans = line["spans"]
            lines.append(
                {
                    "type": block["type"],
                    "bbox": [round(v, 3) for v in line["bbox"]],
                    "has_formula": any(s["type"] != "text" for s in spans),
                    "text": " ".join(s["content"] for s in spans),
                    "spans": [
                        {"type": s["type"], "content": s["content"]} for s in spans
                    ],
                }
            )
            if len(lines) >= limit:
                return lines, page["page_size"]
    return lines, page["page_size"]


def mineru_line_to_latex(line: dict, wrap_math: bool) -> str:
    """Build a LaTeX body from MinerU spans.

    ``wrap_math=False`` is the naive reading of the proposal: MinerU text is
    pasted as-is. ``wrap_math=True`` wraps ``*_equation`` spans in ``$...$`` and
    escapes plain-text spans, which is what a correct implementation needs.
    """
    parts = []
    for span in line["spans"]:
        content = " ".join(span["content"].split())
        if span["type"] == "text":
            parts.append(escape_latex(content) if wrap_math else content)
        elif wrap_math:
            parts.append(f"${content}$")
        else:
            parts.append(content)
    return " ".join(p for p in parts if p)


def case_translated_baseline(name: str, data: dict) -> dict:
    """Hypothesis applied to real translated text at the paragraph's own font size.

    Two sub-variants per paragraph:
      ``raw``     - translated text pasted into the .tex body verbatim (naive
                    reading of the proposal);
      ``escaped`` - prose escaped for LaTeX, then shrunk only if needed.
    """
    results = []
    for para in pick_paragraphs(data["geometry"], data["translated"], page=2):
        w, h = para["box_w"], para["box_h"]
        fs = float(para["src_font_size"])
        raw = strip_style_markup(para["body"])
        escaped = escape_latex(raw)

        raw_stem = f"{name}_{para['id']}_raw"
        raw_rec = compile_tex(
            build_tex(raw, w, h, fs, fs * 1.5),
            WORK / raw_stem,
            raw_stem,
            body=raw,
            box_w=w,
            box_h=h,
            font_size=fs,
        )

        stem = f"{name}_{para['id']}_translated"
        attempts = fit_by_shrink(
            escaped, w, h, fs, 1.5, WORK / stem, stem, max_steps=8
        )
        first, last = attempts[0], attempts[-1]
        results.append(
            {
                "para_id": para["id"],
                "page": para["page"],
                "layout_label": para["layout_label"],
                "src_box": para["src_box"],
                "box_w": round(w, 3),
                "box_h": round(h, 3),
                "src_font_size": fs,
                "n_unicode": para.get("n_unicode"),
                "n_lines_current_pipeline": para.get("n_lines"),
                "rendered_box_current_pipeline": para.get("rendered_box"),
                "current_scale": para.get("scale"),
                "body_chars": len(raw),
                "latex_specials": count_latex_specials(raw),
                "raw_pass_through": {
                    "compiled": raw_rec["compiled"],
                    "fits": raw_rec["fits"],
                    "fits_reason": raw_rec["fits_reason"],
                    "errors": raw_rec["errors"],
                },
                "first_attempt_fits": first["fits"],
                "first_attempt_reason": first["fits_reason"],
                "first_attempt_overfull": first["overfull_hbox"],
                "first_attempt_ink": first.get("ink_bbox"),
                "final_font_size": last["font_size"],
                "final_fits": last["fits"],
                "final_reason": last["fits_reason"],
                "n_attempts": len(attempts),
                "total_seconds": round(sum(a["seconds"] for a in attempts), 3),
                "attempts": attempts,
            }
        )
    return {"case": "translated-text-at-source-font-size", "results": results}


def case_source_formula_text(name: str, data: dict, page_idx: int = 1) -> dict:
    """Hypothesis applied to MinerU OCR source text, including inline LaTeX.

    This is the literal reading of the user's proposal: feed the LaTeX-bearing
    OCR text straight to the compiler. We compile both the naive pasted form and
    the math-wrapped form to separate "MinerU LaTeX is not compilable as-is"
    from "the box still does not fit".
    """
    lines, page_size = mineru_lines(data["spec"]["mineru"], page_idx)
    sample = [ln for ln in lines if ln["has_formula"]][:3]
    results = []
    for i, ln in enumerate(sample):
        box = ln["bbox"]  # MinerU: y-down, top-left origin
        w, h = box[2] - box[0], box[3] - box[1]
        variants = {}
        for label, wrap in (("naive_passthrough", False), ("math_wrapped", True)):
            body = mineru_line_to_latex(ln, wrap_math=wrap)
            stem = f"{name}_mineru_p{page_idx}_line{i}_{label}"
            variants[label] = compile_tex(
                build_tex(body, w, h, 9.0, 13.5),
                WORK / stem,
                stem,
                body=body,
                box_w=w,
                box_h=h,
                font_size=9.0,
            )
            variants[label]["body"] = body
        results.append(
            {
                "mineru_line": ln,
                "box_w": round(w, 3),
                "box_h": round(h, 3),
                "variants": variants,
            }
        )
    return {"case": "mineru-ocr-latex-text", "page_size": page_size, "results": results}


def case_block_latex(name: str, data: dict, page_idx: int = 1) -> dict:
    """Fairest form of the hypothesis: a whole MinerU paragraph block, incl. LaTeX,
    compiled into a page whose paper size equals that block's bbox.

    This uses the *source* text (not the translation), so it isolates the
    question "can LaTeX lay out this content in this box?" from translation
    quality. Font size is taken from the measured MinerU line height.
    """
    doc = json.loads(data["spec"]["mineru"].read_text())
    page = doc["pdf_info"][page_idx]
    results = []
    for bi, block in enumerate(page["para_blocks"]):
        lines = block.get("lines") or []
        n_eq = sum(1 for ln in lines for s in ln["spans"] if s["type"] != "text")
        if block["type"] != "text" or n_eq == 0:
            continue
        box = block["bbox"]
        w, h = box[2] - box[0], box[3] - box[1]
        # MinerU line bbox height is a good proxy for the source leading.
        heights = [ln["bbox"][3] - ln["bbox"][1] for ln in lines]
        median_h = sorted(heights)[len(heights) // 2] if heights else 12.0
        body = " ".join(mineru_line_to_latex(ln, wrap_math=True) for ln in lines)
        min_w = min(ln["bbox"][2] - ln["bbox"][0] for ln in lines)
        # MinerU line boxes are tight (glyph height); scale slightly so the
        # comparison is about wrapping, not about clipping ascenders.
        fs = round(median_h * 0.78, 3)
        stem = f"{name}_block{bi}_p{page_idx}"
        rec = compile_tex(
            build_tex(body, w, h, fs, median_h),
            WORK / stem,
            stem,
            body=body,
            box_w=w,
            box_h=h,
            font_size=fs,
        )
        rec["body"] = body
        results.append(
            {
                "block_index": bi,
                "n_mineru_lines": len(lines),
                "n_inline_equations": n_eq,
                "block_bbox": [round(v, 3) for v in box],
                "box_w": round(w, 3),
                "box_h": round(h, 3),
                "median_line_h": round(median_h, 3),
                "min_line_w": round(min_w, 3),
                "font_size": fs,
                "mineru_text": " ".join(
                    " ".join(s["content"] for s in ln["spans"]) for ln in lines
                )[:400],
                "compile": rec,
            }
        )
    return {"case": "mineru-block-with-inline-latex", "results": results}


def case_fit_analysis(name: str, data: dict) -> dict:
    """Decompose *why* a translated paragraph overflows its bbox.

    Variants on the same real paragraph and bbox, all evaluated at the source
    font size first, then with a bounded shrink as the fallback:
      ``default_punct``   - xeCJK default punctuation (allows hanging punct);
      ``plain_punct``     - ``PunctStyle=plain``, no hanging punctuation;
      ``plain_punct_shrink`` - plain punctuation + bounded shrink search.

    This separates "LaTeX cannot fit this box" from "xeCJK let a trailing
    punctuation mark hang past the paper edge", which is the actual crux.
    """
    results = []
    for para in pick_paragraphs(data["geometry"], data["translated"], page=2):
        w, h = para["box_w"], para["box_h"]
        fs = float(para["src_font_size"])
        body = escape_latex(strip_style_markup(para["body"]))
        variants = {}
        for label, plain in (("default_punct", False), ("plain_punct", True)):
            stem = f"{name}_{para['id']}_fit_{label}"
            rec = compile_tex(
                build_tex(body, w, h, fs, fs * 1.5, punct_plain=plain),
                WORK / stem,
                stem,
                body=body,
                box_w=w,
                box_h=h,
                font_size=fs,
            )
            variants[label] = rec

        shrink_stem = f"{name}_{para['id']}_fit_plain_punct_shrink"
        attempts = fit_by_shrink(
            body, w, h, fs, 1.5, WORK / shrink_stem, shrink_stem, max_steps=12
        )
        variants["plain_punct_shrink"] = attempts[-1]
        variants["plain_punct_shrink"]["n_attempts"] = len(attempts)
        variants["plain_punct_shrink"]["total_seconds"] = round(
            sum(a["seconds"] for a in attempts), 3
        )

        results.append(
            {
                "para_id": para["id"],
                "box_w": round(w, 3),
                "box_h": round(h, 3),
                "src_font_size": fs,
                "n_lines_current_pipeline": para.get("n_lines"),
                "current_scale": para.get("scale"),
                "variants": {
                    k: {
                        "fits": v["fits"],
                        "fits_reason": v["fits_reason"],
                        "overfull_hbox": v["overfull_hbox"],
                        "ink_bbox": v.get("ink_bbox"),
                        "render_lines": v.get("render_lines"),
                        "line_fill": v.get("line_fill"),
                        "font_size": v["font_size"],
                        "seconds": v["seconds"],
                        "n_attempts": v.get("n_attempts"),
                    }
                    for k, v in variants.items()
                },
            }
        )
    return {"case": "fit-decomposition", "results": results}


def case_sweep(name: str, data: dict, max_paras: int = 30) -> dict:
    """Fit-rate sweep across many real paragraphs at the source font size.

    Reports how often the hypothesis actually needs to shrink the font, which is
    the practical cost question for the proposal.
    """
    geom = data["geometry"]
    translated = data["translated"]
    candidates = []
    for para in geom["paragraphs"]:
        if not para.get("overridable") or para.get("layout_label") != "plain text":
            continue
        if not para.get("src_font_size") or para["id"] not in translated:
            continue
        body = strip_style_markup(translated[para["id"]])
        if "{v" in body or len(body) < 40:
            continue
        candidates.append((para, body))
        if len(candidates) >= max_paras:
            break

    rows = []
    for para, body in candidates:
        box = para["src_box"]
        w, h = box[2] - box[0], box[3] - box[1]
        fs = float(para["src_font_size"])
        escaped = escape_latex(body)
        stem = f"{name}_{para['id']}_sweep"
        rec = compile_tex(
            build_tex(escaped, w, h, fs, fs * 1.5),
            WORK / stem,
            stem,
            body=escaped,
            box_w=w,
            box_h=h,
            font_size=fs,
        )

        # Compare mitigation strategies for the residual failures.
        # ``punct_plain`` is already on in ``build_tex``; ``pad`` grows the LaTeX
        # paper vertically to absorb font metrics taller than the tight source
        # box; ``sloppy`` lets TeX stretch instead of reporting overfull lines.
        variants = {}
        for tag, pad, sloppy in (
            ("as_is", 0.0, False),
            ("pad1", 1.0, False),
            ("pad1_sloppy", 1.0, True),
        ):
            tex = build_tex(escaped, w, h + pad, fs, fs * 1.5)
            if sloppy:
                tex = tex.replace("\\begin{document}", "\\sloppy\n\\begin{document}")
            vstem = f"{name}_{para['id']}_sweep_{tag}"
            rec_v = compile_tex(
                tex,
                WORK / vstem,
                vstem,
                body=escaped,
                box_w=w,
                box_h=h + pad,
                font_size=fs,
            )
            variants[tag] = {
                "fits": rec_v["fits"],
                "reason": rec_v["fits_reason"],
                "overfull": rec_v["overfull_hbox"],
                "render_lines": rec_v.get("render_lines"),
                "seconds": rec_v["seconds"],
            }

        # Bounded shrink as the last resort, mirroring the existing pipeline.
        sstem = f"{name}_{para['id']}_sweep_shrink"
        shrink = fit_by_shrink(escaped, w, h, fs, 1.5, WORK / sstem, sstem, max_steps=10)
        variants["shrink"] = {
            "fits": shrink[-1]["fits"],
            "reason": shrink[-1]["fits_reason"],
            "overfull": shrink[-1]["overfull_hbox"],
            "render_lines": shrink[-1].get("render_lines"),
            "font_size": shrink[-1]["font_size"],
            "n_attempts": len(shrink),
            "seconds": round(sum(a["seconds"] for a in shrink), 3),
        }
        rows.append(
            {
                "para_id": para["id"],
                "page": para["page"],
                "box_w": round(w, 3),
                "box_h": round(h, 3),
                "src_font_size": fs,
                "chars": len(body),
                "fits_at_source_size": variants["as_is"]["fits"],
                "fits_reason": variants["as_is"]["reason"],
                "ink_x1": (rec.get("ink_bbox") or [None] * 4)[2],
                "ink_y1": (rec.get("ink_bbox") or [None] * 4)[3],
                "render_lines": rec.get("render_lines"),
                "min_fill": (rec.get("line_fill") or {}).get("min_fill"),
                "n_lines_current_pipeline": para.get("n_lines"),
                "current_scale": para.get("scale"),
                "variants": variants,
                "seconds": rec["seconds"] + sum(v["seconds"] for v in variants.values()),
            }
        )

    fit = sum(1 for r in rows if r["fits_at_source_size"])
    strategy_fit = {
        name: sum(1 for r in rows if r["variants"][name]["fits"])
        for name in ("as_is", "pad1", "pad1_sloppy", "shrink")
    }
    reasons = {}
    for r in rows:
        if not r["fits_at_source_size"]:
            reasons[r["fits_reason"]] = reasons.get(r["fits_reason"], 0) + 1
    return {
        "case": "fit-sweep",
        "n_sampled": len(rows),
        "n_fits_at_source_size": fit,
        "fit_rate": round(fit / len(rows), 3) if rows else None,
        "strategy_fit_counts": strategy_fit,
        "strategy_fit_rates": {
            k: round(v / len(rows), 3) for k, v in strategy_fit.items()
        },
        "failure_reasons": reasons,
        "total_seconds": round(sum(r["seconds"] for r in rows), 3),
        "rows": rows,
    }


def case_current_pipeline_lines(_name: str, data: dict, page: int = 2) -> dict:
    """Measure the line breaking of the current pipeline output on the same page.

    Used as the baseline the hypothesis claims to improve. Reads the existing
    ``*.mono.pdf`` produced by the prior real job; it does not run the pipeline.
    """
    job_output = data["spec"]["job"] / "output"
    mono = next(job_output.glob("*.mono.pdf"), None)
    if mono is None:
        return {"case": "current-pipeline-lines", "skipped": "no mono pdf"}
    doc = fitz.open(mono)
    page_obj = doc[page - 1]
    crop_w = page_obj.rect.width
    rows = []
    for block in page_obj.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = [s for s in line["spans"] if s["text"].strip()]
            if not spans:
                continue
            text = "".join(s["text"] for s in spans)
            x0 = min(s["bbox"][0] for s in spans)
            x1 = max(s["bbox"][2] for s in spans)
            rows.append(
                {
                    "text": text,
                    "y": round(line["bbox"][1], 2),
                    "width": round(x1 - x0, 2),
                    "n_chars": len(text),
                }
            )
    doc.close()
    return {
        "case": "current-pipeline-lines",
        "pdf": str(mono),
        "page": page,
        "page_width": crop_w,
        "n_lines": len(rows),
        "lines": rows,
    }


def case_overlay(name: str, data: dict) -> dict:
    """Overlay one compiled bbox back onto the real input PDF and measure."""
    para = pick_paragraphs(data["geometry"], data["translated"], page=2, limit=1)[0]
    w, h = para["box_w"], para["box_h"]
    fs = float(para["src_font_size"])
    stem = f"{name}_{para['id']}_overlay"
    attempts = fit_by_shrink(
        escape_latex(strip_style_markup(para["body"])), w, h, fs, 1.5, WORK / stem, stem, max_steps=12
    )
    best = attempts[-1]
    if not best.get("compiled"):
        return {"case": "overlay", "skipped": "no compiled pdf"}

    # IL box (y-up) -> needed because show_pdf_page works in MuPDF y-down space.
    pdf_doc = data["spec"]["pdf"]
    import fitz

    doc = fitz.open(pdf_doc)
    page_h = doc[para["page"] - 1].rect.height
    doc.close()

    out_pdf = OUT / f"{stem}.pdf"
    rec = overlay_box(
        src_pdf=pdf_doc,
        out_pdf=out_pdf,
        page_index=para["page"] - 1,
        box_yup=para["src_box"],
        stamp_pdf=Path(best["pdf"]),
        page_h=page_h,
        erase=True,
    )
    rec["para_id"] = para["id"]
    rec["used_font_size"] = best["font_size"]
    rec["box_yup"] = para["src_box"]
    rec["page_h"] = page_h
    # Verify the overlaid text is present in the text layer.
    doc = fitz.open(out_pdf)
    page = doc[para["page"] - 1]
    probe = strip_style_markup(para["body"])[:8]
    rec["probe_text"] = probe
    rec["probe_found"] = probe in page.get_text()
    rec["render_lines_overlay"] = count_render_lines(page)
    doc.close()

    visuals = {
        "original_input_page": render_png(pdf_doc, para["page"] - 1, OUT / f"{stem}_original.png"),
        "latex_overlay_page": render_png(out_pdf, para["page"] - 1, OUT / f"{stem}_overlay.png"),
        "latex_stamp_page": render_png(Path(best["pdf"]), 0, OUT / f"{stem}_stamp.png"),
    }
    # Current pipeline output for the same page, for a side-by-side baseline.
    job_output = data["spec"]["job"] / "output"
    mono = next(job_output.glob("*.mono.pdf"), None)
    if mono:
        cur = render_png(mono, para["page"] - 1, OUT / f"{stem}_current_pipeline.png")
        doc = fitz.open(mono)
        visuals["current_pipeline_page"] = cur
        visuals["current_pipeline_lines_page"] = count_render_lines(doc[para["page"] - 1])
        doc.close()
    return {"case": "overlay-on-original-pdf", "result": rec, "visuals": visuals}


def main() -> None:
    summary = {"generated_at": time.strftime("%Y-%m-%dT%H:%M:%S"), "cases": {}}
    for name in JOBS:
        data = load_job(name)
        summary["cases"].setdefault(name, {})
        summary["cases"][name]["translated"] = case_translated_baseline(name, data)
        summary["cases"][name]["mineru"] = case_source_formula_text(name, data)
        summary["cases"][name]["block_latex"] = case_block_latex(name, data)
        summary["cases"][name]["fit"] = case_fit_analysis(name, data)
        summary["cases"][name]["sweep"] = case_sweep(name, data)
        summary["cases"][name]["current_lines"] = case_current_pipeline_lines(name, data)
        summary["cases"][name]["overlay"] = case_overlay(name, data)
    write_json(OUT / "experiment-results.json", summary)
    print(json.dumps({"out": str(OUT / "experiment-results.json")}, indent=2))


if __name__ == "__main__":
    main()
