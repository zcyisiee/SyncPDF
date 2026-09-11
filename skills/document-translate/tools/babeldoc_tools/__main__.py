"""babeldoc_tools CLI（薄壳）。

用法::

    python -m babeldoc_tools list                       # 工具清单
    python -m babeldoc_tools schema layout_set          # 参数 JSON Schema
    python -m babeldoc_tools call layout_set --args-json '{"workdir": "tmp/wd", "patch": {...}}'
    python -m babeldoc_tools call parse_document --args-file args.json
    python -m babeldoc_tools call review_document --workdir tmp/wd   # 单键参数简写

stdout（机读）::

    {"ok": true, "tool": "layout_set", "data": {...}}
    {"ok": false, "tool": "layout_set", "error": {"code": "...", "message": "..."}}

退出码：0 = ok，1 = 工具失败，2 = 用法错误。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from babeldoc_tools import __version__
from babeldoc_tools import registry


def _load_args(args) -> dict:
    payload: dict = {}
    if args.args_json:
        try:
            payload = json.loads(args.args_json)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"--args-json 解析失败: {exc}") from None
    elif args.args_file:
        try:
            payload = json.loads(Path(args.args_file).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise SystemExit(f"--args-file 读取失败: {exc}") from None
    if args.workdir:
        payload.setdefault("workdir", args.workdir)
    for item in args.arg or []:
        if "=" not in item:
            raise SystemExit(f"--arg 需要 k=v 形式：{item}")
        key, _, raw = item.partition("=")
        try:
            payload[key] = json.loads(raw)
        except json.JSONDecodeError:
            payload[key] = raw
    if not isinstance(payload, dict):
        raise SystemExit("args 必须是 JSON 对象")
    return payload


def _compact_parent() -> argparse.ArgumentParser:
    """让 ``--compact`` 在子命令前后都能用。"""
    parent = argparse.ArgumentParser(add_help=False)
    parent.add_argument("--compact", action="store_true", help="紧凑输出")
    return parent


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        prog="babeldoc_tools",
        description="BabelDOC agent 工具层（JSON in / JSON out）",
    )
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--compact", action="store_true", help="紧凑输出（全局）")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("list", help="列出所有工具", parents=[_compact_parent()])
    p_schema = sub.add_parser("schema", help="显示工具参数 JSON Schema", parents=[_compact_parent()])
    p_schema.add_argument("tool")

    p_call = sub.add_parser("call", help="调用工具", parents=[_compact_parent()])
    p_call.add_argument("tool")
    p_call.add_argument("--args-json", default=None)
    p_call.add_argument("--args-file", default=None)
    p_call.add_argument("--workdir", default=None, help="等价于 args.workdir")
    p_call.add_argument(
        "--arg",
        action="append",
        default=[],
        help="键值参数（可重复，值按 JSON 解析，失败则当字符串）：--arg ids='[\"P01-002\"]'",
    )

    args = parser.parse_args(argv)
    registry.load_builtin_tools()

    if args.command == "list":
        print(
            json.dumps(
                {"ok": True, "data": {"tools": registry.list_tools()}},
                ensure_ascii=False,
                indent=None if args.compact else 2,
            )
        )
        return 0
    if args.command == "schema":
        try:
            schema = registry.get_schema(args.tool)
        except KeyError:
            print(
                json.dumps(
                    {
                        "ok": False,
                        "error": {
                            "code": "unknown_tool",
                            "message": f"未知工具 {args.tool}",
                            "available": registry.tool_names(),
                        },
                    },
                    ensure_ascii=False,
                    indent=2,
                )
            )
            return 1
        print(json.dumps({"ok": True, "tool": args.tool, "data": schema}, ensure_ascii=False, indent=2))
        return 0

    payload = _load_args(args)
    result = registry.dispatch(args.tool, payload)
    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=None if args.compact else 2,
        )
    )
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
