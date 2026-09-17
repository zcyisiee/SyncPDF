"""FastAPI app factory：``bdt serve`` 的 HTTP 层（只读）。

端点：

- ``GET /api/v1/health``（W01）
- ``GET /api/v1/documents`` 及其只读子资源（W02，见
  :mod:`babeldoc_tools.serve.routers.documents`）
- ``GET /api/v1/documents/{did}/events`` + ``/events/stream``（W03，见
  :mod:`babeldoc_tools.serve.routers.events`）
- ``GET/HEAD /api/v1/documents/{did}/artifacts[/{name}]``（W03，见
  :mod:`babeldoc_tools.serve.routers.artifacts`）
- ``POST /api/v1/documents/{did}/jobs``、``GET /api/v1/jobs/{jid}``、
  ``POST /api/v1/jobs/{jid}/cancel``、``GET /api/v1/documents/{did}/jobs``
  （W07，见 :mod:`babeldoc_tools.serve.routers.jobs`）
- ``GET /openapi.json`` / ``GET /docs``（FastAPI 自带）

后续端点（草稿、版本…）在 ``docs/frontend/api.md`` 里冻结形状，由 W08+ 实现 ——
这里不写假成功 stub。

本模块在 import 时即需要 ``fastapi``（web extra）；``bdt serve --help`` 与其它
``bdt`` 子命令都不 import 本模块，因此没有 web extra 也能用。

副作用说明：``jobs_router`` 在装配时读一次 ``<store_base>/.bdt-serve/jobs/*.json``
做重启恢复（把残留的 ``queued``/``running`` 置 ``interrupted``）——
**只有确实存在需要恢复的 job 才写盘**，新根目录下 create_app 不碰文件系统。
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI
from fastapi import Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from babeldoc_tools import __version__
from babeldoc_tools.common import ToolError
from babeldoc_tools.serve.routers.artifacts import artifacts_router
from babeldoc_tools.serve.routers.documents import documents_router
from babeldoc_tools.serve.routers.events import events_router
from babeldoc_tools.serve.routers.jobs import jobs_router
from babeldoc_tools.serve.schemas import API_PREFIX
from babeldoc_tools.serve.schemas import ErrorBody
from babeldoc_tools.serve.schemas import ErrorEnvelope
from babeldoc_tools.serve.schemas import HealthResponse
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
    # job：同文档已有活动 job（409 是可重试冲突）；未知 job / profile；未实现的 action
    "document_busy": 409,
    "job_not_found": 404,
    "unknown_profile": 422,
    "action_not_available": 422,
    # 客户端不得自带的命令/密钥字段（不是"参数错了"，是"这类输入不接受"）
    "forbidden_field": 422,
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
    """组装 FastAPI 应用（工厂：不读环境变量、不起进程；job 恢复只改确实要改的快照）。"""
    app = FastAPI(
        title="bdt serve",
        description="BabelDOC 文档翻译工具层的本地只读 HTTP 接口",
        version=__version__,
    )
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
    app.include_router(events_router(store))
    app.include_router(artifacts_router(store))
    app.include_router(jobs_router(store))

    return app
