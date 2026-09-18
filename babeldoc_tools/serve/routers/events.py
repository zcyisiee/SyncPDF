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

W14 起 SSE **同一条流**里多了虚拟 kind ``job_update``（该文档 job 状态变化的纯通知，
见 :class:`JobUpdateHub`）：run 事件照旧从归档读，job_update 由 `create_app` 挂在
``JobRegistry`` 上的回调推 —— **不落盘**，真相仍在 ``.bdt-serve/jobs/`` 与 ``jobs.jsonl``。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections import deque
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
from babeldoc_tools.serve.jobs import JobRecord
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
    "JOB_UPDATE_BUFFER",
    "JOB_UPDATE_KIND",
    "SSE_HEARTBEAT_SECONDS",
    "SSE_POLL_SECONDS",
    "JobUpdateHub",
    "JobUpdateSubscription",
    "event_page",
    "event_stream",
    "events_router",
    "job_update_frame",
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

#: SSE 的**虚拟** kind：job 状态变化通知（W14）。不是 ``events.jsonl`` 里的 kind，
#: 也不落盘 —— 真相在 ``.bdt-serve/jobs/<jid>.json`` 与 ``jobs.jsonl``。
JOB_UPDATE_KIND = "job_update"

#: 每个 SSE 连接的 job_update 收件箱上限：满了丢最旧（通知层宁丢不积压）。
JOB_UPDATE_BUFFER = 64

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


# --------------------------------------------------------------------------- #
# job 状态变化广播（虚拟 kind `job_update`，W14）
# --------------------------------------------------------------------------- #
class JobUpdateSubscription:
    """一个 SSE 连接的 job_update 收件箱（有界 `deque`：满了自动丢最旧那条）。

    `did` 是订阅的那个文档：hub 只往同 did 的收件箱里投（job 是全局资源，事件流是
    每文档一条）。投递不阻塞（同步 `append`）：发布方在事件循环里，收件方每
    `poll_seconds` 排空一次 —— 推送延迟上限就是一个轮询间隔（0.5s），仍远快于前端
    5s 的兜底轮询。
    """

    __slots__ = ("did", "frames")

    def __init__(self, did: str, buffer: int) -> None:
        self.did = did
        self.frames: deque[str] = deque(maxlen=buffer)

    def drain(self) -> list[str]:
        """取走当前全部帧（入队顺序，最旧在前）；没有 → 空列表。"""
        frames = list(self.frames)
        self.frames.clear()
        return frames


class JobUpdateHub:
    """进程内 job 状态变化广播（SSE 专用）：**无盘写、无订阅者即丢弃**（W14）。

    - 发布方是 :meth:`~babeldoc_tools.serve.jobs.JobRegistry.add_listener` 注入的
      :meth:`publish`（`create_app` 里挂），同步调用、就在那一次状态落盘**之后**；
    - 每个 SSE 连接 :meth:`subscribe` 一个收件箱（按 did 过滤），断开时
      :meth:`unsubscribe`；
    - 没有订阅者时 :meth:`publish` 什么都不留（不积压、不需要后台任务）；有订阅者但
      某一条来不及排空时，收件箱满了丢最旧（通知层宁丢不积压）；
    - 帧的 `id` 是 ``<job_id>:<第 n 次状态变化>``，与 run 事件的 ``<run_id>:<seq>``
      是两个命名空间：:func:`parse_last_event_id` 只认 run_id 形状，所以浏览器自动
      重连时**不会**拿一个 job 命名空间去当续传游标。

    ``_counts`` 每个 job 只留一个 int（与 `JobRegistry.records` 同一量级，不另开泄漏面）。
    """

    def __init__(self, *, buffer: int = JOB_UPDATE_BUFFER) -> None:
        self.buffer = buffer
        self._subscriptions: list[JobUpdateSubscription] = []
        self._counts: dict[str, int] = {}

    @property
    def subscribers(self) -> int:
        """当前订阅数（测试与诊断用）。"""
        return len(self._subscriptions)

    def subscribe(self, did: str) -> JobUpdateSubscription:
        """订阅某个文档的 job 状态变化。"""
        subscription = JobUpdateSubscription(did, self.buffer)
        self._subscriptions.append(subscription)
        return subscription

    def unsubscribe(self, subscription: JobUpdateSubscription) -> None:
        """退订（幂等）：丢掉收件箱，之后的 :meth:`publish` 不再写到它。"""
        with contextlib.suppress(ValueError):
            self._subscriptions.remove(subscription)
        subscription.frames.clear()

    def publish(self, record: JobRecord) -> None:
        """一个 job 状态变化 → 同 did 的每个订阅者各入一帧；没有订阅者就丢掉。"""
        count = self._counts.get(record.job_id, 0) + 1
        self._counts[record.job_id] = count
        if not self._subscriptions:
            return
        frame = job_update_frame(record, count)
        for subscription in tuple(self._subscriptions):
            if subscription.did == record.did:
                subscription.frames.append(frame)


