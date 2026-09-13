#!/usr/bin/env python3
"""Run PP-DocLayoutV3 over a PDF: page images + layout JSON + a box-annotated PDF.

    python scripts/run_layout.py --pdf ../2512.08296v3.pdf
    python scripts/run_layout.py --pdf ../2512.08296v3.pdf --device cpu --pages 1-5

Output layout (default ``<repo>/tmp/paddle-layout/<pdf-stem>/``):

    layout.json          page list, boxes, palette, run metadata
    pages/page-0001.webp rendered page images (overlay background)
    boxes_overlay.pdf    a copy of the input PDF with the boxes drawn in
    run.log              the console log of the run

Performance: page rendering (PyMuPDF + WebP encode) runs in a thread pool and is prefetched
while the GPU session chews through the previous page, so the two halves overlap. Inference
itself is serial: batching measured no better than batch-1 on CoreML.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from layout_engine import (  # noqa: E402
    LABELS, LABELS_ZH, PALETTE, Box, LayoutEngine, class_counts, color_for,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
PROJECT_ROOT = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- helpers
def parse_pages(spec: str | None, total: int) -> list[int]:
    """'1-3,7,10-' -> zero-based page indices."""
    if not spec:
        return list(range(total))
    out: list[int] = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, _, b = part.partition("-")
            start = int(a) if a else 1
            end = int(b) if b else total
        else:
            start = end = int(part)
        out.extend(range(max(1, start), min(total, end) + 1))
    seen, uniq = set(), []
    for p in out:
        if p not in seen:
            seen.add(p)
            uniq.append(p)
    return [p - 1 for p in uniq]


def render_page(page, zoom: float) -> np.ndarray:
    """PyMuPDF page -> HxWx3 uint8 RGB."""
    pix = page.get_pixmap(matrix=None if zoom == 1 else __import__("fitz").Matrix(zoom, zoom),
                          alpha=False)
    return np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)[:, :, :3]


def encode_webp(rgb: np.ndarray, quality: int) -> bytes:
    from PIL import Image
    buf = io.BytesIO()
    Image.fromarray(rgb).save(buf, format="WEBP", quality=quality, method=4)
    return buf.getvalue()


def pixel_to_pdf_matrix(page, zoom: float):
    """Inverse of the transform used by ``get_pixmap``: rendered pixels -> drawing coords.

    Note: PyMuPDF's ``Matrix.invert()`` mutates in place and returns the *determinant*,
    not the inverted matrix - it does not build a new object.
    """
    import fitz
    m = fitz.Matrix(zoom, zoom) * page.rotation_matrix
    try:
        m.invert()
    except ZeroDivisionError:
        return fitz.Matrix(1, 1)
    return m


def annotate_pdf(src: Path, dst: Path, pages: list[dict], zoom: float,
                 fill_opacity: float, stroke_width: float, offset: float) -> None:
    """Draw every box onto a copy of the PDF.

    The stroke is offset *outwards* from the text bbox so the outline sits in the whitespace
    around the glyphs instead of crossing them, matching the viewer's look. The fill stays at
    a low opacity for the same reason.
    """
    import fitz

    doc = fitz.open(src)
    drawn = 0
    for entry in pages:
        page = doc[entry["index"]]
        inv = pixel_to_pdf_matrix(page, zoom)
        for b in entry["boxes"]:
            rect = fitz.Rect(b["x0"], b["y0"], b["x1"], b["y1"]) * inv
            rect.normalize()
            if rect.is_empty or rect.is_infinite:
                continue
            rect = fitz.Rect(rect.x0 - offset, rect.y0 - offset,
                             rect.x1 + offset, rect.y1 + offset) & page.rect
            if rect.is_empty:
                continue
            hexcolor = color_for(b["label"]).lstrip("#")
            color = tuple(int(hexcolor[i:i + 2], 16) / 255 for i in (0, 2, 4))
            page.draw_rect(rect, color=color, fill=color, fill_opacity=fill_opacity,
                           width=stroke_width, overlay=True)
            drawn += 1
    dst.parent.mkdir(parents=True, exist_ok=True)
    doc.save(dst, garbage=3, deflate=True)
    doc.close()
    return drawn


class Tee:
    """Mirror stdout into run.log."""

    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(path, "w", buffering=1)

    def write(self, s: str) -> None:
        sys.__stdout__.write(s)
        self.fh.write(s)

    def flush(self) -> None:
        sys.__stdout__.flush()
        self.fh.flush()


# --------------------------------------------------------------------------- main
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--pdf", required=True, help="input PDF")
    p.add_argument("--out", default=None,
                   help="output dir (default: <repo>/tmp/paddle-layout/<pdf-stem>)")
    p.add_argument("--pages", default=None, help="e.g. '1-5,9' (1-based, default: all)")
    p.add_argument("--zoom", type=float, default=2.0,
                   help="render scale; detections are mapped back exactly (default 2.0)")
    p.add_argument("--threshold", type=float, default=0.5, help="score threshold (default 0.5)")
    p.add_argument("--device", choices=["auto", "gpu", "cpu"], default="auto")
    p.add_argument("--coreml-units", default="CPUAndGPU",
                   choices=["CPUAndGPU", "ALL", "CPUAndNeuralEngine"],
                   help="CoreML MLComputeUnits (default CPUAndGPU: fastest measured)")
    p.add_argument("--workers", type=int, default=0, help="render threads (default: os.cpu_count())")
    p.add_argument("--webp-quality", type=int, default=88)
    p.add_argument("--no-verify", action="store_true", help="skip the GPU-vs-CPU self-check")
    p.add_argument("--no-annotate", action="store_true", help="skip boxes_overlay.pdf")
    p.add_argument("--annotate-opacity", type=float, default=0.10)
    p.add_argument("--annotate-stroke", type=float, default=0.8)
    p.add_argument("--annotate-offset", type=float, default=0.7,
                   help="push the annotated outline this many points outside the bbox")
    p.add_argument("--pdf-copy", action="store_true",
                   help="also copy the input PDF next to the output (for the viewer)")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    import fitz

    pdf_path = Path(args.pdf).resolve()
    if not pdf_path.exists():
        print(f"error: no such PDF: {pdf_path}", file=sys.stderr)
        return 2
    out_dir = Path(args.out) if args.out else REPO_ROOT / "tmp" / "paddle-layout" / pdf_path.stem
    out_dir.mkdir(parents=True, exist_ok=True)
    log = Tee(out_dir / "run.log")
    sys.stdout = log  # type: ignore[assignment]

    doc = fitz.open(pdf_path)
    page_ids = parse_pages(args.pages, doc.page_count)
    print(f"PDF      : {pdf_path}")
    print(f"pages    : {len(page_ids)} of {doc.page_count}")
    print(f"output   : {out_dir}")

    engine = LayoutEngine(device=args.device, units=args.coreml_units, verify=not args.no_verify)

    pages_dir = out_dir / "pages"
    pages_dir.mkdir(exist_ok=True)
    workers = args.workers or min(8, (__import__("os").cpu_count() or 4))

    # ---------------------------------------------------------------- render/infer pipeline
    def render_and_encode(idx: int):
        page = doc[idx]
        rgb = render_page(page, args.zoom)
        return idx, rgb, encode_webp(rgb, args.webp_quality)

    entries: list[dict] = []
    per_page_ms: list[float] = []
    t_start = time.perf_counter()

    with ThreadPoolExecutor(max_workers=workers) as pool:
        queue: deque = deque()
        feed = iter(enumerate(page_ids))
        for _ in range(min(workers, len(page_ids))):        # prefetch while we infer
            nxt = next(feed, None)
            if nxt is None:
                break
            queue.append((nxt[1], pool.submit(render_and_encode, nxt[1])))

        done = 0
        while queue:
            page_no, fut = queue.popleft()
            idx, rgb, webp = fut.result()
            t0 = time.perf_counter()
            boxes: list[Box] = engine.infer_rgb(rgb, threshold=args.threshold)
            ms = (time.perf_counter() - t0) * 1000
            per_page_ms.append(ms)

            name = f"page-{page_no + 1:04d}.webp"
            (pages_dir / name).write_bytes(webp)
            h, w = rgb.shape[:2]
            entries.append({
                "index": page_no,
                "page": page_no + 1,
                "width": w,
                "height": h,
                "image": f"pages/{name}",
                "pdf_width": round(doc[page_no].rect.width, 2),
                "pdf_height": round(doc[page_no].rect.height, 2),
                "infer_ms": round(ms, 1),
                "boxes": [b.to_dict() for b in boxes],
            })
            done += 1
            print(f"  [{done:>3}/{len(page_ids)}] page {page_no + 1:>3}  "
                  f"{len(boxes):>3} boxes  {ms:6.1f} ms")

            nxt = next(feed, None)
            if nxt is not None:
                queue.append((nxt[1], pool.submit(render_and_encode, nxt[1])))

    entries.sort(key=lambda e: e["index"])
    total_s = time.perf_counter() - t_start

    # ---------------------------------------------------------------- annotated PDF
    overlay_rel = None
    if not args.no_annotate:
        overlay = out_dir / "boxes_overlay.pdf"
        drawn = annotate_pdf(pdf_path, overlay, entries, args.zoom, args.annotate_opacity,
                             args.annotate_stroke, args.annotate_offset)
        overlay_rel = overlay.name
        print(f"annotated: {overlay}  ({drawn} rectangles)")

    if args.pdf_copy:
        import shutil
        shutil.copy2(pdf_path, out_dir / pdf_path.name)

    counts = class_counts([]) if not entries else class_counts(
        [[Box(b["label"], b["score"], b["x0"], b["y0"], b["x1"], b["y1"], b["order"],
              b["query_rank"]) for b in e["boxes"]] for e in entries]
    )

    median_ms = float(np.median(per_page_ms)) if per_page_ms else 0.0
    payload = {
        "meta": {
            "pdf": pdf_path.name,
            "pdf_path": str(pdf_path),
            "generated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "model": "PP-DocLayoutV3 (official ONNX export, bbox head only)",
            "device": engine.device,
            "zoom": args.zoom,
            "threshold": args.threshold,
            "pages_processed": len(entries),
            "ms_per_page_median": round(median_ms, 1),
            "total_seconds": round(total_s, 2),
            "annotated_pdf": overlay_rel,
        },
        "labels": list(LABELS),
        "labels_zh": dict(LABELS_ZH),
        "palette": {**{l: PALETTE.get(l, "#111827") for l in LABELS}},
        "class_counts": counts,
        "pages": entries,
    }
    (out_dir / "layout.json").write_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    print(f"\ndone     : {len(entries)} pages in {total_s:.1f}s  "
          f"({median_ms:.1f} ms/page median, {engine.device})")
    print(f"boxes    : {sum(counts.values())} across {len(counts)} classes")
    for label, n in list(counts.items())[:12]:
        print(f"             {label:<18} {n}")
    print(f"view it  : python scripts/serve.py --data {out_dir}")
    log.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
