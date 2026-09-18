"""``/documents/{did}/draft``：草稿读写（``docs/reference/http-api.md``，W09）。

三个端点，字段模型见 schemas.py，保存/编译行为见当前 HTTP 参考：

- ``GET``：当前草稿（缺文件 → 空草稿 ``revision=0``）；
- ``PATCH``：``{base_revision, paragraphs}``，成功 → ``revision+1`` 并**触发 1.5s
  服务端防抖编译**；``base_revision`` 不匹配 → ``409 revision_conflict``；
- ``DELETE``：清空段落覆盖，``revision`` 继续 +1（不回退），同样触发防抖。

两条写端点的额外守卫（EXECUTION.md 纠偏 7）：**活动任务期间编辑只读** —— 该文档已有
``queued``/``running`` job（含正在跑的编译）时一律 ``409 document_busy``（``detail``
带 job_id），否则 PATCH 会和正在物化草稿的编译抢同一个 revision。校验失败是
``422 draft_invalid``（``detail.errors`` 逐条给字段路径），与 pydantic 的
``validation_error`` 分开：前者是"值不合法"，后者是"请求体形状不对"。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter
from fastapi import Path as PathParam

from babeldoc_tools.common import ToolError
from babeldoc_tools.serve.compile import CompileService
from babeldoc_tools.serve.draft import DraftResponse
from babeldoc_tools.serve.draft import DraftStore
from babeldoc_tools.serve.routers.documents import DOCUMENT_ID
from babeldoc_tools.serve.runner import JobRunner
from babeldoc_tools.serve.schemas import API_PREFIX
from babeldoc_tools.serve.schemas import DraftPatchRequest

__all__ = ["draft_router"]


def draft_router(runner: JobRunner, compiles: CompileService) -> APIRouter:
    """按共享的 runner 生成草稿路由（草稿落在 ``<workdir>/.bdt-serve/draft.json``）。

    ``runner`` 自带 store（did → workdir 的路径边界与草稿锁都在它那边），所以这里
    不需要再收一个 store —— 三条端点都经由 ``runner.drafts`` 拿 DraftStore。
    """
    router = APIRouter(prefix=API_PREFIX, tags=["draft"])

    def draft_store(did: str) -> DraftStore:
        """该文档的草稿存储（did 越界/不存在 → 与其它端点同一个 404/400）。"""
        return runner.drafts.for_did(did)

    def reject_busy(did: str) -> None:
        """活动 job 期间编辑只读 → ``409 document_busy``（detail 指向那个 job）。"""
        active = runner.registry.active_for_did(did)
        if active is None or active.effective_scope == "block":
            return
        raise ToolError(
            "document_busy",
            f"文档 {did} 有活动 job：{active.job_id}（{active.status}）；"
            "任务期间草稿只读，请等它结束后再编辑",
            job_id=active.job_id,
            status=active.status,
            action=active.action,
        )

    @router.get(
        "/documents/{did}/draft",
        response_model=DraftResponse,
        summary="读草稿",
        description=(
            "当前草稿：`revision`、`updated_at`、`paragraphs`（每段 `target`/`layout`/"
            "`updated_at`）。从没写过 → `revision=0` + 空 `paragraphs`（不是 404）。"
        ),
        responses={404: {"description": "document_not_found"}},
    )
    async def get_draft(
        did: Annotated[str, PathParam(description=DOCUMENT_ID)],
    ) -> DraftResponse:
        doc = await draft_store(did).get()
        return DraftResponse(**doc.model_dump(exclude={"version"}))

    @router.patch(
        "/documents/{did}/draft",
        response_model=DraftResponse,
        summary="写草稿（乐观并发 + 防抖编译）",
        description=(
            "`base_revision` 必须等于服务器当前 `revision`，否则 `409 "
            "revision_conflict`（`detail.current_revision`）。`paragraphs` 是"
            "`{段落 id: {target?, layout?}}`，`null` = 删字段、整段 `null` = 删该段。"
            "成功 `revision+1` 并触发 1.5s 防抖编译。活动 job 期间 → `409 "
            "document_busy`；字段/范围不合法 → `422 draft_invalid`。"
        ),
        responses={
            404: {"description": "document_not_found"},
            409: {"description": "document_busy / revision_conflict"},
            422: {"description": "draft_invalid / validation_error"},
        },
    )
    async def patch_draft(
        did: Annotated[str, PathParam(description=DOCUMENT_ID)],
        payload: DraftPatchRequest,
    ) -> DraftResponse:
        store_for_did = draft_store(did)
        reject_busy(did)
        doc = await store_for_did.patch(
            base_revision=payload.base_revision, paragraphs=payload.paragraphs
        )
        # 写成功才排防抖：校验/冲突都没改盘，不该触发编译。
        compiles.schedule(did)
        return DraftResponse(**doc.model_dump(exclude={"version"}))

    @router.delete(
        "/documents/{did}/draft",
        response_model=DraftResponse,
        summary="清空草稿（revision 不回退）",
        description=(
            "删掉全部段落覆盖；`revision` 继续 `+1`（**不回退**，前端靠单调 revision "
            "判断 stale）。返回清空后的草稿，并触发防抖编译。活动 job 期间 → `409 "
            "document_busy`。"
        ),
        responses={
            404: {"description": "document_not_found"},
            409: {"description": "document_busy"},
        },
    )
    async def delete_draft(
        did: Annotated[str, PathParam(description=DOCUMENT_ID)],
    ) -> DraftResponse:
        store_for_did = draft_store(did)
        reject_busy(did)
        doc = await store_for_did.delete()
        compiles.schedule(did)
        return DraftResponse(**doc.model_dump(exclude={"version"}))

    return router
