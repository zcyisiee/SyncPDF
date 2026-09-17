"""候选重译：生成（零副作用）/ 采用（写草稿 + 防抖）/ 拒绝（``docs/frontend/api.md`` §3.6，W11）。

一句话的语义：**候选与译文是两回事**。生成一个候选只在
``<workdir>/.bdt-serve/candidates.json`` 里加一行（外加 job 状态），
``agent/translated.md``、``agent/translated.jsonl``、``draft.json``、``output/``
**一个字节都不动**；只有人工「采用」才把候选写成草稿 ``target``（走 W09 的
:meth:`babeldoc_tools.serve.draft.DraftStore.patch_current` + 1.5s 防抖编译）。

生成为什么必须隔离（已核实的实现事实）：``translate.retranslate_blocks``
**直接落盘** —— 它写 ``agent/prompt.retry.md``、``agent/translated.retry.md``，并且把
新译文经 :func:`babeldoc_tools.translate.merge_translated_markdown` **合并进
``agent/translated.md``**。所以候选生成**复用** ``bdt translate --ids``（同一实现、
同一提示词、同一合并逻辑），但跑在**隔离副本**里::

    <workdir>/.bdt-serve/candidates-<job_id>/agent/anchors.json     ← 只 copy 生成真正读到的
    <workdir>/.bdt-serve/candidates-<job_id>/agent/translated.md       两份文件（agent/ 可能
                                                                       有几百 MB 的 IR 产物）

拿回什么：副本里 ``agent/translated.retry.md``（模型这次**返回的原文**，按 ``<!-- id=... -->``
切块）里该段的正文本 → 短锚点转 canonical（与 ``GET /paragraphs`` 的 ``target``、草稿
``target`` 同一表示）→ 写进候选行。**不读合并后的 ``translated.md``**：模型漏掉该段时
合并会保留旧值，那样读出来的"候选"其实是旧译文（假候选）。

复用的是函数而不是进程内的调用：候选生成是一个 job（排队/取消/并发上限全走
:class:`babeldoc_tools.serve.runner.JobRunner`），子进程 = ``bdt translate``，translator
是它自己拉的孙进程 —— 取消时杀整个进程组（连带收费的 LLM 调用）才有意义；在事件循环里
``to_thread(retranslate_blocks)`` 做不到这一点。命令同样只来自 profile（客户端只说 profile id）。

候选文件形状（``next_id`` 是发号器，删行不回退）::

    {"version": 1, "next_id": 2, "items": [
      {"id": "c_0001", "pid": "P05-002", "source": "...", "baseline_target": "...",
       "candidate_target": "...", "status": "pending", "model_label": "stub",
       "job_id": "j_01H...", "created_at": "...", "adopted_at": null}]}
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any
from typing import NamedTuple

from babeldoc.tools.agent import markdown_view
from pydantic import ValidationError

from babeldoc_tools.common import ToolError
from babeldoc_tools.serve import views
from babeldoc_tools.serve.draft import PARAGRAPH_ID_RE
from babeldoc_tools.serve.draft import DraftResponse
from babeldoc_tools.serve.jobs import JobRecord
from babeldoc_tools.serve.jobs import utc_now
from babeldoc_tools.serve.profiles import list_profile_ids
from babeldoc_tools.serve.profiles import resolve_profile
from babeldoc_tools.serve.schemas import CandidateItem
from babeldoc_tools.serve.store import STATE_DIR
from babeldoc_tools.serve.store import DocumentStore
from babeldoc_tools.serve.workdir import WorkdirReader

if TYPE_CHECKING:
    from babeldoc_tools.serve.compile import CompileService
    from babeldoc_tools.serve.runner import JobRunner

__all__ = [
    "AGENT_DIR_NAME",
    "CANDIDATES_DIR_PREFIX",
    "CANDIDATES_FILE",
    "CANDIDATES_VERSION",
    "CANDIDATE_ID_PREFIX",
    "RETRY_MARKDOWN",
    "SNAPSHOT_FILES",
    "CandidateOutcome",
    "CandidatePlan",
    "CandidateRegistry",
    "CandidateService",
    "CandidateStore",
    "build_candidates_argv",
    "candidates_path",
    "paragraph_facts",
    "prepare_candidates",
    "read_candidate_target",
    "read_candidates",
    "settle_candidates",
    "sweep_isolated",
]

#: ``<workdir>/.bdt-serve/candidates.json``（候选列表；服务端私有状态，不进 ``agent/``）。
CANDIDATES_FILE = "candidates.json"
#: 候选文件结构版本（形状变了才 +1，与 api.md §3.6 解耦）。
CANDIDATES_VERSION = 1
#: 隔离副本目录名前缀（``<workdir>/.bdt-serve/candidates-<job_id>/``）。
CANDIDATES_DIR_PREFIX = "candidates-"
#: 候选 id 前缀（``c_`` + 4 位十进制发号，单调递增）。
CANDIDATE_ID_PREFIX = "c_"

#: 副本里要 copy 的 ``agent/`` 文件：``retranslate_blocks`` 真正读到的两个
#: （``render_retry_markdown`` 读 anchors，``merge_translated_markdown`` 读 translated.md）。
#: ``agent/`` 里有 ``il_translated.applied.json``(几百 MB) / ``state.pkl``(几十 MB) 这类
#: 大件，整目录拷贝在长文档上要好几秒，而生成路径一行都不读它们。
SNAPSHOT_FILES = ("anchors.json", "translated.md")
#: ``agent/`` 目录名（副本里的落地位置）。
AGENT_DIR_NAME = "agent"
#: 模型这次返回的译文文件名（``retranslate_blocks`` 写的），也是候选的唯一取值处。
RETRY_MARKDOWN = "translated.retry.md"


# --------------------------------------------------------------------------- #
# 候选人写入/读取
# --------------------------------------------------------------------------- #
def candidates_path(workdir: Path | str) -> Path:
    """``<workdir>/.bdt-serve/candidates.json``。"""
    return Path(workdir) / STATE_DIR / CANDIDATES_FILE


def read_candidates(workdir: Path | str) -> dict[str, Any]:
    """读候选文件；缺失 / 坏 JSON / 形状不符 → 空文件（``next_id=1``，不谎报历史）。"""
    empty: dict[str, Any] = {
        "version": CANDIDATES_VERSION,
        "next_id": 1,
        "items": [],
    }
    try:
        payload = json.loads(candidates_path(workdir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return empty
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        return empty
    next_id = payload.get("next_id")
    if not isinstance(next_id, int) or isinstance(next_id, bool) or next_id < 1:
        next_id = 1
    items = [item for item in payload["items"] if isinstance(item, dict)]
    return {"version": CANDIDATES_VERSION, "next_id": next_id, "items": items}


class CandidateStore:
    """一个 workdir 的候选读写（``asyncio.Lock`` 串行化写，读不加锁）。

    与 :class:`babeldoc_tools.serve.draft.DraftStore` 同一套路：per-workdir 的锁
    （两个文档的候选互不阻塞），写盘是**同目录 tmp + ``os.replace``** 的原子替换，
    读路径靠它保证"永远读到完整文件"。
    """

    def __init__(self, workdir: Path | str) -> None:
        self.workdir = Path(workdir)
        self._lock = asyncio.Lock()

    @property
    def path(self) -> Path:
        """候选文件路径。"""
        return candidates_path(self.workdir)

    def read(self) -> dict[str, Any]:
        """同步读（容错），见 :func:`read_candidates`。"""
        return read_candidates(self.workdir)

    def _write(self, payload: dict[str, Any]) -> None:
        """原子写（同目录 tmp + ``os.replace``）。"""
        path = self.path
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        tmp.replace(path)

    def items_for_pid(self, pid: str) -> list[CandidateItem]:
        """该段的候选：最新的 ``pending`` 在前，其后已决定的候选（新 → 旧）。"""
        items = [
            item for item in self.read()["items"] if str(item.get("pid")) == pid
        ]
        parsed = [_parse_item(item) for item in items]
        live = [item for item in parsed if item is not None]
        pending = [item for item in live if item.status == "pending"]
        decided = [item for item in live if item.status != "pending"]
        return list(reversed(pending)) + list(reversed(decided))

    async def add_pending(
        self,
        *,
        pid: str,
        source: str | None,
        baseline_target: str | None,
        model_label: str,
    ) -> CandidateItem:
        """建一条 ``pending`` 候选（``candidate_target`` 还是 ``null``）并返回它。

        发号在锁内做（``next_id`` 单调递增，删行不回退），因此同一个 did 的并发提交
        不会拿到同一个 id。
        """
        async with self._lock:
            payload = self.read()
            number = int(payload["next_id"])
            payload["next_id"] = number + 1
            item = CandidateItem(
                id=f"{CANDIDATE_ID_PREFIX}{number:04d}",
                pid=pid,
                source=source,
                baseline_target=baseline_target,
                candidate_target=None,
                status="pending",
                model_label=model_label,
                created_at=utc_now(),
            )
            payload["items"].append(item.model_dump())
            self._write(payload)
            return item

    async def complete(self, candidate_id: str, *, candidate_target: str) -> CandidateItem:
        """生成成功：填上候选译文（``status`` 仍是 ``pending`` —— 还没被采用）。"""
        return await self._update(
            candidate_id,
            lambda item: item | {"candidate_target": candidate_target},
        )

    async def attach_job(self, candidate_id: str, *, job_id: str) -> CandidateItem:
        """记下生成它的 job id（提交之后才知道，所以分两步写）。"""
        return await self._update(candidate_id, lambda item: item | {"job_id": job_id})

    async def adopt(self, candidate_id: str) -> CandidateItem:
        """采用：``status=adopted`` + ``adopted_at``（草稿由调用方在之前写好）。"""
        now = utc_now()
        return await self._update(
            candidate_id, lambda item: item | {"status": "adopted", "adopted_at": now}
        )

    async def reject(self, candidate_id: str) -> CandidateItem:
        """拒绝：``status=rejected``（不改任何译文/草稿）。"""
        return await self._update(candidate_id, lambda item: item | {"status": "rejected"})

    async def drop(self, candidate_id: str) -> None:
        """删掉一条候选（生成失败/被取消的那种，不留"半条候选"在列表里）。"""
        async with self._lock:
            payload = self.read()
            payload["items"] = [
                item
                for item in payload["items"]
                if str(item.get("id")) != candidate_id
            ]
            self._write(payload)

    def drop_unfinished(self) -> list[str]:
        """删掉**还没生成的**候选（``pending`` 且没有译文）；返回被删的 id。

        只在启动核对里用：候选行先于 job 存在，而 job 不会跨进程存活（重启后
        ``queued``/``running`` 一律 ``interrupted``），所以启动时还挂着"生成中"的候选
        已经不可能再被填上了 —— 留着它前端会永远转圈。
        """
        payload = self.read()
        dropped = [
            str(item.get("id"))
            for item in payload["items"]
            if item.get("status") == "pending" and item.get("candidate_target") is None
        ]
        if dropped:
            payload["items"] = [
                item for item in payload["items"] if str(item.get("id")) not in dropped
            ]
            self._write(payload)
        return dropped

    async def _update(
        self, candidate_id: str, patch: Callable[[dict[str, Any]], dict[str, Any]]
    ) -> CandidateItem:
        """锁内读-改-写一条候选；id 不在文件里 → ``ToolError(candidate_not_found)``。"""
        async with self._lock:
            payload = self.read()
            for index, raw in enumerate(payload["items"]):
                if str(raw.get("id")) != candidate_id:
                    continue
                updated = patch(dict(raw))
                payload["items"][index] = updated
                self._write(payload)
                item = _parse_item(updated)
                if item is None:  # pragma: no cover - 写进去的就是合法形状
                    raise ToolError(
                        "candidate_not_found",
                        f"候选 {candidate_id} 的形状不合法",
                        candidate_id=candidate_id,
                    )
                return item
        raise ToolError(
            "candidate_not_found", f"候选不存在：{candidate_id}", candidate_id=candidate_id
        )


def _parse_item(raw: dict[str, Any]) -> CandidateItem | None:
    """一条候选（坏行 → ``None``，跳过：一个坏条目不该让整个列表读不出来）。"""
    try:
        return CandidateItem.model_validate(raw)
    except ValidationError:
        return None


class CandidateRegistry:
    """``did`` → :class:`CandidateStore` 缓存（同一个 did 永远拿到同一个锁对象）。"""

    def __init__(self, store: DocumentStore) -> None:
        self._store = store
        self._stores: dict[str, CandidateStore] = {}

    def for_did(self, did: str) -> CandidateStore:
        """该 did 的候选存储（did 越界/不存在 → ``ToolError``，与其它端点同码）。"""
        return self.for_workdir(self._store.resolve(did))

    def for_workdir(self, workdir: Path | str) -> CandidateStore:
        """该 workdir 的候选存储（写入路径已经 resolve 过 did 时用这个，不必再解析一次）。"""
        key = str(Path(workdir))
        store = self._stores.get(key)
        if store is None:
            store = CandidateStore(workdir)
            self._stores[key] = store
        return store


# --------------------------------------------------------------------------- #
# 段落口径（与 GET /paragraphs 同源）
# --------------------------------------------------------------------------- #
def paragraph_facts(workdir: Path | str, pid: str) -> tuple[str | None, str | None]:
    """该段的 ``(原文, 译文基线)``；id 不在产物里 → 404 ``paragraph_not_found``。

    直接用 :func:`babeldoc_tools.serve.views.paragraphs`（``GET /paragraphs`` 的同一实现）：
    候选里存的 ``source``/``baseline_target`` 必须与前端在同一个面板里看到的那两列一致，
    所以不另外拼一份 join（两处口径迟早漂移）。id 形状不合法走同一个 404：它必然不在产物里。
    """
    if not PARAGRAPH_ID_RE.match(pid):
        raise ToolError(
            "paragraph_not_found",
            f"段落 id 不合法：{pid}（形状 [A-Za-z0-9._-]{{1,64}}）",
            pid=pid,
        )
    for item in views.paragraphs(WorkdirReader(workdir)):
        if item.id == pid:
            return item.source, item.target
    raise ToolError("paragraph_not_found", f"段落不存在：{pid}", pid=pid)


# --------------------------------------------------------------------------- #
# 隔离副本（生成路径的零副作用保证）
# --------------------------------------------------------------------------- #
def sweep_isolated(workdir: Path | str) -> list[str]:
    """清掉上一次残留的候选副本（服务崩溃/被 kill 留下的）；返回被删的目录名。"""
    base = Path(workdir) / STATE_DIR
    removed: list[str] = []
    with contextlib.suppress(OSError):
        for entry in base.glob(f"{CANDIDATES_DIR_PREFIX}*"):
            if not entry.is_dir():
                continue
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry.name)
    return removed


@dataclass
class CandidatePlan:
    """一次候选生成的隔离上下文（内存态；终态后随 job 句柄释放）。"""

    job_id: str
    did: str
    #: 真 workdir（候选行写这里，正文产物一个字节都不动）。
    workdir: Path
    #: 隔离副本（``bdt translate --ids`` 实际跑的地方）。
    isolated: Path
    candidate_id: str
    pid: str
    #: 该 did 的候选存储（终态时填/删候选行要用同一把锁）。
    store: CandidateStore
    started_at: str


def prepare_candidates(
    record: JobRecord, workdir: Path | str, store: CandidateStore
) -> CandidatePlan:
    """建隔离副本（只 copy 生成真正读到的那两个 ``agent/`` 文件）；失败清掉副本再抛。

    同步、纯文件操作（调用方放进线程跑，别卡事件循环）。前置失败**不 spawn 子进程**：
    生成没开始，真 workdir 分毫未动，候选行由调用方在失败路径里删掉。
    """
    pid = record.paragraph_id
    candidate_id = record.candidate_id
    if not pid or not candidate_id:  # pragma: no cover - 路由层保证两者都有
        raise ToolError(
            "candidate_context_missing",
            "retranslate job 缺段落 id 或候选 id：不启动子进程",
            job_id=record.job_id,
        )
    workdir = Path(workdir)
    sweep_isolated(workdir)
    isolated = workdir / STATE_DIR / f"{CANDIDATES_DIR_PREFIX}{record.job_id}"
    agent = isolated / AGENT_DIR_NAME
    try:
        agent.mkdir(parents=True, exist_ok=True)
        copied: list[str] = []
        for name in SNAPSHOT_FILES:
            source = workdir / AGENT_DIR_NAME / name
            if source.is_file():
                shutil.copy2(source, agent / name)
                copied.append(name)
        if "anchors.json" not in copied:
            raise ToolError(
                "paragraphs_unavailable",
                f"缺 {AGENT_DIR_NAME}/anchors.json：没有可重译的段落产物（先 bdt parse）",
                workdir=str(workdir),
            )
    except BaseException:
        shutil.rmtree(isolated, ignore_errors=True)
        raise
    return CandidatePlan(
        job_id=record.job_id,
        did=record.did,
        workdir=workdir,
        isolated=isolated,
        candidate_id=candidate_id,
        pid=pid,
        store=store,
        started_at=utc_now(),
    )


def build_candidates_argv(
    isolated: Path, *, pid: str, translator: str
) -> list[str]:
    """候选生成的**唯一** argv 来源：``bdt translate --ids <pid>``（不跑 apply/build）。

    用 ``translate`` 子命令（不是 ``bdt run --from translate``）：后者会一路跑到
    build/check/review（分钟级，且与候选无关）。入口固定
    ``sys.executable -m babeldoc_tools``（模块入口，不依赖 PATH 上的 ``bdt``）；
    ``--translator`` 只来自 profile（客户端永远只给 profile id）。
    **不带** ``--debug``：副本马上要被删，归档/查看器在那里只会变成无主残留。
    """
    return [
        sys.executable,
        "-m",
        "babeldoc_tools",
        "translate",
        "--workdir",
        str(isolated),
        "--ids",
        pid,
        "--translator",
        translator,
    ]


def read_candidate_target(isolated: Path | str, pid: str) -> tuple[str | None, list[str]]:
    """副本里取该段的候选译文 → ``(canonical 文本 | None, 模型这次返回的 id 列表)``。

    读 ``agent/translated.retry.md``（模型这次返回的原文）而不是合并后的
    ``translated.md``：模型漏掉该段时合并会保留旧译文，那样读出来的"候选"是旧值
    （假候选，采用后什么都没有改变）。canonical 转换用 ``markdown_to_canonical``：
    与 ``merge_translated_markdown`` 的反向函数同一份实现（唯一实现，不复制）。
    """
    path = Path(isolated) / AGENT_DIR_NAME / RETRY_MARKDOWN
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None, []
    blocks = markdown_view.parse_translated_markdown(text)
    seen = list(blocks)
    entry = blocks.get(pid)
    if entry is None:
        return None, seen
    body = entry[0].strip()
    if not body:
        return None, seen
    return markdown_view.markdown_to_canonical(body), seen


class CandidateOutcome(NamedTuple):
    """候选生成的收尾结论（job 终态由它决定）。"""

    status: str
    error_code: str | None
    error_message: str | None


async def settle_candidates(
    plan: CandidatePlan, *, status: str, error_code: str | None, error_message: str | None
) -> CandidateOutcome:
    """收尾一次候选生成：填候选行或删掉它，**无论成败都删隔离副本**。

    子进程的终态由调用方按既有规则判（:func:`babeldoc_tools.serve.runner.classify_exit`），
    这里只做候选侧的账：``succeeded`` → 从副本取译文填候选（取不到就把 job 记成
    ``candidate_missing``，不报一个空成功）；其余终态 → 删掉候选行（不留半条）。
    """
    try:
        if status == "succeeded":
            target, seen = read_candidate_target(plan.isolated, plan.pid)
            if target is None:
                await plan.store.drop(plan.candidate_id)
                return CandidateOutcome(
                    "failed",
                    "candidate_missing",
                    f"翻译命令没有返回段落 {plan.pid} 的译文（返回的 id：{seen or '无'}）",
                )
            await plan.store.complete(plan.candidate_id, candidate_target=target)
            return CandidateOutcome("succeeded", None, None)
        with contextlib.suppress(ToolError):
            await plan.store.drop(plan.candidate_id)
        return CandidateOutcome(status, error_code, error_message)
    except Exception as exc:  # noqa: BLE001 - 收尾自身出错也必须落终态
        with contextlib.suppress(Exception):
            await plan.store.drop(plan.candidate_id)
        return CandidateOutcome(
            "failed", "candidate_settle_failed", f"{type(exc).__name__}: {exc}"
        )
    finally:
        shutil.rmtree(plan.isolated, ignore_errors=True)


async def discard_plan(
    plan: CandidatePlan, *, error_code: str, error_message: str | None
) -> CandidateOutcome:
    """生成**没真正开始**（prepare/spawn 失败）：删副本 + 删候选行，真 workdir 不动。"""
    try:
        with contextlib.suppress(ToolError):
            await plan.store.drop(plan.candidate_id)
        return CandidateOutcome("failed", error_code, error_message)
    finally:
        shutil.rmtree(plan.isolated, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 服务：生成（job）/ 采用（写草稿 + 防抖）/ 拒绝
# --------------------------------------------------------------------------- #
class CandidateService:
    """候选重译的调度面（一个 ``bdt serve`` 一个实例，与 :class:`JobRunner` 共用状态）。

    - **生成**：建 ``pending`` 候选行 → 提交 ``action=retranslate`` job（排队/取消/并发
      全部复用 :class:`~babeldoc_tools.serve.runner.JobRunner`）；
    - **采用**：同步请求 —— 走草稿通道写 ``target``（``revision+1``）→ 标 ``adopted``
      → 触发 1.5s 防抖编译（与服务端 PATCH 草稿完全相同的一条链）；
    - **拒绝**：只改候选行状态，不碰草稿；
    - **启动核对**：``pending`` 且没有译文的候选一律删（它们的 job 不会跨进程存活）。
    """

    def __init__(
        self, store: DocumentStore, runner: JobRunner, compiles: CompileService
    ) -> None:
        self.store = store
        self.runner = runner
        self.compiles = compiles
        #: 与 runner 共用同一个 registry：候选行的锁与 job 侧收尾用的是同一把。
        self.stores: CandidateRegistry = runner.candidates
        self.reconcile()

    # ------------------------------------------------------------ 生命周期
    def reconcile(self) -> list[str]:
        """启动核对：删掉"生成中"的候选（它们的 job 已随上次进程结束）。"""
        dropped: list[str] = []
        try:
            dids = self.store.list_dids()
        except ToolError:
            return dropped
        for did in dids:
            dropped.extend(self.stores.for_did(did).drop_unfinished())
        return dropped

    # ---------------------------------------------------------------- 读
    def list_for_pid(self, did: str, pid: str) -> list[CandidateItem]:
        """该段的候选（pid 不存在 → 404，与生成路径同一口径）。"""
        workdir = self.store.resolve(did)
        paragraph_facts(workdir, pid)
        return self.stores.for_did(did).items_for_pid(pid)

    # -------------------------------------------------------------- 生成
    async def request_retranslate(
        self, did: str, pid: str, profile_id: str | None
    ) -> tuple[CandidateItem, JobRecord]:
        """建候选行 + 提交 ``action=retranslate`` job；返回 ``(候选, job)``。

        顺序刻意如此：先校验（did/pid/profile/忙）→ 建候选行 → 提交 job。job 提交失败
        （如另一个请求抢先占了文档槽）时把候选行删掉，不留孤儿行。
        """
        workdir = self.store.resolve(did)
        self._reject_busy(did)
        resolved = self._require_translator_profile(profile_id)
        source, baseline = paragraph_facts(workdir, pid)
        store = self.stores.for_did(did)
        item = await store.add_pending(
            pid=pid,
            source=source,
            baseline_target=baseline,
            model_label=resolved,
        )
        try:
            record = await self.runner.submit(
                did=did,
                action="retranslate",
                from_stage=None,
                pages=None,
                dual=False,
                profile_id=resolved,
                paragraph_id=pid,
                candidate_id=item.id,
            )
        except ToolError:
            await store.drop(item.id)
            raise
        try:
            await store.attach_job(item.id, job_id=record.job_id)
        except ToolError:
            # 极端竞态：job 在 submit 返回前就失败并删掉了候选行（例如隔离副本准备阶段
            # 就报错）。候选那时已经不存在，真相在 job 记录里 —— 不能因为"回链写不上"
            # 把一个已经受理的请求变成 404。
            pass
        return item, record

    # -------------------------------------------------------------- 采用
    async def adopt(self, did: str, pid: str, candidate_id: str) -> tuple[CandidateItem, DraftResponse]:
        """采用候选：写草稿 ``target``（``revision+1``）+ 标 ``adopted`` + 防抖编译。

        草稿先写、状态后改：草稿写失败（活动 job 期间 ``409 document_busy``）时候选必须
        留在 ``pending``（用户还能再点一次），不能出现"标了已采用但草稿没改"的假账。
        """
        workdir = self.store.resolve(did)
        store = self.stores.for_did(did)
        item = self._require_candidate(store, pid, candidate_id)
        if item.status == "adopted":
            raise ToolError(
                "candidate_decided",
                f"候选 {candidate_id} 已被采用（{item.adopted_at}）",
                candidate_id=candidate_id,
                status=item.status,
            )
        if item.status == "rejected":
            raise ToolError(
                "candidate_decided",
                f"候选 {candidate_id} 已被拒绝：拒绝的候选不能采用",
                candidate_id=candidate_id,
                status=item.status,
            )
        if not item.candidate_target:
            raise ToolError(
                "candidate_not_ready",
                f"候选 {candidate_id} 还在生成中（candidate_target 为空），先等 job 结束",
                candidate_id=candidate_id,
                job_id=item.job_id,
            )
        # 忙守卫放在候选自身的状态判断之后："还在生成中"比"有活动 job"更精确
        # （活动 job 就是它的生成 job）。真正要写草稿时才看有没有别的 job 在跑。
        self._reject_busy(did)
        doc = await self.runner.drafts.for_did(did).patch_current(
            {pid: {"target": item.candidate_target}}
        )
        adopted = await store.adopt(candidate_id)
        # 与服务端 PATCH 草稿完全相同的一条链：写成功才排防抖编译。
        self.compiles.schedule(did)
        return adopted, DraftResponse(**doc.model_dump(exclude={"version"}))

    # -------------------------------------------------------------- 拒绝
    async def reject(self, did: str, pid: str, candidate_id: str) -> CandidateItem:
        """拒绝候选：只改状态（不碰草稿，因此**不**受活动 job 的草稿只读守卫限制）。"""
        self.store.resolve(did)
        store = self.stores.for_did(did)
        item = self._require_candidate(store, pid, candidate_id)
        if item.status == "rejected":
            return item  # 幂等：重复拒绝不报错（也不改任何东西）
        if item.status == "adopted":
            raise ToolError(
                "candidate_decided",
                f"候选 {candidate_id} 已被采用：不能再用拒绝把它改回去",
                candidate_id=candidate_id,
                status=item.status,
            )
        return await store.reject(candidate_id)

    # -------------------------------------------------------------- 内部
    def _require_candidate(
        self, store: CandidateStore, pid: str, candidate_id: str
    ) -> CandidateItem:
        """该 pid 下按 id 找候选；id 不存在或不属于这个 pid → 404 ``candidate_not_found``。"""
        for item in store.items_for_pid(pid):
            if item.id == candidate_id:
                return item
        raise ToolError(
            "candidate_not_found",
            f"候选不存在：{candidate_id}（段落 {pid}）",
            candidate_id=candidate_id,
            pid=pid,
        )

    def _require_translator_profile(self, profile_id: str | None) -> str:
        """``profile`` → 可用的 translator profile id；不满足 → 422（不猜、不兜底）。

        三种失败分开报：没给/该 profile 没配 translator → ``profile_missing``，
        未知 id → ``unknown_profile``（detail 带可选 id 列表，命令永不出现）。
        """
        if not profile_id:
            raise ToolError(
                "profile_missing",
                "重译候选需要 profile（它的 translator 命令是唯一的翻译来源）："
                "在请求体里给 profile id",
                available=list_profile_ids(self.store.store_base),
            )
        profile = resolve_profile(self.store.store_base, profile_id)
        if profile is None:
            raise ToolError(
                "unknown_profile",
                f"未知 profile：{profile_id}",
                profile=profile_id,
                available=list_profile_ids(self.store.store_base),
            )
        if not profile.translator:
            raise ToolError(
                "profile_missing",
                f"profile {profile_id} 没有配 translator 命令：候选生成没有可用的翻译命令",
                profile=profile_id,
            )
        return profile.id

    def _reject_busy(self, did: str) -> None:
        """活动 job 期间的守卫（与草稿写端点同一个 409 ``document_busy``）。"""
        active = self.runner.registry.active_for_did(did)
        if active is None:
            return
        raise ToolError(
            "document_busy",
            f"文档 {did} 有活动 job：{active.job_id}（{active.status}）；"
            "任务期间不接受新的重译/采用请求，请等它结束后再试",
            job_id=active.job_id,
            status=active.status,
            action=active.action,
        )
