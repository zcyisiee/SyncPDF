"""``/documents/{did}/versions``：版本归档清单 + 任一历史版本下载（api.md §3.7，W12）。

两条端点（都是 GET，不改任何东西）：

- ``GET /documents/{did}/versions`` → 清单 **新 → 旧** + 当前编译上下文
  （``current_revision``/``stale`` 取自 ``compile``，§3.2）；
- ``GET /documents/{did}/versions/{revision}/pdf`` → 那一版的字节（``inline`` 下载，
  文件名 ``<原产物名去 .pdf>.r<revision>.pdf``）。

边界：``revision`` 走**字符串**路径参数再由 :mod:`babeldoc_tools.serve.versions` 严格
判定（非十进制数字 / 不在清单 / 文件缺失 / 符号链接越界一律 404 ``version_not_found``，
不泄露存在性）。归档目录 ``.bdt-serve/versions/`` **不在** ``GET /artifacts`` 的白名单里，
所以这条路径是读版本 PDF 的唯一入口。

``HEAD`` 不为下载端点注册：它只服务 ``<a download>`` 与 curl（pdf.js 不预览历史版本），
而 W02 的守卫测试断言全站 OpenAPI 只有 ``get``。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter
from fastapi import Path as PathParam
from fastapi.responses import FileResponse

from babeldoc_tools.serve import versions
from babeldoc_tools.serve.compile import compile_status
from babeldoc_tools.serve.routers.documents import DOCUMENT_ID
from babeldoc_tools.serve.schemas import API_PREFIX
from babeldoc_tools.serve.schemas import VersionsResponse
from babeldoc_tools.serve.store import DocumentStore

__all__ = ["VERSION_REVISION_PATH", "versions_router"]

#: 版本号路径参数（字符串判定：非法值报 404，不报 422）。
VERSION_REVISION_PATH = (
    "版本号 = 该版本的编译 revision（十进制整数；来自 `GET /versions` 的 `items[].revision`）"
)


def versions_router(store: DocumentStore) -> APIRouter:
    """按 store 生成版本路由（版本归档落在 ``<workdir>/.bdt-serve/`` 下）。"""
    router = APIRouter(prefix=API_PREFIX, tags=["documents"])

    @router.get(
        "/documents/{did}/versions",
        response_model=VersionsResponse,
        summary="版本归档清单（新 → 旧）",
        description=(
            "每次**成功**编译发布的产物自动归档成一个版本（`revision` = 当时捕获的草稿"
            "revision），保留最近 50 个（超出淘汰最旧的版本文件与清单行）。响应是清单的"
            "**新 → 旧**倒序 + 当前编译上下文：`current_revision`/`stale` 取自详情端点的"
            "`compile`（§3.2）—— 当前可下载的那一版恒是 `output/` 的最新发布，不是清单的"
            "最新行。`trigger` ∈ `debounce`（草稿保存后的服务端自动编译）/ `manual`（显式"
            "POST compile）；`quality` 是发布时刻的质量快照，**只记录不门禁**"
            "（`pipeline_ok=false` 的版本照样可下载）。从没编译成功过 → 空数组（不是 404）。"
        ),
        responses={404: {"description": "document_not_found"}},
    )
    def list_versions(
        did: Annotated[str, PathParam(description=DOCUMENT_ID)],
    ) -> VersionsResponse:
        workdir = store.resolve(did)
        status = compile_status(workdir)
        return VersionsResponse(
            did=did,
            current_revision=status.revision,
            stale=status.stale,
            items=list(reversed(versions.list_versions(workdir))),
        )

    @router.get(
        "/documents/{did}/versions/{revision}/pdf",
        response_class=FileResponse,
        summary="下载指定历史版本",
        description=(
            "返回那一版的字节（`application/pdf`，`Content-Disposition: inline`，文件名 "
            "`<原产物名去 .pdf>.r<revision>.pdf`）。只认清单里有、且确实落在 "
            "`.bdt-serve/versions/` 下的数字名文件：非数字 / 不在清单 / 文件缺失 / "
            "符号链接越界一律 404 `version_not_found`（同一个码，不泄露存在性）。"
            "当前版本仍走 `GET /artifacts`（`output/` 的最新发布）。"
        ),
        responses={
            200: {
                "description": "该版本的 PDF 字节",
                "content": {"application/pdf": {}},
            },
            404: {"description": "document_not_found / version_not_found"},
        },
    )
    def download_version(
        did: Annotated[str, PathParam(description=DOCUMENT_ID)],
        revision: Annotated[str, PathParam(description=VERSION_REVISION_PATH)],
    ) -> FileResponse:
        found = versions.read_version_pdf(store.resolve(did), revision)
        return FileResponse(
            found.path,
            media_type="application/pdf",
            filename=versions.download_name(found.item),
            # 同一个 URL 也可能被浏览器直接打开预览：由前端 `<a download>` 决定是否落盘，
            # 服务端只给出正确的名字（`inline` 而不是 `attachment`）。
            content_disposition_type="inline",
        )

    return router