def job_update_frame(record: JobRecord, count: int) -> str:
    """一条 job_update 帧（api.md §1.4 冻结的形状）。

    ``event: job_update`` + ``id: <job_id>:<count>`` + ``data: {kind, data:{...}}``；
    ``data`` 里只有 job_id/action/status/from_stage/error_code —— 命令、信封、pid、
    路径都不进 SSE。
    """
    payload = {
        "kind": JOB_UPDATE_KIND,
        "data": {
            "job_id": record.job_id,
            "action": record.action,
            "status": record.status,
            "from_stage": record.from_stage,
            "error_code": record.error_code,
        },
    }
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {JOB_UPDATE_KIND}\nid: {record.job_id}:{count}\ndata: {data}\n\n"


async def event_stream(
    run_dir: Path,
    run_id: str,
    after_seq: int = 0,
    *,
    poll_seconds: float = SSE_POLL_SECONDS,
    heartbeat_seconds: float = SSE_HEARTBEAT_SECONDS,
    updates: JobUpdateHub | None = None,
    did: str | None = None,
) -> AsyncIterator[str]:
    """tail ``events.jsonl``：每 ``poll_seconds`` 推增量，静默 ``heartbeat_seconds`` 发心跳。

    首轮**立即**读一次存量事件（run 已结束时前端马上拿到全部事件，再转入心跳）。
    读取用 ``asyncio.to_thread``：``read_events`` 是同步 IO，不能阻塞事件循环。

    给了 ``updates`` + ``did``（W14）时把该文档的 job_update 也接进**同一条流**：订阅
    在生成器第一行才建（没人消费就不占订阅名额），生成器收尾（客户端断开）时退订。

    断开由 ``StreamingResponse`` 负责：Starlette 监听 ``http.disconnect`` 并取消本
    生成器所在任务（ASGI 2.4+ 时靠写失败抛 ``ClientDisconnect``）。这里**不**自己调
    ``request.is_disconnected()`` —— 那会与 Starlette 抢同一个 ``receive`` 通道。
    """
    subscription = (
        updates.subscribe(did) if updates is not None and did is not None else None
    )
    try:
        cursor = after_seq
        idle = 0.0
        while True:
            events = await asyncio.to_thread(_read_events, run_dir, cursor)
            pushed = subscription.drain() if subscription is not None else []
            if events or pushed:
                idle = 0.0
                for event in events:
                    cursor = max(cursor, event["seq"])
                    yield sse_frame(run_id, event, cursor)
                for frame in pushed:
                    yield frame
            else:
                idle += poll_seconds
                if idle >= heartbeat_seconds:
                    idle = 0.0
                    yield HEARTBEAT_FRAME
            await asyncio.sleep(poll_seconds)
    finally:
        if subscription is not None:
            assert updates is not None  # 订阅存在 ⇒ hub 存在（构造时就绑定了）
            updates.unsubscribe(subscription)


async def persistent_stream(store, did, after_seq=0):
    """Document-global committed cursor survives process restarts and job changes."""
    cursor = after_seq
    idle = 0.0
    while True:
        events = await asyncio.to_thread(store.database.events, did, cursor)
        for event in events:
            cursor = event["id"]
            payload = {**event, "seq": cursor, "kind": event["type"]}
            data = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
            yield f"event: {event['type']}\nid: {cursor}\ndata: {data}\n\n"
        idle = 0 if events else idle + SSE_POLL_SECONDS
        if idle >= SSE_HEARTBEAT_SECONDS:
            yield HEARTBEAT_FRAME
            idle = 0
        await asyncio.sleep(SSE_POLL_SECONDS)


# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #
def events_router(
    store: DocumentStore, updates: JobUpdateHub | None = None
) -> APIRouter:
    """按 store（+ 可选的 job_update 广播）生成事件路由（分页 + SSE）。

    ``updates`` 为 ``None`` 时 SSE 只有 run 事件（W03 行为逐帧不变）；``create_app``
    总是传一个 —— job_update 与 run 事件共用 ``/documents/{did}/events/stream``。
    """
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
            "`data: <事件 JSON>`；15s 无事件发一行 `: ping` 注释。**同一条流还带"
            "虚拟 kind `job_update`**（该文档的 job 状态变化：`event: job_update` + "
            "`id: <job_id>:<第 n 次变化>` + `data: {kind,data:{job_id,action,status,"
            "from_stage,error_code}}`，纯通知不落盘，无订阅者即丢弃；job 的真相在 "
            "`GET /jobs/{jid}` 与 `jobs.jsonl`）。断线续传用 Last-Event-ID"
            "（`<run_id>:<seq>`；job 命名空间不是 run 游标，会被忽略）或 "
            "`?after_seq=`；换 run 时必须带新的 ?run_id=。没有 run 归档 → 404 "
            "events_unavailable（错误走统一 JSON 信封，不是 SSE 帧；那时前端靠 job 轮询）"
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
        persistent: bool = False,
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
        if persistent:
            cursor = after_seq or 0
            if last_event_id and last_event_id.isdecimal():
                cursor = max(cursor, int(last_event_id))
            return StreamingResponse(
                persistent_stream(store, did, cursor),
                media_type="text/event-stream",
                headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
            )
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
            event_stream(run_dir, selected, cursor, updates=updates, did=did),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    return router
