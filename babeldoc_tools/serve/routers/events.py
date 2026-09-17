"""``/documents/{did}/events``：事件分页 + SSE 实时流（api.md §1.3 / §1.4）。

事件源是 ``debug/runs/<run_id>/events.jsonl``，**解析一律复用**
:func:`babeldoc.debug_recorder.read_events`（半行不发布、损坏行跳过、按 ``seq``
前缀廉价续读），本模块不重写解析。

两条硬规则来自契约：

- **游标是扫描位置不是匹配位置**：``next_after_seq`` 取本页扫过的最大 ``seq``，
  带 ``stage``/``kind`` 过滤时即使一条都没匹配上也要推进，否则前端轮询会被
  过滤条件卡死；
- **游标语义是 ``(run_id, seq)``**：``seq`` 只在单 run 内递增，所以分页响应带
  ``run_id``，SSE 的 ``id`` 是 ``<run_id>:<seq>``，``Last-Event-ID`` 也按这个形状续传。

没有 ``debug/runs``（或空）→ 404 ``events_unavailable``：不拿空数组冒充"没有事件"。
run 存在但 ``events.jsonl`` 还没出现（刚启动）→ 200 + 空页，这是真实的"还没有事件"。
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated
from typing import Any

from babeldoc.debug_recorder import read_events
from fastapi import APIRouter
from fastapi import Header
from fastapi import Path as PathParam
from fastapi import Query
from fastapi.responses import StreamingResponse

from babeldoc_tools.common import ToolError
from babeldoc_tools.serve.routers.documents import DOCUMENT_ID
from babeldoc_tools.serve.schemas import API_PREFIX
from babeldoc_tools.serve.schemas import EventsPage
from babeldoc_tools.serve.store import DocumentStore
from babeldoc_tools.serve.workdir import RUN_ID_RE
from babeldoc_tools.serve.workdir import RUNS_DIR
from babeldoc_tools.serve.workdir import list_run_ids

__all__ = [
    "EVENTS_DEFAULT_LIMIT",
    "EVENTS_LIMIT_MAX",
    "HEARTBEAT_FRAME",
    "SSE_HEARTBEAT_SECONDS",
    "SSE_POLL_SECONDS",
    "event_page",
    "event_stream",
    "events_router",
    "list_run_ids",
    "parse_last_event_id",
    "select_run",
    "sse_frame",
]

#: 分页默认/上限（api.md §1.3 只冻结了参数名；单 run 真实体量约 4k 条，500 一页够用）。
EVENTS_DEFAULT_LIMIT = 500
EVENTS_LIMIT_MAX = 2000

#: SSE tail 间隔与静默心跳间隔（防代理/浏览器把长静默连接掐掉）。
SSE_POLL_SECONDS = 0.5
SSE_HEARTBEAT_SECONDS = 15.0

#: 心跳帧：SSE 注释行（客户端忽略，但连接保持活跃）。
HEARTBEAT_FRAME = ": ping\n\n"

#: 事件缺 ``kind`` 时的 SSE 事件名（SSE 默认类型）。
_DEFAULT_EVENT_NAME = "message"

#: ``stage``/``kind`` 过滤参数说明。
STAGE_QUERY = "只返回该阶段的事件（过滤不影响游标推进）"
KIND_QUERY = "只返回该 kind 的事件（过滤不影响游标推进）"
AFTER_SEQ_QUERY = "扫描起点：只返回 seq 大于它的事件（同一 run 内有意义）"
RUN_ID_QUERY = "run id（默认最新 run）；形状必须是 <UTC时间戳>Z-<6位十六进制>"
LIMIT_QUERY = (
    f"本页最多扫描多少条事件（默认 {EVENTS_DEFAULT_LIMIT}，上限 {EVENTS_LIMIT_MAX}）"
)


# --------------------------------------------------------------------------- #
# 分页
# --------------------------------------------------------------------------- #
def select_run(workdir: Path | str, run_id: str | None) -> tuple[str, Path]:
    """选 run：``(run_id, run_dir)``；没有 run 或指定的 run 不存在 → 404 语义。"""
    runs = list_run_ids(workdir)
    if run_id is None:
        if not runs:
            raise ToolError(
                "events_unavailable",
                f"文档 {Path(workdir).name!r} 没有任何 run 归档（debug/runs 为空或不存在）",
                did=Path(workdir).name,
            )
        run_id = runs[0]
    elif run_id not in runs:
        raise ToolError(
            "events_unavailable",
            f"run 不存在：{run_id}",
            run_id=run_id,
        )
    return run_id, Path(workdir) / RUNS_DIR / run_id


def event_page(
    workdir: Path | str,
    *,
    run_id: str | None = None,
    after_seq: int = 0,
    limit: int = EVENTS_DEFAULT_LIMIT,
    stage: str | None = None,
    kind: str | None = None,
) -> EventsPage:
    """读一页事件（默认最新 run）。过滤只影响本页内容，不影响游标推进。"""
    selected, run_dir = select_run(workdir, run_id)
    scanned = _read_events(run_dir, after_seq)
    window = scanned[:limit]
    position = _scan_position(window)
    return EventsPage(
        run_id=selected,
        events=[event for event in window if _matches(event, stage=stage, kind=kind)],
        next_after_seq=position if position is not None else after_seq,
        has_more=len(scanned) > limit,
    )


def _read_events(run_dir: Path, after_seq: int) -> list[dict]:
    """``read_events`` 的容错包装：归档在轮询间隔里被删 → 空页（不 500）。"""
    try:
        events = read_events(run_dir, after_seq)
    except OSError:
        return []
    # 没有整数 seq 的事件无法定位游标（每次读都会被重新返回），不发布。
    return [event for event in events if isinstance(event.get("seq"), int)]


def _scan_position(window: list[dict]) -> int | None:
    """本页扫过的最大 ``seq``（= 下次的 ``after_seq``）；一条都没有 → ``None``。"""
    positions = [event["seq"] for event in window if isinstance(event.get("seq"), int)]
    return max(positions) if positions else None


def _matches(event: Mapping[str, Any], *, stage: str | None, kind: str | None) -> bool:
    """``stage``/``kind`` 过滤（``None`` = 不过滤）。"""
    if stage is not None and event.get("stage") != stage:
        return False
    return kind is None or event.get("kind") == kind


# --------------------------------------------------------------------------- #
# SSE
# --------------------------------------------------------------------------- #
def sse_frame(run_id: str, event: Mapping[str, Any], seq: int) -> str:
    """单条 SSE 帧：``event: <kind>`` + ``id: <run_id>:<seq>`` + ``data: <json>``。

    ``data`` 是事件对象的紧凑 JSON 原样透传（前端展开原始 JSON 用）；``json.dumps``
    会把换行转义成 ``\\n``，所以一条事件永远只占一行 ``data:``。
    """
    kind = event.get("kind")
    name = kind if isinstance(kind, str) and kind else _DEFAULT_EVENT_NAME
    data = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    return f"event: {name}\nid: {run_id}:{seq}\ndata: {data}\n\n"


def parse_last_event_id(value: str | None) -> tuple[str, int] | None:
    """``Last-Event-ID`` = ``<run_id>:<seq>`` → ``(run_id, seq)``；不合法 → ``None``。"""
    if not value:
        return None
    run_id, separator, raw_seq = value.rpartition(":")
    if not separator or not RUN_ID_RE.match(run_id):
        return None
    try:
        seq = int(raw_seq)
    except ValueError:
        return None
    return (run_id, seq) if seq >= 0 else None


async def event_stream(
    run_dir: Path,
    run_id: str,
    after_seq: int = 0,
    *,
    poll_seconds: float = SSE_POLL_SECONDS,
    heartbeat_seconds: float = SSE_HEARTBEAT_SECONDS,
) -> AsyncIterator[str]:
    """tail ``events.jsonl``：每 ``poll_seconds`` 推增量，静默 ``heartbeat_seconds`` 发心跳。

    首轮**立即**读一次存量事件（run 已结束时前端马上拿到全部事件，再转入心跳）。
    读取用 ``asyncio.to_thread``：``read_events`` 是同步 IO，不能阻塞事件循环。

    断开由 ``StreamingResponse`` 负责：Starlette 监听 ``http.disconnect`` 并取消本
    生成器所在任务（ASGI 2.4+ 时靠写失败抛 ``ClientDisconnect``）。这里**不**自己调
    ``request.is_disconnected()`` —— 那会与 Starlette 抢同一个 ``receive`` 通道。
    """
    cursor = after_seq
    idle = 0.0
    while True:
        events = await asyncio.to_thread(_read_events, run_dir, cursor)
        if events:
            idle = 0.0
            for event in events:
                cursor = max(cursor, event["seq"])
                yield sse_frame(run_id, event, cursor)
        else:
            idle += poll_seconds
            if idle >= heartbeat_seconds:
                idle = 0.0
                yield HEARTBEAT_FRAME
        await asyncio.sleep(poll_seconds)


# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #
def events_router(store: DocumentStore) -> APIRouter:
    """按 store 生成事件路由（分页 + SSE）。"""
    router = APIRouter(prefix=API_PREFIX, tags=["documents"])

    @router.get(
        "/documents/{did}/events",
        response_model=EventsPage,
        summary="事件分页",
        description=(
            "读 events.jsonl 的一页（默认最新 run，?run_id= 可指定）。next_after_seq 是"
            "**扫描位置**：即使 stage/kind 过滤后本页 0 条也会推进。文件尾部的半行不发布，"
            "损坏行跳过。没有任何 run 归档 → 404 events_unavailable。"
        ),
    )
    def get_events(
        did: Annotated[str, PathParam(description=DOCUMENT_ID)],
        after_seq: Annotated[int, Query(ge=0, description=AFTER_SEQ_QUERY)] = 0,
        limit: Annotated[
            int, Query(ge=1, le=EVENTS_LIMIT_MAX, description=LIMIT_QUERY)
        ] = EVENTS_DEFAULT_LIMIT,
        stage: Annotated[str | None, Query(description=STAGE_QUERY)] = None,
        kind: Annotated[str | None, Query(description=KIND_QUERY)] = None,
        run_id: Annotated[
            str | None, Query(pattern=RUN_ID_RE.pattern, description=RUN_ID_QUERY)
        ] = None,
    ) -> EventsPage:
        return event_page(
            store.resolve(did),
            run_id=run_id,
            after_seq=after_seq,
            limit=limit,
            stage=stage,
            kind=kind,
        )

    @router.get(
        "/documents/{did}/events/stream",
        response_class=StreamingResponse,
        summary="事件实时流（SSE）",
        description=(
            "text/event-stream：每条形如 `event: <kind>` / `id: <run_id>:<seq>` / "
            "`data: <事件 JSON>`；15s 无事件发一行 `: ping` 注释。断线续传用 "
            "Last-Event-ID（`<run_id>:<seq>`）或 `?after_seq=`；换 run 时必须带新的 "
            "?run_id=。没有 run 归档 → 404 events_unavailable（错误走统一 JSON 信封，"
            "不是 SSE 帧）。"
        ),
        responses={
            200: {
                "description": "SSE 帧流（event / id / data 三行 + 空行）",
                "content": {"text/event-stream": {}},
            }
        },
    )
    async def stream_events(
        did: Annotated[str, PathParam(description=DOCUMENT_ID)],
        after_seq: Annotated[
            int | None, Query(ge=0, description=AFTER_SEQ_QUERY)
        ] = None,
        run_id: Annotated[
            str | None, Query(pattern=RUN_ID_RE.pattern, description=RUN_ID_QUERY)
        ] = None,
        last_event_id: Annotated[
            str | None,
            Header(alias="Last-Event-ID", description="<run_id>:<seq>，用于断线续传"),
        ] = None,
    ) -> StreamingResponse:
        workdir = store.resolve(did)
        resume = parse_last_event_id(last_event_id)
        resume_run_id, resume_seq = resume if resume is not None else (None, 0)
        # 显式 ?run_id= 优先；否则用 Last-Event-ID 里那个（还在 → 续传，没了 → 最新 run）。
        candidate = run_id
        if candidate is None and resume_run_id in list_run_ids(workdir):
            candidate = resume_run_id
        selected, run_dir = select_run(workdir, candidate)
        # 游标只在同一个 run 内有意义：换了 run 就从 0 开始。
        cursor = after_seq
        if cursor is None:
            cursor = resume_seq if selected == resume_run_id else 0
        return StreamingResponse(
            event_stream(run_dir, selected, cursor),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    return router
