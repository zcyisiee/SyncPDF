"""CLI for the stable BabelDOC tools."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from . import dispatch
from . import get_schema
from . import list_tools


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="babeldoc-tools")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--compact", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list").add_argument("--compact", action="store_true")
    schema = sub.add_parser("schema")
    schema.add_argument("--compact", action="store_true")
    schema.add_argument("tool")
    call = sub.add_parser("call")
    call.add_argument("--compact", action="store_true")
    call.add_argument("tool")
    call.add_argument("--args-json")
    call.add_argument("--args-file")
    call.add_argument("--workdir")
    args = parser.parse_args(argv)
    if args.command == "list":
        print(json.dumps({"ok": True, "data": {"tools": list_tools()}}, ensure_ascii=False))
        return 0
    if args.command == "schema":
        try:
            payload = {"ok": True, "tool": args.tool, "data": get_schema(args.tool)}
        except KeyError:
            payload = {"ok": False, "tool": args.tool, "error": {"code": "unknown_tool"}}
        print(json.dumps(payload, ensure_ascii=False))
        return 0 if payload["ok"] else 1
    values = {}
    if args.args_json:
        values = json.loads(args.args_json)
    elif args.args_file:
        values = json.loads(Path(args.args_file).read_text(encoding="utf-8"))
    if args.workdir:
        values.setdefault("workdir", args.workdir)
    result = dispatch(args.tool, values)
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
