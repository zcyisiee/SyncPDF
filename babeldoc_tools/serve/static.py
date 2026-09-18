"""``bdt serve`` 的前端静态伺服（W15）：``web/dist`` 在同一个进程、同一个端口上伺候。

``pnpm build`` 的产物（``web/dist``）存在时，``create_app`` 把它挂在 ``/``：SPA 的
index、hash 资源、以及前端路由的**回退**（非接口路径一律给 index.html，由浏览器端
hash 路由接管）。dist 不存在（没跑过 build）就**静默跳过** —— 只伺服 ``/api/v1``，
不报错、不改启动信封（``bdt serve`` 的 stdout 仍是那一条 JSON）。

缓存策略按「文件名是否带内容哈希」分两档：

- ``assets/*``：Vite 给每个产物名里塞了内容哈希，内容一变文件名就变 → 一年
  ``immutable``；
- 其它（``index.html``、``pdfjs/*`` 等由 ``scripts/sync-pdfjs-assets.mjs`` 复制的
  固定名静态资源）：``no-cache`` —— 每次回源校验，新构建立刻生效。

本模块**不**参与接口路由：``/api``、``/docs``、``/redoc``、``/openapi.json`` 一律
不落进 SPA 回退（未知的 ``/api/v1/...`` 照旧是 JSON 错误信封的 404，不拿 HTML 冒充）。
挂载点永远是**最后**注册的（见 :func:`mount_frontend` 的调用方），前面的路由先匹配。
"""

from __future__ import annotations

from pathlib import Path
from pathlib import PurePosixPath

from fastapi import FastAPI
from fastapi.responses import FileResponse
from starlette.exceptions import HTTPException

__all__ = [
    "ASSETS_DIR",
    "DIST_RELATIVE",
    "IMMUTABLE_CACHE",
    "INDEX_FILE",
    "NO_CACHE",
    "RESERVED_PREFIXES",
    "frontend_dist",
    "mount_frontend",
]

#: 构建产物相对**仓库根**的路径（``pnpm build`` 的 ``outDir``，见 ``web/vite.config.ts``）。
DIST_RELATIVE = ("web", "dist")

#: SPA 入口文件名（也在 → dist 里没有它就当「没构建过」）。
INDEX_FILE = "index.html"

#: 带内容哈希的资源目录（Vite 默认）：长缓存 + ``immutable``。
ASSETS_DIR = "assets"

#: ``index.html`` 与其它固定名静态资源：每次回源（新构建立刻生效）。
NO_CACHE = "no-cache"

#: 哈希资源：一年 + immutable（文件名变了才会重新下载）。
IMMUTABLE_CACHE = "public, max-age=31536000, immutable"

#: 服务自己的接口/文档前缀：**不**参与 SPA 回退（未知路径照旧 404 JSON）。
RESERVED_PREFIXES = ("/api", "/docs", "/redoc", "/openapi.json")


def _repo_root() -> Path:
    """本文件是 ``<repo>/babeldoc_tools/serve/static.py`` → 上溯三层是仓库根。"""
    return Path(__file__).resolve().parents[2]


def frontend_dist() -> Path | None:
    """``web/dist``（要求里面有 ``index.html``）；没构建过 → ``None``（静默跳过）。"""
    dist = _repo_root().joinpath(*DIST_RELATIVE)
    return dist if (dist / INDEX_FILE).is_file() else None


def _cache_control(relative: str) -> str:
    """``assets/`` 下的哈希资源长缓存，其余一律 ``no-cache``。"""
    parts = PurePosixPath(relative).parts
    return IMMUTABLE_CACHE if parts[:1] == (ASSETS_DIR,) else NO_CACHE


def _is_reserved(path: str) -> bool:
    """接口/文档路径不交给前端（``path`` 是路由参数，不带前导斜杠）。"""
    full = f"/{path}"
    return any(
        full == prefix or full.startswith(f"{prefix}/") for prefix in RESERVED_PREFIXES
    )


def _resolve_within(root: Path, relative: str) -> Path | None:
    """把请求路径解析成 dist 内**真实存在的文件**；越界（含符号链接穿透）或目录 → ``None``。

    以 :meth:`Path.resolve` 之后的路径做前缀判定：dist 里若有指向外部的符号链接，
    解析结果落在 dist 之外 → 不伺服（与上传端点「符号链接不穿透」同一条规则）。
    """
    if relative == "" or "\x00" in relative:
        return None
    try:
        candidate = (root / relative).resolve()
    except OSError:  # pragma: no cover - 路径过长/权限异常一律当不存在
        return None
    if candidate != root and root not in candidate.parents:
        return None
    return candidate if candidate.is_file() else None


def mount_frontend(app: FastAPI, dist: Path | None = None) -> bool:
    """把前端构建产物挂到 ``/``（GET/HEAD）；没构建过 → 什么都不做并返回 ``False``。

    调用方必须在**注册完全部接口路由之后**再调用本函数：``/{path:path}`` 会接住所有
    未被前面路由认领的路径。``dist`` 显式给出时（测试用）不看仓库里的 ``web/dist``。
    """
    target = dist if dist is not None else frontend_dist()
    if target is None:
        return False
    root = target.resolve()
    index = root / INDEX_FILE
    if not index.is_file():
        return False

    @app.api_route(
        "/{path:path}",
        methods=["GET", "HEAD"],
        response_class=FileResponse,
        include_in_schema=False,
        name="frontend",
    )
    def frontend(path: str) -> FileResponse:
        if _is_reserved(path):
            # 未知的接口路径是 JSON 404（不拿 SPA 的 HTML 冒充接口响应）
            raise HTTPException(status_code=404, detail="Not Found")
        static_file = _resolve_within(root, path)
        if static_file is None:
            # SPA 回退：前端的 hash 路由在浏览器里解析，服务端一律给入口
            return FileResponse(index, headers={"cache-control": NO_CACHE})
        return FileResponse(
            static_file, headers={"cache-control": _cache_control(path)}
        )

    return True
