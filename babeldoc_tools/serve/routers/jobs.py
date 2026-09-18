"""``/documents/{did}/jobs`` 与 ``/jobs/{jid}``：提交 / 查询 / 取消（api.md §3.4，W07/W09）。

本模块只做 HTTP 形状：校验请求体、调 :class:`babeldoc_tools.serve.runner.JobRunner`
（``action=compile`` 走 :class:`babeldoc_tools.serve.compile.CompileService`）、
交给 ``response_model`` 序列化。三条边界：

- **客户端输入只有** ``action``/``from``/``pages``/``dual``/``profile``（``compile`` 另加
  ``scope``/``base_revision``）；translator/reviewer/timeout 之类命令与密钥字段收到即
  422 ``forbidden_field``（见 :data:`FORBIDDEN_JOB_FIELDS`），argv 只在
  :mod:`babeldoc_tools.serve.runner` / :mod:`babeldoc_tools.serve.compile` 里构造；
- **同文档串行**：第二个活动 job → 409 ``document_busy``（detail 带现有 job_id）；
- **诚实不冒充**：``retranslate`` 未实现 → 422 ``action_not_available``
  （detail 给归属任务），不返回假 job。

``compile`` 的 provider 字段是**不需要**的（不调翻译/审查）：客户端给了也不进 job
记录（前端把当前选中的 profile 一起发过来不会被拒）。它另有 ``scope``
（v1 页级回退全量，见 :meth:`CompileService.request_compile`）与 ``base_revision``
（不匹配 → 409 ``revision_conflict``）。

两条 POST（提交 + 取消）是 W07 引入的写端点，``tests/test_serve_app.py`` 的写端点
守卫用白名单放行它们，其余路由仍然必须只有 GET。
"""

from __future__ import annotations

from typing import Annotated
from typing import Any
from typing import Literal

from fastapi import APIRouter
from fastapi import Depends
from fastapi import Path as PathParam
from fastapi import Query
from fastapi import Request
from fastapi import Response
from fastapi.responses import FileResponse

from babeldoc_tools.common import ToolError
from babeldoc_tools.serve import artifacts
from babeldoc_tools.serve.compile import CompileService
from babeldoc_tools.serve.compile import compile_status
from babeldoc_tools.serve.jobs import TERMINAL_STATUSES
from babeldoc_tools.serve.jobs import JobRecord
from babeldoc_tools.serve.routers.documents import DOCUMENT_ID
from babeldoc_tools.serve.runner import JobRunner
from babeldoc_tools.serve.schemas import API_PREFIX
from babeldoc_tools.serve.schemas import JOB_ACTION_HINT
from babeldoc_tools.serve.schemas import JOB_ACTION_PHASE
from babeldoc_tools.serve.schemas import JOB_ACTIONS_IMPLEMENTED
from babeldoc_tools.serve.schemas import JobAccepted
from babeldoc_tools.serve.schemas import JobCreateRequest
from babeldoc_tools.serve.store import DocumentStore
from babeldoc_tools.serve.versions import TRIGGER_MANUAL

__all__ = [
    "FORBIDDEN_JOB_FIELDS",
    "JOB_ID_PATH",
    "JOB_STATUS_FILTER",
    "jobs_router",
    "reject_forbidden_body",
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


async def reject_forbidden_body(request: Request) -> None:
    """FastAPI 依赖：**模型校验之前**读原始 body，禁止字段一律 422 ``forbidden_field``。

    不能只写在路由函数体里：路由的请求体校验先于函数体执行，于是
    ``{"action": "run", "translator": "..."}``（顺带缺 ``profile``）会先炸
    ``validation_error``，把"不接受命令字段"这个更重要的信号盖掉（W08 冒烟实测）。
    非 JSON 的 body 交给 FastAPI 自己报 422。
    """
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):  # pragma: no cover - FastAPI 先报 422
        return
    reject_forbidden_fields(body)


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


