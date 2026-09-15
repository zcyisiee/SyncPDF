#!/usr/bin/env python3
"""Serve the bbox viewer, with support for several documents at once.

    # one document
    python scripts/serve.py --data ../tmp/paddle-layout/2512.08296v3

    # a folder of documents -> the viewer shows a picker
    python scripts/serve.py --data ../tmp/paddle-layout

Routes
    /               -> viewer/
    /data/index.json -> generated on the fly by scanning for */layout.json
    /data/<id>/...   -> that document's assets (single-doc mode: /data/... directly)

``--data`` may point either at a run output dir (one containing ``layout.json``)
or at a parent folder whose children are run output dirs. The index is rebuilt on
every request, so re-running ``run_layout.py`` shows up without a restart.
"""

from __future__ import annotations

import argparse
import functools
import json
import mimetypes
import sys
import threading
import webbrowser
from http.server import SimpleHTTPRequestHandler
from http.server import ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VIEWER_DIR = PROJECT_ROOT / "viewer"

mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("application/json", ".json")

MAX_DOCS = 40


def describe_doc(directory: Path, doc_id: str, base: str) -> dict | None:
    """Read just enough of layout.json to describe a document in the picker."""
    layout = directory / "layout.json"
    try:
        data = json.loads(layout.read_text())
    except (OSError, ValueError) as exc:
        print(f"  ! skipping {directory}: cannot read layout.json ({exc})", file=sys.stderr)
        return None
    meta = data.get("meta", {})
    pages = data.get("pages", [])
    return {
        "id": doc_id,
        "name": meta.get("pdf") or directory.name,
        "folder": directory.name,
        "base": base,
        "pages": len(pages),
        "boxes": sum(len(p.get("boxes", ())) for p in pages),
        "ms_per_page": meta.get("ms_per_page_median"),
        "device": meta.get("device"),
        "threshold": meta.get("threshold"),
        "zoom": meta.get("zoom"),
        "generated_at": meta.get("generated_at"),
        "annotated_pdf": meta.get("annotated_pdf"),
    }


def scan_docs(root: Path) -> list[dict]:
    """Single-doc root -> one entry (id ''); otherwise every child with a layout.json."""
    root = root.resolve()
    if (root / "layout.json").exists():
        doc = describe_doc(root, "", "data/")
        return [doc] if doc else []
    docs = []
    children = [c for c in sorted(root.iterdir()) if c.is_dir()]
    for child in children[:MAX_DOCS]:
        if not (child / "layout.json").exists():
            continue
        doc = describe_doc(child, child.name, f"data/{child.name}/")
        if doc:
            docs.append(doc)
    return docs


class Handler(SimpleHTTPRequestHandler):
    """Routes /data/* to the run directories, everything else to viewer/."""

    data_root: Path
    viewer_dir: Path

    # ---------------------------------------------------------------- helpers
    def _docs(self) -> list[dict]:
        return scan_docs(self.data_root)

    def _send_json(self, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _resolve_data(self, rel: str) -> Path | None:
        """Map a path below /data/ onto disk, rejecting traversal."""
        rel = rel.lstrip("/")
        root = self.data_root.resolve()
        if (root / "layout.json").exists():          # single-document mode
            candidate = (root / rel).resolve()
            try:
                candidate.relative_to(root)
            except ValueError:
                return None
            return candidate
        head, _, tail = rel.partition("/")           # multi-document mode
        if head not in {d["id"] for d in self._docs()}:
            return None
        base = (root / head).resolve()
        candidate = (base / tail).resolve()
        try:
            candidate.relative_to(base)
        except ValueError:
            return None
        return candidate

    # ---------------------------------------------------------------- routing
    def do_GET(self) -> None:  # noqa: N802
        clean = self.path.split("?", 1)[0].split("#", 1)[0]
        if clean in ("/data/index.json", "/data/index.json/"):
            docs = self._docs()
            self._send_json({
                "docs": docs,
                "multi": not (self.data_root / "layout.json").exists(),
                "root": str(self.data_root),
            })
            return
        super().do_GET()

    def translate_path(self, path: str) -> str:  # noqa: D102
        clean = path.split("?", 1)[0].split("#", 1)[0]
        if clean.startswith("/data/"):
            target = self._resolve_data(clean[len("/data/"):])
            if target is None:
                return str(self.data_root / "__not_found__")
            if target.is_dir():
                target = target / "index.html"
            return str(target)
        target = (self.viewer_dir / clean.lstrip("/")).resolve()
        try:
            target.relative_to(self.viewer_dir.resolve())
        except ValueError:
            return str(self.viewer_dir / "__forbidden__")
        if target.is_dir():
            target = target / "index.html"
        return str(target)

    def end_headers(self) -> None:  # noqa: D102
        # layout.json is regenerated per run; never let the browser pin a stale copy
        self.send_header("Cache-Control", "no-cache, must-revalidate")
        super().end_headers()

    def log_message(self, fmt: str, *args) -> None:  # noqa: D102
        if "404" in (fmt % args):
            sys.stderr.write("  404  %s\n" % (fmt % args))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", required=True,
                    help="a run output dir, or a folder containing several of them")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args(argv)

    data_root = Path(args.data).resolve()
    if not data_root.is_dir():
        print(f"error: not a directory: {data_root}", file=sys.stderr)
        return 2
    docs = scan_docs(data_root)
    if not docs:
        print(f"error: no layout.json found in {data_root} or its subfolders\n"
              f"       run: python scripts/run_layout.py --pdf <file.pdf>", file=sys.stderr)
        return 2

    Handler.data_root = data_root
    Handler.viewer_dir = VIEWER_DIR
    httpd = ThreadingHTTPServer((args.host, args.port),
                                functools.partial(Handler, directory=str(VIEWER_DIR)))
    url = f"http://{args.host}:{args.port}/"
    print(f"viewer : {url}")
    print(f"data   : {data_root}")
    print(f"docs   : {len(docs)}")
    for d in docs:
        print(f"         {d['id'] or '(single)':<28} {d['pages']:>3} pages  {d['boxes']:>4} boxes")
    print("ctrl-c to stop")
    if not args.no_open:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
