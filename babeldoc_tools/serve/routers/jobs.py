"""``/documents/{did}/jobs`` 与 ``/jobs/{jid}``：提交 / 查询 / 取消（api.md §3.4，W07）。

本模块只做 HTTP 形状：校验请求体、调 :class:`babeldoc_tools.serve.runner.JobRunner`、
交给 ``response_model`` 序列化。三条边界：

- **客户端输入只有** ``action``/``from``/``pages``/``dual``/``profile``；
  translator/reviewer/timeout 之类命令与密钥字段收到即 422 ``forbidden_field``
  （见 :data:`FORBIDDEN_JOB_FIELDS`），argv 只在 :mod:`babeldoc_tools.serve.runner` 里构造；
- **同文档串行**：第二个活动 job → 409 ``document_busy``（detail 带现有 job_id）；
- **诚实不冒充**：``retranslate``/``compile`` 未实现 → 422 ``action_not_available``
  （detail 给归属任务），不返回假 job。

两条 POST（提交 + 取消）是 W07 引入的写端点，``tests/test_serve_app.py`` 的写端点
守卫用白名单放行它们，其余路由仍然必须只有 GET。
"""

from __future__ import annotations

from typing import Annotated
from typing import Any
from typing import Literal

from fastapi import APIRouter
from fastapi import Path as PathParam
from fastapi import Query
from fastapi import Request
from fastapi import Response

from babeldoc_tools.common import ToolError
from babeldoc_tools.serve.jobs import TERMINAL_STATUSES
from babeldoc_tools.serve.jobs import JobRecord
from babeldoc_tools.serve.routers.documents import DOCUMENT_ID
from babeldoc_tools.serve.runner import JobRunner
from babeldoc_tools.serve.schemas import API_PREFIX
from babeldoc_tools.serve.schemas import JOB_ACTION_PHASE
from babeldoc_tools.serve.schemas import JOB_ACTIONS_IMPLEMENTED
from babeldoc_tools.serve.schemas import JobAccepted
from babeldoc_tools.serve.schemas import JobCreateRequest
from babeldoc_tools.serve.store import DocumentStore

__all__ = [
    "FORBIDDEN_JOB_FIELDS",
    "JOB_ID_PATH",
    "JOB_STATUS_FILTER",
    "jobs_router",
    "reject_forbidden_fields",
]

#: **客户端不得自带**的字段（api.md §3.4 / EXECUTION.md 第 8 条）：它们是命令、密钥
#: 或服务端护栏。收到（非 null）→ 422 ``forbidden_field``，而不是静默忽略 —— 静默
#: 忽略会让前端以为"我传的命令生效了"。
FORBIDDEN_JOB_FIELDS = (
    "translator",
    "reviewer",
    "timeout",
    "shell",
    "command",
    "cmd",
    "env",
    "api_key",
    "token",
)

#: ``jobs/{jid}`` 路径参数说明。
JOB_ID_PATH = "job id（``j_`` + 26 字符 ULID 风格；来自 POST jobs 的响应）"
#: ``GET /documents/{did}/jobs`` 的状态过滤说明。
JOB_STATUS_FILTER = "只看该状态的 job（省略 = 全部，新 → 旧）"


def reject_forbidden_fields(body: Any) -> None:
    """请求体里出现禁止字段 → ``ToolError(forbidden_field)``（含字段名）。"""
    if not isinstance(body, dict):
        return
    for field in FORBIDDEN_JOB_FIELDS:
        if body.get(field) is not None:
            raise ToolError(
                "forbidden_field",
                (
                    f"{field} 不由客户端提供：job 只接受 profile id，"
                    "translator/reviewer/timeout 由服务端从 profile 解析"
                ),
                field=field,
            )


