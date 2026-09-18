"""``bdt serve`` 的命令行接入：参数、依赖检查、绑定端口、启动 uvicorn。

模块 import 只依赖标准库 + :mod:`babeldoc_tools.common` / ``store``，**不 import
fastapi/uvicorn**：没有 web extra 时 ``bdt serve --help`` 与其它子命令照常可用，
只有真正启动服务才要求依赖（缺失时报可操作的错误）。

stdout 仍是 ``bdt`` 的单行 JSON 约定：**绑定端口成功后再打印一次**启动信封
（含真实端口与可访问 URL），随后 uvicorn 的日志与 access log 全部走 stderr。
因此 ``--open`` 打开的是已确认监听端口的 URL，不会出现假 URL。
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import socket
import sys
import webbrowser

from babeldoc_tools import __version__
from babeldoc_tools.common import ToolError
from babeldoc_tools.serve.schemas import API_PREFIX
from babeldoc_tools.serve.store import DocumentStore

__all__ = ["add_parser", "run"]

#: 缺 web extra 时的可操作提示（只在真正启动服务时检查）。
WEB_EXTRA_HINT = (
    "serve 需要 web extra（fastapi/uvicorn/python-multipart）。"
    '安装：uv pip install --python .venv/bin/python "fastapi>=0.115" '
    '"uvicorn[standard]>=0.32" "python-multipart>=0.0.9"；'
    "或 uv sync --extra web"
)

_LOOPBACK_HOSTS = {"localhost", "::1", "127.0.0.1"}

#: 通配地址需要换成可点击的 loopback（这里只做映射，从不拿它去 bind，故无 S104 风险）。
_WILDCARD_HOSTS = frozenset({"0.0.0.0", "::"})  # noqa: S104


def add_parser(subparsers) -> argparse.ArgumentParser:
    """把 ``serve`` 挂到 ``bdt`` 的子命令上（不 import web 依赖）。"""
    parser = subparsers.add_parser(
        "serve",
        help="启动本地只读 HTTP 服务（FastAPI + OpenAPI；需 web extra）",
        description=(
            "启动本地只读 HTTP 服务：枚举 <root>/<did>/ 下的文档 workdir，"
            "或只公开 --workdir 指定的那一个（兄弟目录不可见）。"
            "默认只监听 loopback；W01 无认证，绑定非 loopback 地址前请自行确认网络环境。"
        ),
    )
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument(
        "--root",
        help="文档根目录：其下每个子目录 = 一个文档（did = 目录名）",
    )
    target.add_argument(
        "--workdir",
        help="只服务这一个 workdir（did = 目录名；兄弟目录不枚举、不暴露）",
    )
    parser.add_argument(
        "--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）"
    )
    parser.add_argument(
        "--port", type=int, default=0, help="监听端口；0 = 自动分配空闲端口（默认）"
    )
    parser.add_argument(
        "--open", action="store_true", help="绑定成功后用浏览器打开（使用实际端口）"
    )
    parser.add_argument(
        "--migrate",
        action="store_true",
        help="启动前把现有 workdir 的源 PDF 和可用段落索引导入 SQLite",
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="启动前清理超过一天的临时文件与缓存；不删除 assets",
    )
    return parser


def _emit(payload: dict) -> None:
    """按 ``bdt`` 约定往 stdout 写单行 JSON（serve 的启动信封）。"""
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _browsable_host(host: str) -> str:
    """通配地址换成可点击的 loopback（``0.0.0.0`` / ``::`` 本身不是 URL 主机）。"""
    if host in _WILDCARD_HOSTS:
        return "127.0.0.1"
    return host


def _url_host(host: str) -> str:
    """IPv6 字面量加方括号，保证拼出的是合法 URL。"""
    if ":" in host and not host.startswith("["):
        return f"[{host}]"
    return host


def _is_loopback(host: str) -> bool:
    return host in _LOOPBACK_HOSTS or host.startswith("127.")


def _bind_socket(host: str, port: int) -> socket.socket:
    """先自己 bind/listen，拿到真实端口后再交给 uvicorn（``--open`` 依赖这一点）。"""
    infos = socket.getaddrinfo(
        host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE
    )
    family, socktype, proto, _canon, sockaddr = infos[0]
    sock = socket.socket(family, socktype, proto)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(sockaddr)
        sock.listen(128)
    except OSError:
        sock.close()
        raise
    return sock


def _build_store(args: argparse.Namespace) -> DocumentStore:
    if args.root:
        return DocumentStore.for_root(args.root)
    return DocumentStore.for_workdir(args.workdir)


#: uvicorn 日志全部改走 stderr（默认 access log 会写 stdout，污染 bdt 的单行 JSON 约定）。
#: handler 里的 uvicorn formatter 用 "()" 字符串延迟解析，本模块依旧不 import fastapi/uvicorn。
_LOG_CONFIG = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "default": {
            "()": "uvicorn.logging.DefaultFormatter",
            "fmt": "%(levelprefix)s %(message)s",
        },
        "access": {
            "()": "uvicorn.logging.AccessFormatter",
            "fmt": '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
        },
    },
    "handlers": {
        "default": {
            "class": "logging.StreamHandler",
            "formatter": "default",
            "stream": "ext://sys.stderr",
        },
        "access": {
            "class": "logging.StreamHandler",
            "formatter": "access",
            "stream": "ext://sys.stderr",
        },
    },
    "loggers": {
        "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
        "uvicorn.error": {"level": "INFO"},
        "uvicorn.access": {"handlers": ["access"], "level": "INFO", "propagate": False},
    },
}


def _load_app(store: DocumentStore):
    """延迟导入 web 依赖并建 app；缺依赖 → ``ToolError(web_extra_missing)``。"""
    try:
        import uvicorn

        from babeldoc_tools.serve import app as app_module
    except ImportError as exc:
        raise ToolError("web_extra_missing", WEB_EXTRA_HINT, missing=str(exc)) from exc
    return app_module.create_app(store), uvicorn


def _open_browser(url: str) -> None:
    """就绪后打开浏览器；失败只警告（stderr），不影响服务。"""
    try:
        if not webbrowser.open(url):
            sys.stderr.write(f"bdt serve: 无法自动打开浏览器，请手动访问 {url}\n")
    except Exception as exc:  # noqa: BLE001 - 浏览器缺失不阻断服务
        sys.stderr.write(f"bdt serve: 打开浏览器失败（{exc}），请手动访问 {url}\n")


def run(args: argparse.Namespace) -> int:
    """执行 ``bdt serve``：返回进程退出码（0 = 正常停止）。"""
    if not 0 <= args.port <= 65535:
        _emit(
            {
                "ok": False,
                "error": {
                    "code": "invalid_port",
                    "message": f"--port 必须在 0..65535 之间：{args.port}",
                },
            }
        )
        return 1
    try:
        store = _build_store(args)
        if args.cleanup:
            from babeldoc_tools.serve.cleanup import cleanup

            sys.stderr.write(
                json.dumps(cleanup(store.store_base), ensure_ascii=False) + "\n"
            )
        if args.migrate:
            from babeldoc_tools.serve.migrate import migrate_root

            if store.mode != "root":
                raise ToolError("upload_not_supported", "--migrate 需要 --root 模式")
            result = migrate_root(store.root)
            sys.stderr.write(
                f"bdt serve: migrated {len(result['migrated'])} documents; "
                f"skipped {len(result['skipped'])}\n"
            )
        app, uvicorn = _load_app(store)
    except ToolError as exc:
        _emit(
            {
                "ok": False,
                "error": {"code": exc.code, "message": exc.message},
            }
        )
        sys.stderr.write(f"bdt serve: {exc.message}\n")
        return 1

    host = _browsable_host(args.host)
    try:
        sock = _bind_socket(args.host, args.port)
    except OSError as exc:
        _emit(
            {
                "ok": False,
                "error": {
                    "code": "port_unavailable",
                    "message": f"无法监听 {args.host}:{args.port}：{exc}",
                },
            }
        )
        sys.stderr.write(f"bdt serve: 无法监听 {args.host}:{args.port}：{exc}\n")
        return 1

    port = sock.getsockname()[1]
    base = f"http://{_url_host(host)}:{port}"
    _emit(
        {
            "ok": True,
            "data": {
                "url": f"{base}/",
                "api": f"{base}{API_PREFIX}",
                "docs": f"{base}/docs",
                "host": args.host,
                "port": port,
                "mode": store.mode,
                "root": str(store.root),
                "version": __version__,
                "pid": os.getpid(),
            },
        }
    )
    sys.stderr.write(
        f"bdt serve: {base}/ （mode={store.mode}, root={store.root}）"
        "；日志走 stderr，Ctrl-C 停止\n"
    )
    if not _is_loopback(host):
        sys.stderr.write(
            f"bdt serve: 警告：绑定到非 loopback 地址 {args.host}，"
            "W01 无认证，文档根目录将对网络可见\n"
        )
    if args.open:
        _open_browser(f"{base}/")

    config = uvicorn.Config(
        app, host=args.host, port=port, log_level="info", log_config=_LOG_CONFIG
    )
    server = uvicorn.Server(config)
    try:
        server.run(sockets=[sock])
    except KeyboardInterrupt:  # pragma: no cover - uvicorn 通常已处理
        sys.stderr.write("bdt serve: 已停止\n")
    finally:
        with contextlib.suppress(OSError):
            sock.close()
    return 0
