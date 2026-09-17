"""job 的持久化、准入控制与重启恢复（``docs/frontend/api.md`` §3.4，W07）。

本模块只管**状态**：job 快照、生命周期事件、同文档串行与全局并发上限。真正起
子进程、取消进程组的事在 :mod:`babeldoc_tools.serve.runner`。

持久化布局（``store_base`` 由 :attr:`babeldoc_tools.serve.store.DocumentStore.store_base`
给出：``--root`` 模式 = 服务根目录，``--workdir`` 模式 = 那个 workdir）::

    <store_base>/.bdt-serve/jobs.jsonl          # append-only 生命周期事件
    <store_base>/.bdt-serve/jobs/<jid>.json     # 当前状态快照（tmp + rename 原子写）

状态机（``api.md`` §3.4）::

    queued ──start──> running ──退出码 0 + ok 信封──> succeeded
       │                 │──非 0 / 无信封───────────> failed
       │                 │──cancel──────────────────> canceled
       └──cancel────────> canceled
       └──(服务重启)────> interrupted
                         running ──(重启/身份不符)──> interrupted

``action=compile``（W09）只对**终态判定**有一处特例：编译成功的定义是"隔离副本里
apply+build 成功并发布了新 PDF"（见 :mod:`babeldoc_tools.serve.compile`），而
``bdt run --from apply`` 之后的 check/review 是质量门禁 —— 门禁不过时子进程 exit 1，
但编译其实成功了。那种情况 job 记 ``succeeded``（信封原样保留门禁结论）；
build 没成功则按普通的退出码/信封规则记 ``failed``。

两条**不可妥协**的规则（EXECUTION.md 纠偏第 5 条）：

- **不自动重跑收费调用**：重启后 ``queued`` 一律置 ``interrupted``（前端显式重试 =
  发新 job），``running`` 只在确认进程身份时才保留；
- **不凭孤立 pid 发信号**：进程身份 = ``boot_id``（起 job 的 serve 进程 pid + 启动
  时刻）+ ``spawn_marker``（服务端构造的 argv 的 sha256 前缀）。**argv 本身不落盘** ——
  它含 profile 里的命令字符串，可能内嵌密钥；只存指纹用于核对。

信封（``JobRecord.envelope``）落盘前也过一道脱敏（
:func:`babeldoc_tools.serve.runner.sanitize_envelope`）：命令字符串换成 profile id、
带 token 的 debug 查看器 URL 移除 —— ``GET /jobs/{jid}`` 返回的东西里没有密钥。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import threading
import time
from datetime import datetime
from datetime import timezone
from pathlib import Path
from typing import Any
from typing import Literal

from pydantic import BaseModel
from pydantic import ValidationError

from babeldoc_tools.serve.store import STATE_DIR

__all__ = [
    "ACTIVE_STATUSES",
    "EVENT_CANCELED",
    "EVENT_FAILED",
    "EVENT_FINISHED",
    "EVENT_INTERRUPTED",
    "EVENT_QUEUED",
    "EVENT_STARTED",
    "EVENTS_FILE",
    "JOBS_DIR",
    "JOB_ID_PREFIX",
    "MAX_CONCURRENT_JOBS",
    "RESTART_REASON",
    "TERMINAL_STATUSES",
    "JobRecord",
    "JobRegistry",
    "new_boot_id",
    "new_job_id",
    "pid_alive",
    "process_group_id",
    "utc_now",
]

#: ``<store_base>/.bdt-serve/jobs.jsonl``（append-only 生命周期事件）。
EVENTS_FILE = "jobs.jsonl"
#: ``<store_base>/.bdt-serve/jobs/<jid>.json``（当前状态快照）。
JOBS_DIR = "jobs"

#: job id 前缀（如 ``j_01J8Z...``）：只有一个来源，路由/测试/日志都用它。
JOB_ID_PREFIX = "j_"

#: 状态集合（``api.md`` §3.4 冻结）：``queued``/``running`` 是活动态。
ACTIVE_STATUSES: tuple[str, ...] = ("queued", "running")
TERMINAL_STATUSES: tuple[str, ...] = (
    "succeeded",
    "failed",
    "canceled",
    "interrupted",
)

#: 全局并发上限（api.md §3.4：跨文档并发但全局限流）；超出的 job 停在 ``queued``。
MAX_CONCURRENT_JOBS = 2

#: 生命周期事件名（api.md §1.4 的 job 事件；持久化在 jobs.jsonl，W08 决定 SSE 形状）。
EVENT_QUEUED = "job_queued"
EVENT_STARTED = "job_started"
EVENT_FINISHED = "job_finished"
EVENT_FAILED = "job_failed"
EVENT_CANCELED = "job_canceled"
EVENT_INTERRUPTED = "job_interrupted"

#: 重启恢复的原因标记（``JobRecord.interrupted_reason``）。
RESTART_REASON = "server_restart"

#: ULID 风格 id 的编码表（Crockford base32：无 I/L/O/U，字典序 = 时间序）。
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
#: ``j_`` + 26 字符 = 128 位（48 位毫秒时间戳 + 80 位随机）。
_JOB_ID_LENGTH = 26

#: 发号锁 + 上一次的时间戳毫秒：同进程内保证 id 严格单调。
_id_lock = threading.Lock()
_last_ms = 0


def utc_now() -> str:
    """当前 UTC 时间：ISO8601 毫秒 + ``Z``（``api.md`` §1 的全站时间形状）。"""
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z")
    )


def new_boot_id(pid: int | None = None) -> str:
    """本 serve 进程的身份：``<pid>-<启动毫秒>``。

    pid 会被复用，启动毫秒不会：两者一起构成"这个进程实例"的指纹，重启后必然不同。
    """
    return f"{os.getpid() if pid is None else pid}-{int(time.time() * 1000)}"


def new_job_id(now_ms: int | None = None, entropy: bytes | None = None) -> str:
    """``j_`` + 26 字符 ULID 风格 id：48 位毫秒时间戳 + 80 位随机，字典序 = 时间序。

    标准库实现（``time`` + ``os.urandom``），不引第三方依赖。同进程内保证严格单调：
    同一毫秒内的多次发号靠抬高时间戳位保持有序（旧的 id 永远小于新的 id）。
    """
    global _last_ms
    ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    rand = os.urandom(10) if entropy is None else entropy
    with _id_lock:
        if ms <= _last_ms:
            ms = _last_ms + 1
        _last_ms = ms
    value = (ms << 80) | int.from_bytes(rand, "big")
    chars = []
    for _ in range(_JOB_ID_LENGTH):
        chars.append(_CROCKFORD[value & 0x1F])
        value >>= 5
    return JOB_ID_PREFIX + "".join(reversed(chars))


def pid_alive(pid: int | None) -> bool:
    """``os.kill(pid, 0)`` 探测：pid 存在且可被我们发信号。

    只回答"这个 pid 现在有进程吗"，**不**回答"它是我们的 job 吗" —— 后者要靠
    :attr:`JobRecord.boot_id`（见 :meth:`JobRegistry.load`）。
    """
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # 存在但不是我们的进程
    except OSError:
        return False
    return True


def process_group_id(pid: int | None) -> int | None:
    """pid 所属进程组 id（``os.getpgid``）；进程已消失/无权限 → ``None``。"""
    if not pid or pid <= 0:
        return None
    try:
        return os.getpgid(pid)
    except OSError:
        return None


class JobRecord(BaseModel):
    """一个 job 的持久化快照；**同时**是 ``GET /jobs/{jid}`` 的响应体。

    一份模型两处用（快照文件 = 响应体），避免"存储字段"与"响应字段"两套字段漂移。
    ``pid``/``pgid`` 只在运行中有值（终态清空：不再持有进程身份）；``boot_id`` 与
    ``spawn_marker`` 是重启恢复用的身份指纹，不是命令原文。
    """

    job_id: str
    did: str
    #: W07 实现 ``run``（``bdt run --from <stage>``）与 ``check``（``--from check``）；
    #: W09 加 ``compile``（草稿编译：隔离副本里 apply+build，见
    #: :mod:`babeldoc_tools.serve.compile`）。``retranslate`` 仍是 W11，路由层 422。
    action: Literal["run", "retranslate", "compile", "check"]
    status: Literal[
        "queued", "running", "succeeded", "failed", "canceled", "interrupted"
    ] = "queued"
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    #: 实际生效的起点阶段（``run`` 的 ``--from``；``check`` 固定 ``check``；
    #: ``compile`` 固定 ``apply``）。
    from_stage: str | None = None
    #: profile **id**（命令字符串永不进记录、永不回客户端）；``compile`` 不需要
    #: provider → ``None``（它只跑 apply+build，不调翻译/审查）。
    profile: str | None = None
    #: ``--pages`` 原样透传（只对 ``run`` 有效）。
    pages: str | None = None
    dual: bool = False
    #: ``POST /documents/{did}/jobs`` 的 ``use_glossary``（W13，缺省 true）实际落成的值：
    #: 只有 ``action=run`` 才是 true（词表只约束翻译阶段）。它只是**开关**；词表内容与
    #: 注入用的文件路径全在服务端，客户端看不到也不传。
    use_glossary: bool = False
    #: ``compile`` 的页级语义（api.md §3.4）：请求的 scope / 实际生效的 scope /
    #: 回退原因。v1 页级编译回退全量，所以 ``requested_scope="pages"`` 时
    #: ``effective_scope="full"`` 且 ``downgrade_reason`` 非空。
    requested_scope: str | None = None
    effective_scope: str | None = None
    downgrade_reason: str | None = None
    #: 这次 job 的**触发原因**（只有 ``compile`` 有值，W12）：``debounce`` = 草稿保存后
    #: 服务端防抖自动编译，``manual`` = 显式 ``POST /jobs {action:"compile"}``；其它 action
    #: （以及重启恢复留下的历史记录）为 ``None``。版本归档用它记 ``trigger``（api.md §3.7）。
    trigger: Literal["debounce", "manual"] | None = None
    #: ``retranslate``（W11）的段落 id 与候选 id：job 与候选一一对应（其它 action 为
    #: ``None``）。候选行在提交时就建好了，这两个字段是 job 侧的回链（前端从 job 找到
    #: 候选、或从候选找到 job 都不需要额外请求）。
    paragraph_id: str | None = None
    candidate_id: str | None = None
    #: 子进程 debug recorder 建的 run（spawn 之后新出现的那个）；没有 → None。
    run_id: str | None = None
    exit_code: int | None = None
    #: 子进程 stdout 的收尾信封原文（截断到
    #: :data:`~babeldoc_tools.serve.runner.MAX_ENVELOPE_BYTES`）。
    #: **已脱敏**（W08）：``data.config.translator/reviewer`` 的命令字符串换成 profile id、
    #: 带 token 的 ``data.debug.url`` 移除 —— 落盘/回传的都是这一份。
    envelope: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    #: 运行中的进程/进程组（终态清空）。
    pid: int | None = None
    pgid: int | None = None
    cancel_requested_at: str | None = None
    #: 起这个 job 的 serve 进程身份；重启后必然不同。
    boot_id: str | None = None
    #: 服务端构造的 argv 的 sha256 前缀（诊断 + 身份核对，不含命令原文）。
    spawn_marker: str | None = None
    #: ``interrupted`` 的原因（目前只有 ``server_restart``）。
    interrupted_reason: str | None = None


#: 终态 → 生命周期事件名。
_TERMINAL_EVENTS = {
    "succeeded": EVENT_FINISHED,
    "failed": EVENT_FAILED,
    "canceled": EVENT_CANCELED,
    "interrupted": EVENT_INTERRUPTED,
}


class JobRegistry:
    """job 快照 + 生命周期事件 + 准入表（一个 ``bdt serve`` 一个实例）。

    ``lock`` 是**调度准入锁**：提交（路由）与 pump（runner 启动下一个）共用它，使
    "读活动表 → 入队/启动"跨 await 原子 —— 否则两个并发 POST 会同时判定槽位空闲。
    状态变更只在事件循环里发生（本模块没有自己的线程）。
    """

    def __init__(
        self,
        store_base: Path | str,
        *,
        boot_id: str | None = None,
        max_concurrent: int = MAX_CONCURRENT_JOBS,
    ) -> None:
        self.store_base = Path(store_base)
        self.boot_id = boot_id or new_boot_id()
        self.max_concurrent = max_concurrent
        #: 全部 job 快照（含终态；``GET /documents/{did}/jobs`` 直接读它）。
        self.records: dict[str, JobRecord] = {}
        #: 调度准入锁（见类 docstring）。
        self.lock = asyncio.Lock()
        #: 排队中的 job id，FIFO。
        self._queue: list[str] = []

    # ---------------------------------------------------------------- 路径
    @property
    def base_dir(self) -> Path:
        """``<store_base>/.bdt-serve/``（本服务自己的状态目录）。"""
        return self.store_base / STATE_DIR

    @property
    def jobs_dir(self) -> Path:
        """``<store_base>/.bdt-serve/jobs/``（一个 job 一个快照文件）。"""
        return self.base_dir / JOBS_DIR

    @property
    def events_path(self) -> Path:
        """``<store_base>/.bdt-serve/jobs.jsonl``（append-only 生命周期事件）。"""
        return self.base_dir / EVENTS_FILE

    def snapshot_path(self, job_id: str) -> Path:
        """单个 job 的快照路径。"""
        return self.jobs_dir / f"{job_id}.json"

    # ---------------------------------------------------------------- 恢复
    def load(self) -> list[JobRecord]:
        """读回全部快照并按身份核对活动 job；返回被改写的记录（可能为空）。

        - ``queued`` → ``interrupted``：服务重启不自动重跑（收费调用要人显式重试）；
        - ``running`` 且身份不符（``boot_id`` 不是本进程，或 pid 已消失，或进程组对不上）
          → ``interrupted``，**不发任何信号**：孤立 pid 可能已经是别人的进程。

        坏快照（不可解析/字段不符）跳过：一个坏文件不该让服务起不来。
        """
        recovered: list[JobRecord] = []
        for path in self.jobs_dir.glob(f"{JOB_ID_PREFIX}*.json"):
            record = _read_snapshot(path)
            if record is not None:
                self.records[record.job_id] = record
        for record in sorted(self.records.values(), key=lambda item: item.job_id):
            if record.status == "queued" or (
                record.status == "running" and not self._owns_running(record)
            ):
                recovered.append(self._interrupt(record, reason=RESTART_REASON))
        return recovered

    def _owns_running(self, record: JobRecord) -> bool:
        """这个 ``running`` 记录确实是**本进程**起的那一个吗（boot_id + pid + 进程组）。"""
        if record.boot_id != self.boot_id or not pid_alive(record.pid):
            return False
        if record.pgid is None:
            return True
        return process_group_id(record.pid) == record.pgid

    # ---------------------------------------------------------------- 查询
    def get(self, job_id: str) -> JobRecord | None:
        """按 id 取快照；没有 → ``None``。"""
        return self.records.get(job_id)

    def list_for_did(self, did: str, *, status: str | None = None) -> list[JobRecord]:
        """某文档的 job，新 → 旧（job id 单调 ⇒ 倒序即时间倒序）；可按状态过滤。"""
        records = [
            record
            for record in self.records.values()
            if record.did == did and (status is None or record.status == status)
        ]
        return sorted(records, key=lambda item: item.job_id, reverse=True)

    def active_for_did(self, did: str) -> JobRecord | None:
        """该文档的活动 job（``queued`` 或 ``running``）；同文档最多 1 个。"""
        for record in self.records.values():
            if record.did == did and record.status in ACTIVE_STATUSES:
                return record
        return None

    # ------------------------------------------------------------ 状态流转
    def create(
        self,
        *,
        did: str,
        action: str,
        from_stage: str | None,
        profile: str | None,
        pages: str | None = None,
        dual: bool = False,
        requested_scope: str | None = None,
        effective_scope: str | None = None,
        downgrade_reason: str | None = None,
        trigger: str | None = None,
        paragraph_id: str | None = None,
        candidate_id: str | None = None,
        use_glossary: bool = False,
    ) -> JobRecord:
        """新建 job 并入队（``queued``）；准入判断由调用方在 ``lock`` 内做。

        ``use_glossary`` 在这里归一：只有 ``action="run"``（= 真的会跑翻译阶段）才留
        true，其余 action 一律记 false —— 记录里的字段要么真生效，要么就别声称生效。
        """
        record = JobRecord(
            job_id=new_job_id(),
            did=did,
            action=action,
            created_at=utc_now(),
            from_stage=from_stage,
            profile=profile,
            pages=pages,
            dual=dual,
            use_glossary=bool(use_glossary) and action == "run",
            requested_scope=requested_scope,
            effective_scope=effective_scope,
            downgrade_reason=downgrade_reason,
            trigger=trigger,
            paragraph_id=paragraph_id,
            candidate_id=candidate_id,
        )
        self.records[record.job_id] = record
        self._queue.append(record.job_id)
        self._write(record, EVENT_QUEUED)
        return record

    def pop_queued(self) -> JobRecord | None:
        """取队首仍处于 ``queued`` 的 job（FIFO）并移出队列；没有 → ``None``。"""
        while self._queue:
            job_id = self._queue.pop(0)
            record = self.records.get(job_id)
            if record is not None and record.status == "queued":
                return record
        return None

    def mark_started(
        self, record: JobRecord, *, pid: int, pgid: int, spawn_marker: str
    ) -> JobRecord:
        """``queued`` → ``running``：记进程身份（pid/pgid/boot_id/argv 指纹）。"""
        record.status = "running"
        record.started_at = utc_now()
        record.pid = pid
        record.pgid = pgid
        record.boot_id = self.boot_id
        record.spawn_marker = spawn_marker
        self._write(record, EVENT_STARTED)
        return record

    def mark_cancel_requested(self, record: JobRecord) -> JobRecord:
        """记下"取消已请求"（不动状态：状态要等进程真的退出才改）。"""
        record.cancel_requested_at = utc_now()
        self.save(record)
        return record

    def mark_finished(
        self,
        record: JobRecord,
        *,
        status: str,
        exit_code: int | None = None,
        envelope: str | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        run_id: str | None = None,
        interrupted_reason: str | None = None,
    ) -> JobRecord:
        """置终态：清掉进程身份（终态不再持有 pid）、写快照 + 生命周期事件。"""
        if status not in TERMINAL_STATUSES:
            raise ValueError(f"不是终态：{status}")
        record.status = status
        record.finished_at = utc_now()
        record.exit_code = exit_code
        record.envelope = envelope
        record.error_code = error_code
        record.error_message = error_message
        if run_id is not None:
            record.run_id = run_id
        if interrupted_reason is not None:
            record.interrupted_reason = interrupted_reason
        record.pid = None
        record.pgid = None
        with contextlib.suppress(ValueError):
            self._queue.remove(record.job_id)
        self._write(record, _TERMINAL_EVENTS[status])
        return record

    def _interrupt(self, record: JobRecord, *, reason: str) -> JobRecord:
        """恢复路径专用的 ``interrupted``（等价于 ``mark_finished``，语义更直白）。"""
        return self.mark_finished(
            record,
            status="interrupted",
            interrupted_reason=reason,
            error_code=reason,
            error_message="服务重启：不自动重跑，请显式重试",
        )

    # ------------------------------------------------------------ 持久化
    def save(self, record: JobRecord) -> None:
        """原子写状态快照（同目录 tmp + ``os.replace``，读方永远看到完整 JSON）。"""
        path = self.snapshot_path(record.job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(
            json.dumps(record.model_dump(), ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        tmp.replace(path)

    def append_event(self, event: str, record: JobRecord, **extra: Any) -> None:
        """追加一行生命周期事件（append-only；只 append，不改历史行）。"""
        payload: dict[str, Any] = {
            "at": utc_now(),
            "event": event,
            "job_id": record.job_id,
            "did": record.did,
            "action": record.action,
            "status": record.status,
            "profile": record.profile,
        }
        for key in (
            "run_id",
            "exit_code",
            "error_code",
            "interrupted_reason",
            "requested_scope",
            "effective_scope",
            "trigger",
            "paragraph_id",
            "candidate_id",
        ):
            value = getattr(record, key)
            if value is not None:
                payload[key] = value
        payload.update(extra)
        self.events_path.parent.mkdir(parents=True, exist_ok=True)
        with self.events_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False) + "\n")

    def _write(self, record: JobRecord, event: str) -> None:
        """快照先落盘、事件后追加：事件日志不会引用盘上还没有的状态。"""
        self.save(record)
        self.append_event(event, record)


def _read_snapshot(path: Path) -> JobRecord | None:
    """读一个 job 快照；不可解析 / 字段不符 → ``None``（跳过，不影响其它 job）。"""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    try:
        return JobRecord.model_validate(payload)
    except ValidationError:
        return None