def jobs_router(
    store: DocumentStore, runner: JobRunner, compiles: CompileService
) -> APIRouter:
    """按 store 生成 job 路由（job 状态落在 ``store.store_base`` 下）。

    ``runner``/``compiles`` 由 :func:`babeldoc_tools.serve.app.create_app` 建好传入：
    同一个 job 注册表要同时给草稿路由（活动 job 期间编辑只读）与编译服务（防抖提交）
    用，不能每个路由各建一个（那样重启恢复会跑两遍、活动表也会分裂）。
    """
    router = APIRouter(prefix=API_PREFIX, tags=["jobs"])

    @router.post(
        "/documents/{did}/jobs",
        response_model=JobAccepted,
        status_code=202,
        summary="提交 job（run / check / compile）",
        description=(
            "以子进程跑 `bdt run`（同文档串行：已有活动 job → 409 document_busy；"
            "全局最多 2 个并发，超出排队 queued）。202 的 status 恒为 queued，"
            "真实状态轮询 GET /jobs/{jid}。`compile`（W09）在隔离副本里跑"
            "apply+build 并原子发布 PDF：`scope=pages` 按已批准设计回退全量"
            "（记录 requested_scope/effective_scope/downgrade_reason），"
            "`base_revision` 与当前草稿不一致 → 409 revision_conflict。"
            "retranslate 未实现 → 422 action_not_available。客户端只能给"
            "action/from/pages/dual/profile/scope/base_revision："
            "translator/reviewer/timeout 之类一律 422 forbidden_field。"
        ),
        responses={
            409: {
                "description": "document_busy / revision_conflict（compile 的 base_revision 不符）"
            },
            422: {
                "description": "unknown_profile / action_not_available / forbidden_field"
            },
        },
    )
    async def create_job(
        did: Annotated[str, PathParam(description=DOCUMENT_ID)],
        payload: JobCreateRequest,
        response: Response,
        _forbidden: Annotated[None, Depends(reject_forbidden_body)],
    ) -> JobAccepted:
        if payload.action not in JOB_ACTIONS_IMPLEMENTED:
            raise ToolError(
                "action_not_available",
                f"action={payload.action} 不在 jobs 端点：候选重译要绑定段落"
                "（pid + 候选 id 由服务端发号），入口是段落下的 retranslate 端点",
                action=payload.action,
                phase=JOB_ACTION_PHASE.get(payload.action, "未排期"),
                hint=JOB_ACTION_HINT.get(payload.action),
            )
        if payload.action == "compile":
            # profile 是可选的（compile 不调 provider）：给了也不进记录，不报错 ——
            # 前端把当前选中的 profile 一起发过来是正常行为。
            _reject_run_only_fields(payload)
            if payload.thinking is not None:
                raise ToolError("forbidden_field", "compile does not use thinking")
            if payload.reviewer_profile is not None:
                raise ToolError("forbidden_field", "compile does not use AI review")
            record = await compiles.request_compile(
                did,
                scope=payload.scope or "full",
                base_revision=payload.base_revision,
                # 显式 POST 的触发原因（防抖自动编译走 CompileService 内部的
                # debounce 分支）；版本归档靠它区分自动/手动（api.md §3.7）。
                trigger=TRIGGER_MANUAL,
            )
        else:
            _reject_compile_only_fields(payload)
            if payload.action == "check":
                _reject_run_only_fields(payload)
                from_stage = "check"
            else:
                # 缺省留给 CLI（= parse）：契约里 from 是可选字段。
                from_stage = payload.from_stage
            record = await runner.submit(
                did=did,
                action=payload.action,
                from_stage=from_stage,
                pages=payload.pages if payload.action == "run" else None,
                dual=payload.dual if payload.action == "run" else False,
                profile_id=payload.profile or "",
                reviewer_profile=payload.reviewer_profile,
                thinking=payload.thinking,
                # 词表开关（W13）：客户端只给布尔，注入路径由服务端在 argv 构造时取。
                use_glossary=payload.use_glossary,
            )
        response.headers["Location"] = f"{API_PREFIX}/jobs/{record.job_id}"
        return JobAccepted(job_id=record.job_id, action=payload.action)

    @router.post(
        "/documents/{did}/export",
        response_model=JobAccepted,
        status_code=202,
        summary="导出当前 revision",
    )
    async def export_document(
        did: Annotated[str, PathParam(description=DOCUMENT_ID)],
        payload: dict[str, Any] | None = None,
    ) -> JobAccepted:
        current = compiles.drafts.for_did(did).read()
        body = payload or {}
        base_revision = body.get("base_revision", current.revision)
        record = await compiles.request_compile(
            did, scope="full", base_revision=base_revision, trigger=TRIGGER_MANUAL
        )
        return JobAccepted(job_id=record.job_id, action="compile")

    @router.get(
        "/documents/{did}/exports/latest",
        summary="下载最新成功导出",
    )
    def latest_export(did: Annotated[str, PathParam(description=DOCUMENT_ID)]) -> FileResponse:
        workdir = store.resolve(did)
        status = compile_status(workdir)
        if status.status != "ok" or status.stale or not status.artifact:
            raise ToolError("export_not_ready", "当前 revision 尚未成功导出")
        name = status.artifact.get("name")
        if not isinstance(name, str):
            raise ToolError("export_not_ready", "导出产物元数据不可用")
        entry = artifacts.resolve_artifact(workdir, f"output/{name}")
        return FileResponse(entry.path, media_type="application/pdf")

    @router.post(
        "/documents/{did}/blocks/{block_id}/compile",
        response_model=JobAccepted,
        status_code=202,
        summary="编译单个 block",
    )
    async def compile_block(
        did: Annotated[str, PathParam(description=DOCUMENT_ID)],
        block_id: str,
        payload: dict[str, Any] | None = None,
    ) -> JobAccepted:
        import re

        if not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", block_id):
            raise ToolError("block_not_found", "block_id 不合法")
        current = compiles.drafts.for_did(did).read()
        if block_id not in current.paragraphs:
            from babeldoc_tools.serve.views import paragraphs
            from babeldoc_tools.serve.workdir import WorkdirReader

            items = paragraphs(WorkdirReader(store.resolve(did)), None)
            if not any(item.id == block_id for item in items):
                raise ToolError("block_not_found", f"block 不存在：{block_id}")
        base_revision = (payload or {}).get("base_revision", current.revision)
        if not isinstance(base_revision, int) or base_revision != current.revision:
            raise ToolError("revision_conflict", "草稿 revision 已变化", current_revision=current.revision)
        record = await compiles.request_compile(
            did, scope="pages", base_revision=base_revision, trigger=TRIGGER_MANUAL
        )
        return JobAccepted(job_id=record.job_id, action="compile")

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


def _reject_compile_only_fields(payload: JobCreateRequest) -> None:
    """``scope``/``base_revision`` 只对 ``action=compile`` 有效（api.md §3.4）。"""
    for field, value in (
        ("scope", payload.scope),
        ("base_revision", payload.base_revision),
    ):
        if value is not None:
            raise ToolError(
                "forbidden_field",
                f"{field} 只对 action=compile 有效（当前 action={payload.action}）",
                field=field,
                action=payload.action,
            )
