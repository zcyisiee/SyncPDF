"""FastAPI app factory：``bdt serve`` 的 HTTP 层（只读）。

端点：

- ``GET /api/v1/health``（W01）
- ``GET /api/v1/documents`` 及其只读子资源（W02，见
  :mod:`babeldoc_tools.serve.routers.documents`）
- ``GET /api/v1/documents/{did}/events`` + ``/events/stream``（W03，见
  :mod:`babeldoc_tools.serve.routers.events`；W14 起 SSE 同一条流里带虚拟 kind
  ``job_update``）
- ``GET/HEAD /api/v1/documents/{did}/artifacts[/{name}]``（W03，见
  :mod:`babeldoc_tools.serve.routers.artifacts`）
- ``POST /api/v1/documents/{did}/jobs``、``GET /api/v1/jobs/{jid}``、
  ``POST /api/v1/jobs/{jid}/cancel``、``GET /api/v1/documents/{did}/jobs``
  （W07，见 :mod:`babeldoc_tools.serve.routers.jobs`；W09 起支持 ``action=compile``）
- ``GET/PATCH/DELETE /api/v1/documents/{did}/draft``（W09，见
  :mod:`babeldoc_tools.serve.routers.draft`）
- ``POST /api/v1/documents``（W08 上传，见 :mod:`babeldoc_tools.serve.routers.documents`）
  与 ``GET/PUT /api/v1/profiles``（W08，见 :mod:`babeldoc_tools.serve.routers.profiles`）
- ``GET /api/v1/documents/{did}/versions[/{revision}/pdf]``（W12 版本归档，见
  :mod:`babeldoc_tools.serve.routers.versions`）
- ``GET/PUT/DELETE /api/v1/glossary``（W13 全局词表，见
  :mod:`babeldoc_tools.serve.routers.glossary`）
- ``GET /openapi.json`` / ``GET /docs``（FastAPI 自带）
- ``GET /`` 及其下的前端静态资源（W15，见 :mod:`babeldoc_tools.serve.static`）：
  ``web/dist`` 存在时同一个端口伺候 SPA（含前端路由回退），没构建过就静默跳过

后续端点（流式段落级进度…）在 ``docs/frontend/api.md`` 里冻结形状 —— 这里不写假成功 stub。

本模块在 import 时即需要 ``fastapi``（web extra）；``bdt serve --help`` 与其它
``bdt`` 子命令都不 import 本模块，因此没有 web extra 也能用。

副作用说明：``JobRunner`` 装配时读一次 ``<store_base>/.bdt-serve/jobs/*.json``
做重启恢复（残留的 ``queued``/``running`` → ``interrupted``），``CompileService``
把残留的 ``running`` 编译状态落定为 ``failed`` —— **只有确实存在需要恢复的状态才写盘**，
新根目录下 create_app 不碰文件系统。job/草稿/编译状态是**共享实例**：job 注册表同时
给 jobs 路由、草稿路由（活动 job 期间编辑只读）与编译服务（防抖）用。
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator
from typing import Any

from fastapi import FastAPI
from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from babeldoc_tools import __version__
from babeldoc_tools.common import ToolError
from babeldoc_tools.serve.candidates import CandidateService
from babeldoc_tools.serve.compile import CompileService
from babeldoc_tools.serve.glossary import GlossaryStore
from babeldoc_tools.serve.routers.artifacts import artifacts_router
from babeldoc_tools.serve.routers.candidates import candidates_router
from babeldoc_tools.serve.routers.documents import documents_router
from babeldoc_tools.serve.routers.draft import draft_router
from babeldoc_tools.serve.routers.events import JobUpdateHub
from babeldoc_tools.serve.routers.events import events_router
from babeldoc_tools.serve.routers.glossary import glossary_router
from babeldoc_tools.serve.routers.jobs import jobs_router
from babeldoc_tools.serve.routers.profiles import profiles_router
from babeldoc_tools.serve.routers.versions import versions_router
from babeldoc_tools.serve.runner import JobRunner
from babeldoc_tools.serve.schemas import API_PREFIX
from babeldoc_tools.serve.schemas import ErrorBody
from babeldoc_tools.serve.schemas import ErrorEnvelope
from babeldoc_tools.serve.schemas import HealthResponse
from babeldoc_tools.serve.static import mount_frontend
from babeldoc_tools.serve.store import DocumentStore

__all__ = ["create_app", "error_response"]

#: ``ToolError.code`` → HTTP 状态码；未列出的一律 500。
_TOOL_ERROR_STATUS = {
    "invalid_document_id": 400,
    "path_escape": 400,
    "document_not_found": 404,
    # 产物不存在（不是空数组假成功）：parse 快照 / layout 几何 / 全部段落产物
    "snapshot_unavailable": 404,
    "geometry_unavailable": 404,
    "paragraphs_unavailable": 404,
    # 事件：没有任何 run 归档 / 指定的 run 不存在（不拿空数组冒充"没有事件"）
    "events_unavailable": 404,
    # 产物下载：不在白名单内 / 不存在 / 路径或符号链接越界（同一个码，不泄露存在性）
    "artifact_not_found": 404,
    # W12 版本归档：版本不在清单里 / 文件名不是数字 / 文件缺失（同一个码，不泄露存在性）
    "version_not_found": 404,
    # job：同文档已有活动 job（409 是可重试冲突）；未知 job / profile；未实现的 action
    "document_busy": 409,
    "job_not_found": 404,
    "unknown_profile": 422,
    "action_not_available": 422,
    "report": 422,
    # W11 候选：pid/cid 不存在是 404（不是"空列表假成功"）；已决定/还没生成完是 409
    "paragraph_not_found": 404,
    "candidate_not_found": 404,
    "candidate_decided": 409,
    "candidate_not_ready": 409,
    # W11 候选：没给 profile / profile 没配 translator 命令（后者与 unknown_profile 分开上报）
    "profile_missing": 422,
    # W09 草稿：乐观并发失败（带 detail.current_revision）/ 字段与范围不合法
    "revision_conflict": 409,
    "draft_invalid": 422,
    # W13 词表：条目不合法（空 source/target、超长、超条数）→ 422（盘上一字不改）
    "glossary_invalid": 422,
    # 客户端不得自带的命令/密钥字段（不是"参数错了"，是"这类输入不接受"）
    "forbidden_field": 422,
    # W08 上传：体积超限是 413；不是 PDF/缺文件名是 422；候选目录名全被占是 409
    "file_too_large": 413,
    "invalid_pdf": 422,
    "upload_conflict": 409,
    # W08 上传：``--workdir`` 模式只公开一个 workdir，没有"新文档"可建
    "upload_not_supported": 409,
    # W08 profiles：脚本路径引用不在白名单内/不存在（422，与 forbidden_field 分开，
    # 前者是"位置不对"，后者是"形状里带了命令字符"）
    "script_path_forbidden": 422,
    "invalid_root": 500,
    "root_missing": 503,
}

#: 通用 HTTP 状态 → 错误码（未列出的一律 ``http_error``）。
_STATUS_ERROR_CODE = {
    400: "bad_request",
    403: "forbidden",
    404: "not_found",
    405: "method_not_allowed",
    409: "conflict",
    422: "validation_error",
    500: "internal_error",
    503: "unavailable",
}


def error_response(
    code: str,
    message: str,
    *,
    status_code: int,
    detail: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
) -> JSONResponse:
    """构造统一错误信封（``detail`` 为空时不出现该键）。"""
    body = ErrorEnvelope(error=ErrorBody(code=code, message=message, detail=detail))
    return JSONResponse(
        status_code=status_code,
        content=body.model_dump(exclude_none=True),
        headers=headers,
    )


def create_app(store: DocumentStore, *, api_prefix: str = API_PREFIX) -> FastAPI:
    """组装 FastAPI 应用（工厂：不读环境变量、不起进程；恢复只改确实要改的状态）。"""
    # 共享实例：job 注册表 / 草稿锁 / 编译调度 / 候选存储（路由们用同一份，见模块 docstring）。
    # 全局词表（W13）也是共享实例：``/glossary`` 路由写它，job 启动时从它取注入路径。
    glossary = GlossaryStore(store.store_base)
    runner = JobRunner(store, glossary=glossary)
    # W14：job 状态变化 → 进程内广播（SSE 的 job_update）。订阅者订阅才有开销：
    # 没人连着事件流时 publish 直接丢弃（不积压、不落盘）。
    job_updates = JobUpdateHub()
    runner.registry.add_listener(job_updates.publish)
    compiles = CompileService(store, runner)
    # 候选服务复用 runner 的 job 注册表与候选 registry：生成是 job，采用写草稿。
    candidates = CandidateService(store, runner, compiles)

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        """服务关停：取消未触发的防抖计时器（到点没跑的编译不补跑）。

        定时器是纯内存态（不持久化）：重启后丢就丢了，下次写草稿再触发；浏览器断开
        与它无关（服务端定时器，EXECUTION.md 纠偏 7）。
        """
        yield
        compiles.shutdown()

    app = FastAPI(
        title="bdt serve",
        description="BabelDOC 文档翻译工具层的本地 HTTP 接口",
        version=__version__,
        lifespan=lifespan,
    )
    # job_update 广播挂在 app.state 上（路由层拿到的是同一个实例；也方便诊断/测试）
    app.state.job_updates = job_updates
    # 不注册 CORSMiddleware：v1 只服务 loopback 同源/开发代理，禁止任意来源跨域。

    @app.exception_handler(ToolError)
    async def _tool_error_handler(_request: Request, exc: ToolError) -> JSONResponse:
        status_code = _TOOL_ERROR_STATUS.get(exc.code, 500)
        return error_response(
            exc.code,
            exc.message,
            status_code=status_code,
            detail=dict(exc.extra) or None,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _http_error_handler(
        request: Request, exc: StarletteHTTPException
    ) -> JSONResponse:
        code = _STATUS_ERROR_CODE.get(exc.status_code, "http_error")
        message = f"{request.method} {request.url.path}: {exc.detail}"
        return error_response(
            code,
            message,
            status_code=exc.status_code,
            headers=getattr(exc, "headers", None),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error_handler(
        _request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        return error_response(
            "validation_error",
            "请求参数校验失败",
            status_code=422,
            detail={"errors": jsonable_encoder(exc.errors())},
        )

    @app.exception_handler(Exception)
    async def _unhandled_error_handler(
        _request: Request, exc: Exception
    ) -> JSONResponse:
        # 不回显 exception 文本（可能含路径/密钥），只给类型名。
        return error_response(
            "internal_error",
            "服务内部错误",
            status_code=500,
            detail={"exception": type(exc).__name__},
        )

    @app.get(
        f"{api_prefix}/health",
        response_model=HealthResponse,
        tags=["meta"],
        summary="存活探测 + 可见文档数",
        description=(
            "根目录在运行期被删/不可读时返回 503 ``root_missing``"
            "（而不是谎报 ok）。"
        ),
    )
    def health() -> HealthResponse:
        return HealthResponse(
            version=__version__,
            mode=store.mode,
            root=str(store.root),
            documents=len(store.list_dids()),
        )

    app.include_router(documents_router(store))
    app.include_router(events_router(store, job_updates))
    app.include_router(artifacts_router(store))
    app.include_router(jobs_router(store, runner, compiles))
    app.include_router(draft_router(runner, compiles))
    app.include_router(candidates_router(candidates))
    app.include_router(versions_router(store))
    app.include_router(profiles_router(store))
    app.include_router(glossary_router(glossary))
    # 前端静态资源最后挂（`/{path:path}` 接住所有未被接口认领的路径）：没跑过
    # `pnpm build` 时静默跳过，只伺服 API —— 不报错、也不改启动信封。
    mount_frontend(app)

    return app
