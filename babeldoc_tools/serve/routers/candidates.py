"""``/documents/{did}/paragraphs/{pid}`` 下的候选重译端点（``docs/frontend/api.md`` §3.6，W11）。

四条端点，形状与错误码都在 §3.6 冻结：

- ``POST /…/paragraphs/{pid}/retranslate`` ``{profile}`` → 202 ``{candidate_id, job_id}``
  （建候选行 + 提交 ``action=retranslate`` job；**不动任何正文产物**）；
- ``GET /…/paragraphs/{pid}/candidates`` → 该段的候选列表（含 ``status``）；
- ``POST /…/paragraphs/{pid}/candidates/{cid}/adopt`` → 200 草稿（``revision+1`` + 防抖编译）；
- ``POST /…/paragraphs/{pid}/candidates/{cid}/reject`` → 200 候选（只改状态）。

三条边界（与 ``jobs``/``draft`` 路由同一套口径）：

- **客户端只说 profile id**：请求体里没有 translator/reviewer/timeout 字段（带了会 422
  ``forbidden_field``，由 :data:`FORBIDDEN_JOB_FIELDS` 的同一个依赖拦）；命令只在服务端
  从 profile 解析后进 argv；
- **生成受排队/忙守卫**（它是一个 job）：活动 job 期间 → 409 ``document_busy``；
  **采用**要写草稿，用草稿写端点同样的忙守卫；**拒绝**不碰草稿，不受忙限制；
- **候选不是译文**：除 adopt 之外，这些端点都不改 ``translated.md`` / ``draft.json`` /
  ``output/``（服务端保证，测试用 mtime/sha 断言）。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Path as PathParam

from babeldoc_tools.serve.candidates import CandidateService
from babeldoc_tools.serve.draft import DraftResponse
from babeldoc_tools.serve.routers.documents import DOCUMENT_ID
from babeldoc_tools.serve.routers.jobs import reject_forbidden_body
from babeldoc_tools.serve.schemas import API_PREFIX
from babeldoc_tools.serve.schemas import CandidateItem
from babeldoc_tools.serve.schemas import CandidateJobAccepted
from babeldoc_tools.serve.schemas import CandidateListResponse
from babeldoc_tools.serve.schemas import CandidateRetranslateRequest

__all__ = ["CANDIDATE_ID_PATH", "PARAGRAPH_ID_PATH", "candidates_router"]

#: 段落 id 路径参数说明（形状 ``[A-Za-z0-9._-]{1,64}``，必须在产物里存在）。
PARAGRAPH_ID_PATH = "段落 id（与 GET /paragraphs 的 id 一致，如 P05-002）"
#: 候选 id 路径参数说明。
CANDIDATE_ID_PATH = "候选 id（``c_`` + 4 位十进制；来自 retranslate / candidates 响应）"


def candidates_router(service: CandidateService) -> APIRouter:
    """按共享的候选服务生成路由（候选落在 ``<workdir>/.bdt-serve/candidates.json``）。"""
    router = APIRouter(prefix=API_PREFIX, tags=["candidates"])

    @router.post(
        "/documents/{did}/paragraphs/{pid}/retranslate",
        response_model=CandidateJobAccepted,
        status_code=202,
        summary="生成重译候选（不采用就不改译文）",
        description=(
            "对不满意段落让 AI 重译：建一条 `pending` 候选并提交 `action=retranslate` "
            "job（同文档串行、全局限流、可取消——与其它 job 同规则）。翻译命令来自 "
            "`profile`（客户端永远不传命令），生成在**隔离副本**里跑，"
            "`agent/translated.md`／`draft.json`／`output/` 一概不动；"
            "候选译文只在人工采用后才会进草稿。"
            "`profile` 缺失／该 profile 没配 translator → 422 `profile_missing`；"
            "未知 profile → 422 `unknown_profile`；pid 不存在 → 404 `paragraph_not_found`；"
            "有活动 job → 409 `document_busy`。"
        ),
        responses={
            404: {"description": "document_not_found / paragraph_not_found"},
            409: {"description": "document_busy"},
            422: {"description": "profile_missing / unknown_profile / forbidden_field"},
        },
    )
    async def retranslate(
        did: Annotated[str, PathParam(description=DOCUMENT_ID)],
        pid: Annotated[str, PathParam(description=PARAGRAPH_ID_PATH)],
        payload: CandidateRetranslateRequest,
        _forbidden: Annotated[None, Depends(reject_forbidden_body)],
    ) -> CandidateJobAccepted:
        item, record = await service.request_retranslate(did, pid, payload.profile, payload.thinking)
        return CandidateJobAccepted(candidate_id=item.id, job_id=record.job_id)

    @router.get(
        "/documents/{did}/paragraphs/{pid}/candidates",
        response_model=CandidateListResponse,
        summary="该段的候选列表",
        description=(
            "该段的重译候选：最新的 `pending` 在前，其后是已决定的候选（新 → 旧）。"
            "`candidate_target=null` 表示还在生成中（job 结束前）；生成失败/取消的候选行会被"
            "删掉，不留在列表里。pid 不在段落产物里 → 404 `paragraph_not_found`。"
        ),
        responses={404: {"description": "document_not_found / paragraph_not_found"}},
    )
    def list_candidates(
        did: Annotated[str, PathParam(description=DOCUMENT_ID)],
        pid: Annotated[str, PathParam(description=PARAGRAPH_ID_PATH)],
    ) -> CandidateListResponse:
        return CandidateListResponse(pid=pid, items=service.list_for_pid(did, pid))

    @router.post(
        "/documents/{did}/paragraphs/{pid}/candidates/{cid}/adopt",
        response_model=DraftResponse,
        summary="采用候选（写草稿 + 防抖编译）",
        description=(
            "人工采用：把候选译文写成该段的草稿 `target`（走草稿通道，`revision+1`）并触发"
            "1.5s 防抖编译，然后把候选标成 `adopted`。返回**新草稿**（与 `PATCH /draft` "
            "同一形状），前端可直接写进缓存。不需要 `base_revision`（服务端自己在写锁内取"
            "当前 revision）。已有相同译文覆盖 → 仍 +1（不猜用户意图）。"
            "cid 不存在 → 404 `candidate_not_found`；已采用/已拒绝 → 409 `candidate_decided`；"
            "还在生成中 → 409 `candidate_not_ready`；有活动 job（要写草稿）→ 409 `document_busy`。"
        ),
        responses={
            404: {"description": "document_not_found / candidate_not_found"},
            409: {
                "description": "document_busy / candidate_decided / candidate_not_ready"
            },
            422: {"description": "draft_invalid（候选文本写不进草稿时不静默）"},
        },
    )
    async def adopt_candidate(
        did: Annotated[str, PathParam(description=DOCUMENT_ID)],
        pid: Annotated[str, PathParam(description=PARAGRAPH_ID_PATH)],
        cid: Annotated[str, PathParam(description=CANDIDATE_ID_PATH)],
    ) -> DraftResponse:
        _item, draft = await service.adopt(did, pid, cid)
        return draft

    @router.post(
        "/documents/{did}/paragraphs/{pid}/candidates/{cid}/reject",
        response_model=CandidateItem,
        summary="拒绝候选（只改状态）",
        description=(
            "把候选标成 `rejected`：不改译文、不改草稿、不触发编译（因此**不**受活动 job 的"
            "草稿只读守卫限制）。重复拒绝是幂等的（返回当前候选，不再改任何东西）；"
            "已采用的候选不能用拒绝改回去 → 409 `candidate_decided`；"
            "cid 不存在 → 404 `candidate_not_found`。"
        ),
        responses={
            404: {"description": "document_not_found / candidate_not_found"},
            409: {"description": "candidate_decided"},
        },
    )
    async def reject_candidate(
        did: Annotated[str, PathParam(description=DOCUMENT_ID)],
        pid: Annotated[str, PathParam(description=PARAGRAPH_ID_PATH)],
        cid: Annotated[str, PathParam(description=CANDIDATE_ID_PATH)],
    ) -> CandidateItem:
        return await service.reject(did, pid, cid)

    return router
