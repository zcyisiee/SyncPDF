"""``bdt cloud serve`` 的 HTTP 层（``docs/reference/cloud-api.md``）：路由只做鉴权、参数与响应形状。

业务在 :mod:`~babeldoc_tools.cloud.jobs`，翻译在 :mod:`~babeldoc_tools.cloud.runner`；
静态前端（``cloud-web/dist``）由 nginx 托管，这里只有 ``/api``。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import re
import sqlite3
import sys
import threading
from pathlib import Path
from typing import Annotated

from fastapi import Depends
from fastapi import FastAPI
from fastapi import File
from fastapi import Form
from fastapi import Request
from fastapi import Response
from fastapi import UploadFile
from fastapi.responses import FileResponse
from fastapi.responses import JSONResponse
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from babeldoc_tools import rust_backend
from babeldoc_tools.cloud import auth
from babeldoc_tools.cloud.cleanup import cleanup
from babeldoc_tools.cloud.db import Database
from babeldoc_tools.cloud.db import Paths
from babeldoc_tools.cloud.files import Duals
from babeldoc_tools.cloud.files import Previews
from babeldoc_tools.cloud.jobs import ACTIVE
from babeldoc_tools.cloud.jobs import FINISHED
from babeldoc_tools.cloud.jobs import Service
from babeldoc_tools.cloud.runner import Runner
from babeldoc_tools.cloud.uploads import save_source
from babeldoc_tools.common import ToolError

__all__ = ["create_app", "engine_sha"]

_STATUS = {
    "unauthorized": 401,
    "invalid_code": 401,
    "too_many_attempts": 429,
    "quota_exceeded": 429,
    "job_not_found": 404,
    "page_not_found": 404,
    "preview_unavailable": 404,
    "job_not_active": 409,
    "job_active": 409,
    "not_ready": 409,
    "file_too_large": 413,
    "not_pdf": 415,
    "pdf_unreadable": 422,
    "pdf_encrypted": 422,
    "too_many_pages": 422,
    "no_text_layer": 422,
    "invalid_model": 400,
    "invalid_thinking": 400,
    "invalid_request": 400,
    "dual_failed": 500,
}
_REV = re.compile(r"^\d+\.\d+$")
_HEARTBEAT = 15.0
_CLEANUP_SECONDS = 3600
#: 预览 key 已含内容版本（sha / attempt.revision），浏览器可长期缓存。
_IMMUTABLE = {"Cache-Control": "private, max-age=31536000, immutable"}


def engine_sha(engine: str | None) -> str:
    """缓存键里的引擎版本：二进制本身的 sha256（crate 版本号不随改动变化）。"""
    binary = Path(engine).expanduser().resolve() if engine else rust_backend._ENGINE
    try:
        with binary.open("rb") as handle:
            return hashlib.file_digest(handle, "sha256").hexdigest()
    except OSError as exc:
        raise ToolError("engine_missing", f"Rust 引擎不可读：{binary}") from exc


class LoginBody(BaseModel):
    code: str


def _current_user(request: Request) -> sqlite3.Row:
    user = auth.user_for_token(request.app.state.db, request.cookies.get(auth.COOKIE))
    if user is None:
        raise ToolError("unauthorized", "请先用邀请码登录")
    return user


User = Annotated[sqlite3.Row, Depends(_current_user)]


def _client_ip(request: Request) -> str:
    # 只监听 loopback、由 nginx 反代：X-Real-IP 由 nginx 写入。
    return request.headers.get("x-real-ip") or (request.client.host if request.client else "")


def create_app(
    root: Path,
    *,
    engine: str | None = None,
    translator: str = "pi",
    layout_device: str = "cpu",
    run_worker: bool = True,
) -> FastAPI:
    paths = Paths(Path(root)).ensure()
    db = Database(paths.db)
    service = Service(db, paths, engine_sha(engine))
    runner = Runner(service, paths, engine=engine, translator=translator, layout_device=layout_device)
    previews = Previews(paths)
    duals = Duals(paths, engine)
    limiter = auth.LoginLimiter()
    stop = threading.Event()

    def periodic_cleanup() -> None:
        while True:
            try:
                sys.stderr.write(f"bdt cloud: cleanup {json.dumps(cleanup(db, paths))}\n")
            except Exception as exc:  # noqa: BLE001 - 清理失败不影响服务
                sys.stderr.write(f"bdt cloud: cleanup 出错：{exc!r}\n")
            if stop.wait(_CLEANUP_SECONDS):
                return

    @contextlib.asynccontextmanager
    async def lifespan(_app: FastAPI):
        if run_worker:
            runner.start()
        threading.Thread(target=periodic_cleanup, name="bdt-cloud-cleanup", daemon=True).start()
        try:
            yield
        finally:
            stop.set()
            if run_worker:
                await asyncio.to_thread(runner.stop)
            db.close()

    app = FastAPI(title="bdt cloud", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.db = db
    app.state.service = service
    app.state.runner = runner
    app.state.paths = paths

    @app.exception_handler(ToolError)
    async def tool_error(_request: Request, exc: ToolError) -> JSONResponse:
        return JSONResponse(
            {"error": {"code": exc.code, "message": exc.message}},
            status_code=_STATUS.get(exc.code, 400),
        )

    def owned(user, job_id: str):
        job = service.get_job(user["id"], job_id)
        if job is None:
            raise ToolError("job_not_found", "找不到这条记录")
        return job

    # ---- 账户 -------------------------------------------------------------- #
    @app.post("/api/login")
    def login(body: LoginBody, request: Request, response: Response) -> dict:
        ip = _client_ip(request)
        if limiter.blocked(ip):
            raise ToolError("too_many_attempts", "尝试次数过多，请 10 分钟后再试")
        token = auth.login(db, body.code)
        if token is None:
            limiter.fail(ip)
            raise ToolError("invalid_code", "邀请码无效")
        response.set_cookie(
            auth.COOKIE, token, max_age=auth.SESSION_SECONDS, httponly=True, samesite="lax", path="/"
        )
        return {"ok": True}

    @app.post("/api/logout")
    def logout(request: Request, response: Response) -> dict:
        token = request.cookies.get(auth.COOKIE)
        if token:
            auth.logout(db, token)
        response.delete_cookie(auth.COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/me")
    def me(user: User) -> dict:
        used = service.quota_used(user["id"])
        return {
            "name": user["name"],
            "code": auth.mask_code(user["invite_code"]),
            "daily_quota": user["daily_quota"],
            "used": used,
            "remaining": max(0, user["daily_quota"] - used),
        }

    # ---- 任务 -------------------------------------------------------------- #
    @app.get("/api/jobs")
    def list_jobs(user: User) -> dict:
        return {"items": service.list_jobs(user["id"])}

    @app.post("/api/jobs", status_code=201)
    def create_job(
        user: User,
        file: Annotated[UploadFile, File()],
        model: Annotated[str, Form()],
        thinking: Annotated[str, Form()],
    ) -> dict:
        service.check_options(model, thinking)
        sha, size, page_sizes = save_source(paths, file.file)
        service.add_source(sha, size, page_sizes)
        filename = Path(file.filename or "paper.pdf").name[:200]
        job_id = service.create_job(user, filename, sha, model, thinking)
        return service.job_view(owned(user, job_id))

    @app.get("/api/jobs/{job_id}")
    def get_job(user: User, job_id: str) -> dict:
        return service.job_view(owned(user, job_id))

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(user: User, job_id: str) -> dict:
        service.cancel_job(user["id"], job_id)
        return service.job_view(owned(user, job_id))

    @app.delete("/api/jobs/{job_id}", status_code=204)
    def delete_job(user: User, job_id: str) -> Response:
        service.delete_job(user["id"], job_id)
        return Response(status_code=204)

    @app.get("/api/jobs/{job_id}/events")
    async def job_events(user: User, job_id: str, request: Request) -> StreamingResponse:
        owned(user, job_id)
        header = request.headers.get("last-event-id") or request.query_params.get("after") or "0"
        try:
            after = int(header)
        except ValueError as exc:
            raise ToolError("invalid_request", "Last-Event-ID 必须是整数") from exc

        async def stream():
            token = service.hub.subscribe()
            seq, queue_sent = after, None
            try:
                while True:
                    token[1].clear()
                    for event in service.events_after(job_id, seq):
                        seq = event.pop("seq")
                        kind = event.pop("kind")
                        yield f"id: {seq}\nevent: {kind}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                    job = service.get_job(user["id"], job_id)
                    if job is None:
                        return
                    queue = service.queue_info(job["translation_id"]) if job["status"] == "queued" else None
                    # 离开排队由 status 事件表达；queue 只在仍排队时推变化
                    if queue is not None and queue != queue_sent:
                        queue_sent = queue
                        yield f"event: queue\ndata: {json.dumps(queue)}\n\n"
                    if job["status"] not in ACTIVE:
                        yield "event: end\ndata: {}\n\n"
                        return
                    if await request.is_disconnected():
                        return
                    try:
                        await asyncio.wait_for(token[1].wait(), _HEARTBEAT)
                    except TimeoutError:
                        yield ": ping\n\n"
            finally:
                service.hub.unsubscribe(token)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/jobs/{job_id}/pages/{page}.webp")
    def page_preview(user: User, job_id: str, page: int, v: str = "src", rev: str | None = None) -> FileResponse:
        job = owned(user, job_id)
        if not 1 <= page <= job["pages"]:
            raise ToolError("page_not_found", f"没有第 {page} 页")
        tid = job["translation_id"]
        if v == "src":
            pdf, key = paths.source(job["source_sha"]), job["source_sha"]
        elif v == "tr" and job["tr_status"] in FINISHED:
            pdf, key = paths.translated(tid), job["translated_sha"]
        elif v == "tr" and job["tr_status"] == "running" and rev and _REV.match(rev):
            pdf, key = paths.work / tid / "translated.pdf", f"{tid}-{rev}"
        elif v == "tr":
            raise ToolError("preview_unavailable", "译文还没有生成")
        else:
            raise ToolError("invalid_request", "v 只能是 src 或 tr")
        return FileResponse(previews.page(pdf, key, page), media_type="image/webp", headers=_IMMUTABLE)

    @app.get("/api/jobs/{job_id}/download")
    def download(user: User, job_id: str, kind: str = "translated") -> FileResponse:
        job = owned(user, job_id)
        if job["tr_status"] not in FINISHED:
            raise ToolError("not_ready", "译文还没有生成")
        tid = job["translation_id"]
        stem = Path(job["filename"]).stem
        if kind == "translated":
            path, name = paths.translated(tid), f"{stem}-中文.pdf"
        elif kind == "dual":
            path = duals.get(tid, paths.source(job["source_sha"]), paths.translated(tid))
            name = f"{stem}-中英对照.pdf"
        else:
            raise ToolError("invalid_request", "kind 只能是 translated 或 dual")
        return FileResponse(path, media_type="application/pdf", filename=name)

    return app