def jobs_router(store: DocumentStore) -> APIRouter:
    """按 store 生成 job 路由（job 状态落在 ``store.store_base`` 下）。"""
    router = APIRouter(prefix=API_PREFIX, tags=["jobs"])
    # JobRunner 建立时就 load 一次持久化快照：上次残留的 queued/running → interrupted
    # （不自动重跑、不凭孤立 pid 发信号），之后所有 job 读写都走这一个实例。
    runner = JobRunner(store)

    @router.post(
        "/documents/{did}/jobs",
        response_model=JobAccepted,
        status_code=202,
        summary="提交 job（run / check）",
        description=(
            "以子进程跑 `bdt run`（同文档串行：已有活动 job → 409 document_busy；"
            "全局最多 2 个并发，超出排队 queued）。202 的 status 恒为 queued，"
            "真实状态轮询 GET /jobs/{jid}。retranslate/compile 未实现 → 422 "
            "action_not_available。客户端只能给 action/from/pages/dual/profile："
            "translator/reviewer/timeout 之类一律 422 forbidden_field。"
        ),
        responses={
            409: {"description": "document_busy：同文档已有活动 job"},
            422: {
                "description": "unknown_profile / action_not_available / forbidden_field"
            },
        },
    )
    async def create_job(
        did: Annotated[str, PathParam(description=DOCUMENT_ID)],
        payload: JobCreateRequest,
        request: Request,
        response: Response,
    ) -> JobAccepted:
        # 禁止字段必须在模型校验之前看原始 body：模型里没有这些键，默认行为是忽略。
        reject_forbidden_fields(await _body_object(request))
        if payload.action not in JOB_ACTIONS_IMPLEMENTED:
            raise ToolError(
                "action_not_available",
                f"action={payload.action} 还没实现：job 的 argv 与质量门禁都要按实"
                "阶段拼装，不返回假 job",
                action=payload.action,
                phase=JOB_ACTION_PHASE.get(payload.action, "未排期"),
            )
        if payload.action != "run":
            _reject_run_only_fields(payload)
            from_stage = "check"
        else:
            # 缺省留给 CLI（= parse）：契约里 from 是可选字段。
            from_stage = payload.from_stage
        # 文档不在服务范围/越界 → 404/400；同文档已有活动 job → 409（都在 runner 里）。
        record = await runner.submit(
            did=did,
            action=payload.action,
            from_stage=from_stage,
            pages=payload.pages if payload.action == "run" else None,
            dual=payload.dual if payload.action == "run" else False,
            profile_id=payload.profile,
        )
        response.headers["Location"] = f"{API_PREFIX}/jobs/{record.job_id}"
        return JobAccepted(job_id=record.job_id, action=payload.action)

    @router.get(
        "/jobs/{jid}",
        response_model=JobRecord,
        summary="job 全状态",
        description=(
            "job 快照全字段（envelope 是子进程 stdout 收尾信封原文，最长 4KB）。"
            "status ∈ queued|running|succeeded|failed|canceled|interrupted；"
            "pid/pgid 只在 running 时有值。"
        ),
        responses={404: {"description": "job_not_found"}},
    )
    def get_job(jid: Annotated[str, PathParam(description=JOB_ID_PATH)]) -> JobRecord:
        return runner.require(jid)

    @router.post(
        "/jobs/{jid}/cancel",
        response_model=JobRecord,
        status_code=202,
        summary="取消 job（终止整个进程组）",
        description=(
            "幂等：活动 job → 202，已终态 → 200 原样返回。running 时给整个进程组发"
            "SIGTERM（连带 translator 孙进程）→ 5s 宽限 → SIGKILL，进程真的退出后才置"
            " canceled 并释放文档槽；queued 直接 canceled。不自动重跑。"
        ),
        responses={
            200: {"description": "已终态：幂等返回当前状态"},
            404: {"description": "job_not_found"},
        },
    )
    async def cancel_job(
        jid: Annotated[str, PathParam(description=JOB_ID_PATH)],
        response: Response,
    ) -> JobRecord:
        current = runner.require(jid)
        response.status_code = 200 if current.status in TERMINAL_STATUSES else 202
        return await runner.cancel(jid)

    @router.get(
        "/documents/{did}/jobs",
        response_model=list[JobRecord],
        summary="该文档的 job 列表",
        description=(
            "该文档的全部 job，新 → 旧（job id 单调 ⇒ 倒序即时间倒序）；"
            "`?status=` 按状态过滤（取值同 job 的 status）。"
        ),
        responses={404: {"description": "document_not_found"}},
    )
    def list_jobs(
        did: Annotated[str, PathParam(description=DOCUMENT_ID)],
        status: Annotated[
            Literal[
                "queued", "running", "succeeded", "failed", "canceled", "interrupted"
            ]
            | None,
            Query(description=JOB_STATUS_FILTER),
        ] = None,
    ) -> list[JobRecord]:
        store.resolve(did)  # 文档不存在 / 不在--workdir 范围 → 404
        return runner.list_for_did(did, status=status)

    return router


def _reject_run_only_fields(payload: JobCreateRequest) -> None:
    """``from``/``pages``/``dual`` 只对 ``action=run`` 有效（api.md §3.4）。"""
    for field, value in (
        ("from", payload.from_stage),
        ("pages", payload.pages),
        ("dual", payload.dual or None),
    ):
        if value is not None:
            raise ToolError(
                "forbidden_field",
                f"{field} 只对 action=run 有效（当前 action={payload.action}）",
                field=field,
                action=payload.action,
            )


async def _body_object(request: Request) -> Any:
    """读原始请求体（FastAPI 已解析过，这里用的是同一份缓存）；非 JSON → ``None``。"""
    try:
        return await request.json()
    except (ValueError, UnicodeDecodeError):  # pragma: no cover - FastAPI 先报 422
        return None
