"""Compare line-breaking quality: current BabelDOC output vs the LaTeX hypothesis.

Reads an existing ``*.mono.pdf`` and ``layout_geometry.json`` from a real job and
measures, per paragraph, how full each non-final line is relative to the
paragraph's own rendered box. A "mysterious" break shows up as a non-final line
with a large empty tail while the next line starts a new line.

Then writes the LaTeX counterpart measurement for the same paragraphs so the two
can be compared side by side.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import fitz

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from latex_box import compile_tex  # noqa: E402
from latex_box import build_tex  # noqa: E402
from latex_box import escape_latex  # noqa: E402
from latex_box import line_fill_metrics  # noqa: E402
from latex_box import strip_style_markup  # noqa: E402

ROOT = HERE.parents[2]
OUT = ROOT / "docs" / "layout-hypothesis" / "out"
WORK = ROOT / "docs" / "layout-hypothesis" / "work"

JOBS = {
    "2026-f1872": Path("/tmp/babeldoc-three-test.eH8NAn/2026-f1872"),
    "2312-04432": Path("/tmp/babeldoc-three-test.eH8NAn/2312-04432"),
}


def current_pipeline_paragraph_fills(job: Path, page: int) -> list[dict]:
    """Per-paragraph non-final-line fill ratios in the current pipeline output."""
    rows, _ = _current_pipeline_fills(job, page, include_lines=False)
    return rows


def _current_pipeline_fills(job: Path, page: int, include_lines: bool = False):
    geom = json.loads((job / "agent" / "layout_geometry.json").read_text())
    mono = next((job / "output").glob("*.mono.pdf"))
    doc = fitz.open(mono)
    page_obj = doc[page - 1]

    # Collect the page's text lines once, then bin them into paragraph boxes.
    lines = []
    for block in page_obj.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = [s for s in line["spans"] if s["text"].strip()]
            if not spans:
                continue
            lines.append(
                {
                    "x0": min(s["bbox"][0] for s in spans),
                    "x1": max(s["bbox"][2] for s in spans),
                    "y0": line["bbox"][1],
                    "y1": line["bbox"][3],
                    "text": "".join(s["text"] for s in spans),
                }
            )
    doc.close()

    rows = []
    for para in geom["paragraphs"]:
        if para.get("page") != page:
            continue
        box = para.get("rendered_box") or para.get("src_box")
        if not box:
            continue
        # rendered_box is IL y-up; convert to MuPDF y-down via page height.
        page_h = 792.0
        geom_page = next((p for p in geom["page_info"] if p["page"] == page), None)
        if geom_page and geom_page.get("cropbox"):
            page_h = geom_page["cropbox"][3] - geom_page["cropbox"][1]
        top, bottom = page_h - box[3], page_h - box[1]
        inside = [
            ln
            for ln in lines
            if ln["y0"] >= top - 2 and ln["y1"] <= bottom + 2 and ln["x0"] >= box[0] - 4
        ]
        if len(inside) < 2:
            continue
        inside.sort(key=lambda ln: ln["y0"])
        box_w = box[2] - box[0]
        fills = [
            round((ln["x1"] - box[0]) / box_w, 3) if box_w else None for ln in inside
        ]
        body = fills[:-1]
        rows.append(
            {
                "para_id": para["id"],
                "layout_label": para.get("layout_label"),
                "n_lines": len(inside),
                "box_w": round(box_w, 3),
                "n_lines_geometry": para.get("n_lines"),
                "fills": fills,
                "min_body_fill": min(body) if body else None,
                "short_nonfinal_lines": sum(1 for f in body if f is not None and f < 0.8),
                "text_head": inside[0]["text"][:40],
            }
        )
    return rows, lines


def latex_paragraph_fills(job_name: str, job: Path, page: int, limit: int = 30) -> list[dict]:
    """Same measurement for the LaTeX-compiled version of each paragraph."""
    geom = json.loads((job / "agent" / "layout_geometry.json").read_text())
    translated = {}
    for line in (job / "agent" / "translated.jsonl").read_text().splitlines():
        if line.strip():
            obj = json.loads(line)
            translated[obj["id"]] = obj["target"]

    rows = []
    for para in geom["paragraphs"]:
        if para.get("page") != page or not para.get("overridable"):
            continue
        if para.get("layout_label") != "plain text" or not para.get("src_font_size"):
            continue
        if para["id"] not in translated:
            continue
        body = strip_style_markup(translated[para["id"]])
        if "{v" in body or len(body) < 40:
            continue
        box = para["src_box"]
        w, h = box[2] - box[0], box[3] - box[1]
        fs = float(para["src_font_size"])
        stem = f"{job_name}_{para['id']}_cmp_latex"
        rec = compile_tex(
            build_tex(escape_latex(body), w, h, fs, fs * 1.5),
            WORK / stem,
            stem,
            body=escape_latex(body),
            box_w=w,
            box_h=h,
            font_size=fs,
        )
        metrics = rec.get("line_fill") or {}
        rows.append(
            {
                "para_id": para["id"],
                "box_w": round(w, 3),
                "box_h": round(h, 3),
                "fits": rec["fits"],
                "fits_reason": rec["fits_reason"],
                "n_lines": metrics.get("n_lines"),
                "fills": metrics.get("fills"),
                "min_body_fill": metrics.get("min_body_fill"),
                "short_nonfinal_lines": metrics.get("short_nonfinal_lines"),
                "seconds": rec["seconds"],
            }
        )
        if len(rows) >= limit:
            break
    return rows


def main() -> None:
    pages = [2, 3, 4, 5]
    summary = {}
    for name, job in JOBS.items():
        current: list[dict] = []
        latex: list[dict] = []
        for page in pages:
            for r in current_pipeline_paragraph_fills(job, page):
                current.append({**r, "page": page})
            for r in latex_paragraph_fills(name, job, page):
                latex.append({**r, "page": page})
        cur_by_id = {r["para_id"]: r for r in current}
        lat_by_id = {r["para_id"]: r for r in latex}
        paired_ids = sorted(set(cur_by_id) & set(lat_by_id))
        paired = [
            {
                "para_id": pid,
                "page": cur_by_id[pid]["page"],
                "current_n_lines": cur_by_id[pid]["n_lines"],
                "latex_n_lines": lat_by_id[pid]["n_lines"],
                "current_min_body_fill": cur_by_id[pid]["min_body_fill"],
                "latex_min_body_fill": lat_by_id[pid]["min_body_fill"],
                "current_short_nonfinal": cur_by_id[pid]["short_nonfinal_lines"],
                "latex_short_nonfinal": lat_by_id[pid]["short_nonfinal_lines"],
                "current_fills": cur_by_id[pid]["fills"],
                "latex_fills": lat_by_id[pid]["fills"],
                "text_head": cur_by_id[pid]["text_head"],
            }
            for pid in paired_ids
        ]
        cur_bf = [r["min_body_fill"] for r in current if r["min_body_fill"] is not None]
        lat_bf = [r["min_body_fill"] for r in latex if r["min_body_fill"] is not None]
        summary[name] = {
            "pages": pages,
            "current_pipeline": {
                "n_paragraphs": len(current),
                "n_with_short_nonfinal_line": sum(
                    1 for r in current if r["short_nonfinal_lines"] > 0
                ),
                "min_body_fill": min(cur_bf) if cur_bf else None,
                "median_body_fill": sorted(cur_bf)[len(cur_bf) // 2] if cur_bf else None,
                "paragraphs": current,
            },
            "latex_hypothesis": {
                "n_paragraphs": len(latex),
                "n_with_short_nonfinal_line": sum(
                    1 for r in latex if (r["short_nonfinal_lines"] or 0) > 0
                ),
                "min_body_fill": min(lat_bf) if lat_bf else None,
                "median_body_fill": sorted(lat_bf)[len(lat_bf) // 2] if lat_bf else None,
                "n_fits": sum(1 for r in latex if r["fits"]),
                "total_seconds": round(sum(r["seconds"] for r in latex), 3),
                "paragraphs": latex,
            },
            "paired": {
                "n_paired_paragraphs": len(paired),
                "n_current_has_short_line": sum(
                    1 for p in paired if (p["current_short_nonfinal"] or 0) > 0
                ),
                "n_latex_has_short_line": sum(
                    1 for p in paired if (p["latex_short_nonfinal"] or 0) > 0
                ),
                "paragraphs": paired,
            },
        }
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "line-quality-comparison.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    for name, s in summary.items():
        print(f"== {name}")
        for side in ("current_pipeline", "latex_hypothesis"):
            d = s[side]
            print(
                f"   {side:18s} paragraphs={d['n_paragraphs']:3d} "
                f"short_nonfinal={d['n_with_short_nonfinal_line']:3d} "
                f"min_body_fill={d['min_body_fill']} median={d['median_body_fill']}"
            )
        p = s["paired"]
        print(
            f"   paired            n={p['n_paired_paragraphs']:3d} "
            f"current_short={p['n_current_has_short_line']} "
            f"latex_short={p['n_latex_has_short_line']}"
        )


if __name__ == "__main__":
    main()
