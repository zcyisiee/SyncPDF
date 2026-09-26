"""``bdt cloud`` 的命令行接入：``serve`` 启动服务，``invite`` 生成邀请码。

与 ``bdt serve`` 同样的约定：模块 import 不引入 fastapi/uvicorn；stdout 只写单行 JSON
（``serve`` 在绑定端口成功后打印一次），日志全部走 stderr。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from pathlib import Path

from babeldoc_tools import __version__
from babeldoc_tools.cloud.db import DEFAULT_ROOT
from babeldoc_tools.common import ToolError
from babeldoc_tools.serve.cli import _LOG_CONFIG
from babeldoc_tools.serve.cli import WEB_EXTRA_HINT
from babeldoc_tools.serve.cli import _bind_socket

__all__ = ["add_parser", "run"]


def add_parser(subparsers) -> argparse.ArgumentParser:
    parser = subparsers.add_parser(
        "cloud",
        help="云端多用户翻译服务（邀请码登录、排队、共享译文缓存；需 web extra）",
        description="云端版：serve 启动 HTTP 服务（前端静态文件由 nginx 托管），invite 生成邀请码。",
    )
    actions = parser.add_subparsers(dest="cloud_command", required=True)
    serve = actions.add_parser("serve", help="启动云端 HTTP 服务（只有 /api）")
    serve.add_argument("--root", default=str(DEFAULT_ROOT), help=f"数据根目录（默认 {DEFAULT_ROOT}）")
    serve.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1，由 nginx 反代）")
    serve.add_argument("--port", type=int, default=8790, help="监听端口（默认 8790）")
    serve.add_argument("--engine", help="syncpdf-cli 路径（默认仓库内 release 构建）")
    serve.add_argument("--translator", default="pi", help="翻译通道（默认 pi=DeepSeek；测试用 fake:*）")
    serve.add_argument("--layout-device", default="cpu", help="版面模型设备（默认 cpu）")
    invite = actions.add_parser("invite", help="新建账户并打印其邀请码")
    invite.add_argument("--root", default=str(DEFAULT_ROOT), help=f"数据根目录（默认 {DEFAULT_ROOT}）")
    invite.add_argument("--name", required=True, help="账户显示名")
    invite.add_argument("--quota", type=int, default=5, help="每日篇数额度（默认 5）")
    return parser


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _fail(code: str, message: str) -> int:
    _emit({"ok": False, "error": {"code": code, "message": message}})
    sys.stderr.write(f"bdt cloud: {message}\n")
    return 1


def _invite(args: argparse.Namespace) -> int:
    from babeldoc_tools.cloud.auth import create_invite
    from babeldoc_tools.cloud.db import Database
    from babeldoc_tools.cloud.db import Paths

    if args.quota < 1:
        return _fail("invalid_quota", "--quota 必须是正整数")
    paths = Paths(Path(args.root).expanduser()).ensure()
    db = Database(paths.db)
    try:
        _emit({"ok": True, "data": create_invite(db, args.name, args.quota)})
    finally:
        db.close()
    return 0


def _serve(args: argparse.Namespace) -> int:
    if not 0 <= args.port <= 65535:
        return _fail("invalid_port", f"--port 必须在 0..65535 之间：{args.port}")
    try:
        try:
            import uvicorn

            from babeldoc_tools.cloud.app import create_app
        except ImportError as exc:
            raise ToolError("web_extra_missing", WEB_EXTRA_HINT) from exc
        root = Path(args.root).expanduser()
        app = create_app(root, engine=args.engine, translator=args.translator, layout_device=args.layout_device)
    except ToolError as exc:
        return _fail(exc.code, exc.message)
    try:
        sock = _bind_socket(args.host, args.port)
    except OSError as exc:
        return _fail("port_unavailable", f"无法监听 {args.host}:{args.port}：{exc}")
    port = sock.getsockname()[1]
    _emit({
        "ok": True,
        "data": {
            "api": f"http://{args.host}:{port}/api",
            "host": args.host,
            "port": port,
            "root": str(root),
            "translator": args.translator,
            "version": __version__,
            "pid": os.getpid(),
        },
    })
    # SSE 长连接不会自己结束：停服最多等它们 3 秒，随后 lifespan 终止引擎、把运行中的翻译留给重启重排
    server = uvicorn.Server(
        uvicorn.Config(app, log_level="info", log_config=_LOG_CONFIG, timeout_graceful_shutdown=3)
    )
    try:
        server.run(sockets=[sock])
    finally:
        with contextlib.suppress(OSError):
            sock.close()
    return 0


def run(args: argparse.Namespace) -> int:
    if args.cloud_command == "invite":
        return _invite(args)
    return _serve(args)
