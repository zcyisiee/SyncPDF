"""``/documents/{did}/artifacts``：产物清单 + 白名单 Range 下载（api.md §1.5）。

路由只做三件事：解析路径参数、调 :mod:`babeldoc_tools.serve.artifacts` 判白名单、
把文件交给 Starlette ``FileResponse``。Range 由 ``FileResponse`` 原生支持
（``Accept-Ranges: bytes`` / ``206`` + ``Content-Range`` / 不可满足时 ``416``），
与 pdf.js 的行为直接对齐；这里不手写 Range 解析。

``HEAD`` 必须等价存在（pdf.js 与 curl -I 都靠它拿长度），但 FastAPI 不会为 GET
自动补 HEAD，所以有两个入口。HEAD 用 ``include_in_schema=False`` 注册：它对
OpenAPI 是 GET 的重复描述，且 W02 的守卫测试断言全站 OpenAPI 只有 ``get``。
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

from fastapi import APIRouter
from fastapi import Path as PathParam
from fastapi.responses import FileResponse

from babeldoc_tools.serve import artifacts
from babeldoc_tools.serve.routers.documents import DOCUMENT_ID
from babeldoc_tools.serve.schemas import API_PREFIX
from babeldoc_tools.serve.schemas import ArtifactItem
from babeldoc_tools.serve.schemas import ArtifactsResponse
from babeldoc_tools.serve.store import DocumentStore

__all__ = ["artifacts_router"]

ARTIFACT_NAME = (
    "产物短名 = workdir 相对路径，如 output/paper.mono.pdf、agent/translated.md、"
    "FINAL_REPORT.md；只接受清单内的白名单名字"
)
ARTIFACTS_PATH = "/documents/{did}/artifacts"
ARTIFACT_PATH = "/documents/{did}/artifacts/{name:path}"


def artifacts_router(store: DocumentStore) -> APIRouter:
    """按 store 生成产物路由（与其它只读路由同风格）。"""
    router = APIRouter(prefix=API_PREFIX, tags=["documents"])

    @router.get("/documents/{did}/assets/{digest}", response_class=FileResponse)
    def get_asset(did: str, digest: str) -> FileResponse:
        from babeldoc_tools.common import ToolError
        from babeldoc_tools.serve.asset_store import AssetStore

        store.resolve(did)
        database = store.database
        with database._lock:
            allowed = database.connection.execute(
                "SELECT 1 FROM local_previews WHERE document_id=? AND asset_sha256=? "
                "UNION SELECT 1 FROM pages WHERE document_id=? AND page_asset=? "
                "UNION SELECT 1 FROM exports WHERE document_id=? AND asset_sha256=?",
                (did, digest, did, digest, did, digest),
            ).fetchone()
        if not allowed:
            raise ToolError("artifact_not_found", "该 asset 不属于文档当前产物")
        return FileResponse(
            AssetStore(store.store_base, database).resolve(digest),
            media_type="application/pdf",
        )

    def workdir(did: str) -> Path:
        """did → workdir（did 校验与根目录约束全在 store 里）。"""
        return store.resolve(did)

    def download(did: str, name: str) -> FileResponse:
        """白名单命中的产物 → 源字节（含 Range/HEAD 处理）。

        不设置 ``Content-Disposition``：同一个 URL 既给 pdf.js 预览也给前端"下载"
        按钮用，是否附件由前端决定（``<a download>`` / fetch），服务端不替它决定。
        """
        entry = artifacts.resolve_artifact(workdir(did), name)
        return FileResponse(entry.path, media_type=artifacts.media_type(entry.kind))

    @router.get(
        ARTIFACTS_PATH,
        response_model=ArtifactsResponse,
        summary="可下载产物清单",
        description=(
            "白名单产物的清单（name/path/kind/size/mtime），name 就是下载 URL 的 "
            "{name}。白名单 = 根级 FINAL_REPORT.md(report) / source.pdf(source) + "
            "output/*.pdf(pdf) + agent/*.md(markdown) 与 agent/*.json|*.jsonl(json)；"
            "不递归子目录，debug/ 与 run 临时产物不在其中。没有任何产物时返回空数组"
            "（清单本身可用且确实为空，不是缺产物）。"
        ),
    )
    def list_artifacts(
        did: Annotated[str, PathParam(description=DOCUMENT_ID)],
    ) -> ArtifactsResponse:
        return [
            ArtifactItem(
                name=entry.name,
                path=entry.name,
                kind=entry.kind,
                size=entry.size,
                mtime=entry.mtime,
            )
            for entry in artifacts.list_artifacts(workdir(did))
        ]

    @router.get(
        ARTIFACT_PATH,
        response_class=FileResponse,
        summary="下载产物（支持 Range/HEAD）",
        description=(
            "只允许清单内的白名单名字；越界（`..`/绝对路径/符号链接指向 workdir 外）"
            "与不在白名单内的名字都返回 404 ``artifact_not_found``，不泄露存在性。"
            "支持 `Range: bytes=a-b`（206 + Content-Range，pdf.js 依赖）与 HEAD。"
        ),
        responses={
            200: {
                "description": "产物字节（Content-Type 按 kind 决定）",
                "content": {
                    "application/pdf": {},
                    "application/json": {},
                    "text/markdown": {},
                },
            }
        },
    )
    def get_artifact(
        did: Annotated[str, PathParam(description=DOCUMENT_ID)],
        name: Annotated[str, PathParam(description=ARTIFACT_NAME)],
    ) -> FileResponse:
        return download(did, name)

    @router.head(
        ARTIFACT_PATH,
        include_in_schema=False,
        summary="产物元信息（HEAD）",
    )
    def head_artifact(
        did: Annotated[str, PathParam(description=DOCUMENT_ID)],
        name: Annotated[str, PathParam(description=ARTIFACT_NAME)],
    ) -> FileResponse:
        return download(did, name)

    return router
