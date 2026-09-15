"""Compile a text block into a LaTeX page whose paper size equals a BabelDOC bbox.

Experiment helper for the "LaTeX per bbox" hypothesis. It does not touch the
product pipeline: it only builds a ``.tex`` source, calls ``xelatex``, and
reports whether the produced ink actually fits the requested box.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import time
from pathlib import Path

import fitz

#: 解析后的 xelatex 可执行路径（避免在 argv 里用部分路径，S607）。
_XELATEX = shutil.which("xelatex") or "xelatex"

# BabelDOC ships these fonts; reference them by path so fontspec does not need
# a system fontconfig entry (a bare family name failed on this machine).
FONT_DIR = Path.home() / ".cache" / "babeldoc" / "fonts"
CJK_KIND = {
    "sans": "SourceHanSansCN-Regular.ttf",
    "serif": "SourceHanSerifCN-Regular.ttf",
}

TEX_HEADER = r"""\documentclass{article}
\usepackage[paperwidth=%(w).4fpt,paperheight=%(h).4fpt,margin=0pt]{geometry}
\usepackage{fontspec}
\usepackage{xeCJK}
\usepackage{amsmath}
\usepackage{amssymb}
\setCJKmainfont[Path=%(fontdir)s/,Extension=.ttf,UprightFont=%(cjkbase)s]{%(cjkbase)s}
%(cjkbreak)s%(punctstyle)s\pagestyle{empty}
\setlength{\parindent}{0pt}
\setlength{\parskip}{0pt}
\begin{document}
\fontsize{%(fs).4f}{%(lead).4f}\selectfont
%(body)s
\end{document}
"""

# Enabling CJK line breaking lets xeCJK wrap between Han characters, which is
# what lets Chinese prose fill a narrow box without overflowing.
CJK_LINEBREAK = '\\XeTeXlinebreaklocale "zh"\n\\XeTeXlinebreakskip = 0pt plus 1pt\n'

# xeCJK's default punctuation style lets a trailing 。/，hang into the margin,
# which pushes ink past a tight paper edge even though TeX reports no overfull
# box. ``plain`` keeps punctuation inside the measure. Measured on a real
# paragraph: default ink x1 = 258.50pt vs plain = 251.61pt for a 251.61pt page.
PUNCT_PLAIN = "\\xeCJKsetup{PunctStyle=plain}\n"

# Translated text arrives with BabelDOC style spans; strip them for a first pass.
STYLE_TAG = re.compile(r"</?style[^>]*>")
# {vN} are BabelDOC formula placeholders; they are not LaTeX and must not be
# passed through as a group.
FORMULA_PLACEHOLDER = re.compile(r"\{v(\d+)\}")

# LaTeX specials that appear in real translated prose. `\`, `~`, `^` are also
# special but do not show up in the sampled corpus; keep them for safety.
LATEX_SPECIALS = {
    "\\": r"\textbackslash{}",
    "&": r"\&",
    "%": r"\%",
    "$": r"\$",
    "#": r"\#",
    "_": r"\_",
    "{": r"\{",
    "}": r"\}",
    "~": r"\textasciitilde{}",
    "^": r"\textasciicircum{}",
}


def strip_style_markup(text: str) -> str:
    """Drop BabelDOC ``<style id='N'>`` wrappers from translated text."""
    return STYLE_TAG.sub("", text)


def escape_latex(text: str) -> str:
    """Escape prose so it compiles as literal text (no math interpretation)."""
    out = []
    for ch in text:
        out.append(LATEX_SPECIALS.get(ch, ch))
    return "".join(out)


def count_latex_specials(text: str) -> dict:
    """Count characters that would change or break LaTeX parsing."""
    counts: dict[str, int] = {}
    for ch in text:
        if ch in LATEX_SPECIALS:
            counts[ch] = counts.get(ch, 0) + 1
    return counts


def build_tex(
    body: str,
    w: float,
    h: float,
    font_size: float,
    lead: float,
    kind="sans",
    cjk_linebreak: bool = True,
    punct_plain: bool = True,
) -> str:
    cjk = CJK_KIND[kind]
    return TEX_HEADER % {
        "w": w,
        "h": h,
        "fontdir": FONT_DIR,
        "cjk": cjk,
        "cjkbase": cjk.rsplit(".", 1)[0],
        "cjkbreak": CJK_LINEBREAK if cjk_linebreak else "",
        "punctstyle": PUNCT_PLAIN if punct_plain else "",
        "fs": font_size,
        "lead": lead,
        "body": body,
    }


def line_fill_metrics(page: fitz.Page, box_w: float | None = None) -> dict:
    """Measure per-line fill ratios to judge line-breaking quality.

    A well-broken paragraph has lines near 1.0 fill and only the last line
    short. Pathologies: a very short non-final line, or a line with one glyph.
    """
    rows: dict[float, list] = {}
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            spans = [s for s in line["spans"] if s["text"].strip()]
            if not spans:
                continue
            key = round(line["bbox"][1], 0)
            rows.setdefault(key, []).append(
                (min(s["bbox"][0] for s in spans), max(s["bbox"][2] for s in spans))
            )
    fills = []
    for key in sorted(rows):
        x0 = min(r[0] for r in rows[key])
        x1 = max(r[1] for r in rows[key])
        width = x1 - x0
        fills.append(round(width / box_w, 3) if box_w else round(width, 3))
    if not fills:
        return {"n_lines": 0, "fills": [], "min_fill": None, "short_nonfinal_lines": 0}
    body_fills = fills[:-1]
    return {
        "n_lines": len(fills),
        "fills": fills,
        "min_fill": min(fills),
        "min_body_fill": min(body_fills) if body_fills else None,
        "short_nonfinal_lines": sum(1 for f in body_fills if f < 0.6),
    }


def count_render_lines(page: fitz.Page) -> int:
    """Count visual text lines via baseline clustering of span origins."""
    baselines = []
    for block in page.get_text("dict")["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            for span in line["spans"]:
                if span["text"].strip():
                    baselines.append(round(span["origin"][1], 1))
    if not baselines:
        return 0
    baselines.sort()
    n = 1
    for prev, cur in zip(baselines, baselines[1:], strict=False):
        if cur - prev > 2.0:
            n += 1
    return n


def render_png(pdf_path: Path, page_index: int, out_png: Path, dpi: int = 150) -> dict:
    """Render one PDF page to PNG for visual inspection."""
    doc = fitz.open(pdf_path)
    page = doc[page_index]
    pix = page.get_pixmap(dpi=dpi)
    out_png.parent.mkdir(parents=True, exist_ok=True)
    pix.save(out_png)
    rec = {"png": str(out_png), "width": pix.width, "height": pix.height, "dpi": dpi}
    doc.close()
    return rec


def compile_tex(
    tex: str,
    workdir: Path,
    stem: str,
    timeout: float = 45.0,
    body: str = "",
    box_w: float = 0.0,
    box_h: float = 0.0,
    font_size: float = 0.0,
) -> dict:
    """Write ``tex``, run xelatex, and return a metrics record.

    ``overset_from_tex`` picks up LaTeX's own overfull signal; ``fits`` is a
    measured judgement (ink bbox inside the paper, no ink loss).
    """
    workdir.mkdir(parents=True, exist_ok=True)
    tex_path = workdir / f"{stem}.tex"
    tex_path.write_text(tex, encoding="utf-8")
    log_path = workdir / f"{stem}.log"

    started = time.perf_counter()
    proc = subprocess.run(
        [
            _XELATEX,
            "-interaction=nonstopmode",
            "-halt-on-error",
            f"-output-directory={workdir}",
            str(tex_path),
        ],
        cwd=workdir,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    elapsed = time.perf_counter() - started
    log = proc.stdout + proc.stderr
    log_path.write_text(log, encoding="utf-8")

    overfull = len(re.findall(r"Overfull \\hbox", log))
    underfull = len(re.findall(r"Underfull \\hbox", log))
    errors = re.findall(r"^! .*$", log, flags=re.M)

    record = {
        "stem": stem,
        "tex": str(tex_path),
        "log": str(log_path),
        "returncode": proc.returncode,
        "seconds": round(elapsed, 3),
        "overfull_hbox": overfull,
        "underfull_hbox": underfull,
        "errors": errors[:5],
        "requested_w": round(box_w, 3),
        "requested_h": round(box_h, 3),
        "font_size": round(font_size, 4),
        "n_body_chars": len(body),
    }

    pdf_path = workdir / f"{stem}.pdf"
    if proc.returncode != 0 or not pdf_path.exists():
        record.update({"compiled": False, "fits": False, "fits_reason": "compile-failed"})
        return record

    doc = fitz.open(pdf_path)
    page = doc[0]
    record["page_rect"] = [round(v, 3) for v in page.rect]
    ink = fitz.Rect()
    n_text_chars = len(page.get_text())
    for block in page.get_text("blocks"):
        ink |= fitz.Rect(block[:4])
    for drawing in page.get_drawings():
        ink |= fitz.Rect(drawing["rect"])
    record["ink_bbox"] = [round(v, 3) for v in ink] if not ink.is_empty else None
    record["text_chars_extractable"] = n_text_chars
    record["render_lines"] = count_render_lines(page)
    record["line_fill"] = line_fill_metrics(page, box_w)
    doc.close()

    fit = True
    reason = "ok"
    if n_text_chars == 0:
        fit = False
        reason = "no-extractable-text"
    if not ink.is_empty and box_w > 0 and box_h > 0:
        # 0.5pt tolerance for rounding in geometry/xelatex paper sizing.
        if ink.x1 > box_w + 0.5 or ink.x0 < -0.5:
            fit = False
            reason = "horizontal-overflow"
        if ink.y1 > box_h + 0.5 or ink.y0 < -0.5:
            fit = False
            reason = "vertical-overflow"
    if overfull:
        fit = False
        reason = "overfull-hbox" if reason == "ok" else reason + "+overfull"

    record.update({"compiled": True, "fits": fit, "fits_reason": reason, "pdf": str(pdf_path)})
    return record


def fit_by_shrink(
    body: str,
    w: float,
    h: float,
    start_font_size: float,
    lead_ratio: float,
    workdir: Path,
    stem: str,
    min_font_size: float = 4.0,
    max_steps: int = 12,
    tolerance: float = 0.03,
) -> list[dict]:
    """Try ``start_font_size``, then shrink until the ink fits the box.

    Mirrors the intent of ``Typesetting._find_optimal_scale_and_layout`` but is
    allowed to stop as soon as the block fits; returns every attempt.
    """
    attempts: list[dict] = []
    font_size = start_font_size
    for step in range(max_steps):
        lead = font_size * lead_ratio
        tex = build_tex(body, w, h, font_size, lead)
        rec = compile_tex(
            tex, workdir, f"{stem}_s{step}", body=body, box_w=w, box_h=h, font_size=font_size
        )
        rec["step"] = step
        rec["lead"] = round(lead, 4)
        attempts.append(rec)
        if rec["fits"]:
            break
        next_size = max(font_size * (1.0 - tolerance), min_font_size)
        if next_size >= font_size:
            break  # already at the floor
        font_size = next_size
    return attempts


def overlay_box(
    src_pdf: Path,
    out_pdf: Path,
    page_index: int,
    box_yup: list[float],
    stamp_pdf: Path,
    page_h: float,
    erase_color: tuple[float, float, float] = (1, 1, 1),
    mode: str = "draw",
) -> dict:
    """Stamp ``stamp_pdf`` page 0 into ``box_yup`` on ``src_pdf`` page ``page_index``.

    ``box_yup`` uses BabelDOC IL coordinates (origin bottom-left); MuPDF uses a
    top-left origin, hence the ``page_h - y`` flip.

    ``mode`` selects how the original box content is handled:
      ``draw``     - paint a white rectangle over the box (visual erase only; the
                     original text stays in the text layer, so extraction yields
                     both the old and the new text);
      ``redact``   - apply a MuPDF redaction annotation, which removes the
                     original glyphs *and* their text-layer entries;
      ``none``     - overlay only, no erasing.
    """
    started = time.perf_counter()
    x0, y0, x1, y1 = box_yup
    rect = fitz.Rect(x0, page_h - y1, x1, page_h - y0)

    doc = fitz.open(src_pdf)
    stamp = fitz.open(stamp_pdf)
    page = doc[page_index]
    if mode == "redact":
        page.add_redact_annot(rect)
        page.apply_redactions()
    elif mode == "draw":
        page.draw_rect(rect, color=erase_color, fill=erase_color, overlay=True)
    elif mode != "none":
        raise ValueError(f"unknown overlay mode: {mode}")
    page.show_pdf_page(rect, stamp, 0, keep_proportion=False, overlay=True)
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    doc.save(out_pdf, garbage=4, deflate=True)
    elapsed = time.perf_counter() - started

    check = fitz.open(out_pdf)
    chk_page = check[page_index]
    extracted = chk_page.get_text()
    clipped = chk_page.get_text("text", clip=rect)
    record = {
        "out_pdf": str(out_pdf),
        "page_index": page_index,
        "mode": mode,
        "rect_mupdf": [round(v, 3) for v in rect],
        "seconds": round(elapsed, 3),
        "page_count": check.page_count,
        "extractable_chars": len(extracted),
        "chars_in_box_clip": len(clipped.strip()),
    }
    check.close()
    doc.close()
    stamp.close()
    return record


def write_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")
