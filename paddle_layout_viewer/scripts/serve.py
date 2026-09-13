#!/usr/bin/env python3
"""Serve the bbox viewer:  /  -> viewer/     /data/ -> the run output dir.

    python scripts/serve.py --data ../tmp/paddle-layout/2512.08296v3
    python scripts/serve.py --data DIR --port 8765 --no-open
"""

from __future__ import annotations

import argparse
import functools
import mimetypes
import sys
import threading
import webbrowser
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
VIEWER_DIR = PROJECT_ROOT / "viewer"

mimetypes.add_type("image/webp", ".webp")
mimetypes.add_type("application/json", ".json")


class Handler(SimpleHTTPRequestHandler):
    """Routes /data/* to the run directory, everything else to viewer/."""

    data_dir: Path
    viewer_dir: Path

    def translate_path(self, path: str) -> str:  # noqa: D102
        clean = path.split("?", 1)[0].split("#", 1)[0]
        if clean.startswith("/data/"):
            root, rel = self.data_dir, clean[len("/data/"):]
        else:
            root, rel = self.viewer_dir, clean.lstrip("/")
        target = (root / rel).resolve()
        try:                                     # block path traversal
            target.relative_to(root.resolve())
        except ValueError:
            return str(root / "__forbidden__")
        if target.is_dir():
            target = target / "index.html"
        return str(target)

    def end_headers(self) -> None:  # noqa: D102
        # layout.json is regenerated per run; never let the browser pin a stale copy
        self.send_header("Cache-Control", "no-cache, must-revalidate")
        self.send_header("Access-Control-Allow-Origin", "*")
        super().end_headers()

    def log_message(self, fmt: str, *args) -> None:  # noqa: D102
        if "404" in (fmt % args):
            sys.stderr.write("  404  %s\n" % (fmt % args))


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--data", required=True, help="run output dir (contains layout.json)")
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-open", action="store_true")
    args = ap.parse_args(argv)

    data_dir = Path(args.data).resolve()
    if not (data_dir / "layout.json").exists():
        print(f"error: {data_dir}/layout.json not found - run scripts/run_layout.py first",
              file=sys.stderr)
        return 2

    Handler.data_dir = data_dir
    Handler.viewer_dir = VIEWER_DIR
    httpd = ThreadingHTTPServer((args.host, args.port),
                                functools.partial(Handler, directory=str(VIEWER_DIR)))
    url = f"http://{args.host}:{args.port}/"
    print(f"viewer : {url}")
    print(f"data   : {data_dir}")
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
