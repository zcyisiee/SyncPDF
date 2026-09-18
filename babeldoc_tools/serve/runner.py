"""受控子进程：job → ``bdt run`` 子进程的启动、监控、取消（W07）。

三条硬约束（EXECUTION.md 纠偏第 5 / 8 条；``docs/frontend/api.md`` §3.4）：

1. **argv 只由服务端构造**。客户端能给的只有 ``action``/``from``/``pages``/``dual``/
   ``profile``/``use_glossary``；translator/reviewer 命令在 :mod:`babeldoc_tools.serve.profiles` 里按
   profile id 解析后直接进 argv，**不回传客户端**。入口固定为
   ``sys.executable -m babeldoc_tools run``（模块入口，不是第二个 console 入口；裸
   ``bdt`` 依赖 PATH，服务端不可靠）。词表同理：客户端只给 ``use_glossary`` 布尔，
   词表**文件路径**由服务端取 ``<store_base>/.bdt-serve/glossary.csv`` 经 ``--glossaries``
   注入（仅 ``action=run`` 且真的跑 translate 阶段时；重译/编译不注入）。
2. **取消杀整个进程组**。``start_new_session=True`` 让子进程自成进程组，取消时
   ``os.killpg`` SIGTERM → 5s 宽限 → SIGKILL，连带 translator 孙进程一起收掉，并且
   **等进程真的退出后才置 canceled 并释放文档槽**（锁持到退出）。
3. **重启不自动重跑、不凭孤立 pid 发信号**（``boot_id`` 方案见
   :mod:`babeldoc_tools.serve.jobs`）：本模块只在**自己 spawn 过**的 ``Popen`` 上发信号。
4. **信封脱敏后才落盘**：子进程 stdout 的收尾信封会被引在 ``data.config``/``error.message``
   里的 profile 命令（可能内嵌密钥）与带 token 的 ``data.debug.url``，落库前一律经
   :func:`sanitize_envelope` 换掉（W07 遗留风险 #1）。

子进程的 stdout 只有收尾一行 JSON 信封（``{"ok":...}``）；stderr 是日志。两个管道都
必须读干（见 :class:`PipeCapture`），否则子进程会被写满的管道卡住。
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any
from typing import NamedTuple

from babeldoc_tools.common import ToolError
from babeldoc_tools.serve import candidates as candidates_mod
from babeldoc_tools.serve import compile as compile_mod
from babeldoc_tools.serve.draft import DraftRegistry
from babeldoc_tools.serve.glossary import GlossaryStore
from babeldoc_tools.serve.jobs import TERMINAL_STATUSES
from babeldoc_tools.serve.jobs import JobRecord
from babeldoc_tools.serve.jobs import JobRegistry
from babeldoc_tools.serve.profiles import Profile
from babeldoc_tools.serve.profiles import list_profile_ids
from babeldoc_tools.serve.profiles import resolve_profile
from babeldoc_tools.serve.store import DocumentStore
from babeldoc_tools.serve.uploads import SOURCE_NAME
from babeldoc_tools.serve.workdir import list_run_ids

__all__ = [
    "CANCEL_GRACE_SECONDS",
    "CHECK_TIMEOUT_SECONDS",
    "EXIT_POLL_SECONDS",
    "JOB_TIMEOUT_SECONDS",
    "MAX_ENVELOPE_BYTES",
    "PIPE_TAIL_LINES",
    "PROFILE_PLACEHOLDER",
    "JobOutcome",
    "JobRunner",
    "PipeCapture",
    "argv_marker",
    "build_job_argv",
    "capture_pipes",
    "classify_exit",
    "job_timeout_seconds",
    "new_run_id",
    "sanitize_envelope",
    "sanitize_payload",
    "spawn_job",
    "stdout_envelope",
]

#: job 级超时（``api.md`` §3.4 没有这个字段，是服务端护栏）：``run`` 一整条链路
#: 默认 3600s、单阶段性的 ``check`` 600s。超时走取消路径（杀进程组）+ ``timed_out``。
JOB_TIMEOUT_SECONDS = 3600.0
CHECK_TIMEOUT_SECONDS = 600.0

#: SIGTERM 之后的宽限时间；到点还在就 SIGKILL。
CANCEL_GRACE_SECONDS = 5.0
#: 等进程退出的轮询间隔（子进程真实退出优先于延迟）。
EXIT_POLL_SECONDS = 0.05
#: 宽限期内轮询进程退出的间隔。
CANCEL_POLL_SECONDS = 0.05
#: 等管道读线程收尾的上限（进程已退出，读线程只剩把缓冲读完）。
PIPE_JOIN_SECONDS = 5.0

#: 每个管道保留的末尾行数（诊断够用，不把整篇日志吃进内存）。
PIPE_TAIL_LINES = 200

#: 收尾信封入库的字节上限（``api.md`` §3.4：envelope 可截断到 4KB 存）。
MAX_ENVELOPE_BYTES = 4096

#: 脱敏后替换 profile 命令字符串的占位符形状（``api.md`` §3.4：``<profile:echo-t>``）。
PROFILE_PLACEHOLDER = "<profile:{}>"

#: 带 token 的 debug 查看器 URL（兜底路径用：``?token=...`` 整段抹掉）。
_TOKEN_URL_RE = re.compile(r"https?://[^\s\"'<>]*\?token=[^\s\"'<>]*")


def job_timeout_seconds(action: str) -> float:
    """该 action 的 job 级超时：``check`` 600s，``run`` 3600s。"""
    return CHECK_TIMEOUT_SECONDS if action == "check" else JOB_TIMEOUT_SECONDS


# --------------------------------------------------------------------------- #
# argv 构造（客户端输入永远只是参数值，命令只来自 profile）
# --------------------------------------------------------------------------- #
def build_job_argv(
    *,
    workdir: Path,
    action: str,
    from_stage: str | None,
    pages: str | None,
    dual: bool,
    profile: Profile,
    glossaries: str | None = None,
) -> list[str]:
    """job → ``bdt run`` 的 argv。**唯一**的 argv 来源（服务端拼装）。

    - 入口固定 ``[sys.executable, "-m", "babeldoc_tools", "run", "--workdir", <workdir>]``；
    - ``--from``：``check`` 固定 ``check``（check 阶段 + 其后 review/report 沿用同一套
      质量门禁，不另起一份判断）；``run`` 用请求里的 ``from``（缺省留给 CLI 的 parse）；
    - ``--translator``/``--reviewer`` 只在 profile 给了对应命令时出现，值来自 profile
      （客户端无法影响）；
    - ``glossaries`` 是**服务端**取好的词表 CSV 绝对路径（值为 ``None`` = 不注入）。
      调用方（:meth:`JobRunner._glossary_path`）已经判过“这个 job 该不该注入 + 词表是不是
      空”，这里只管拼接：空词表不注入（仅多一段没有条目的约束说明，毫无收益）；
    - ``run`` 且起点是 ``parse``（含缺省）时**追加 PDF 位置参数**
      ``<workdir>/source.pdf``：位置参数是 parse 的输入，服务端只有这一个来源
      （上传时写在那里）；``translate`` 及之后的阶段不需要它（CLI 允许省略）；
    - ``--debug --debug-no-open``：给子进程建 ``debug/runs/<run_id>`` 归档（前端事件/
      几何/时间线都读它）；``--debug-no-open`` 避免无人值守时弹浏览器。
    """
    argv = [
        sys.executable,
        "-m",
        "babeldoc_tools",
        "run",
        "--workdir",
        str(workdir),
        "--debug",
        "--debug-no-open",
    ]
    if action == "check":
        argv += ["--from", "check"]
    elif from_stage:
        argv += ["--from", from_stage]
    if profile.translator:
        argv += ["--translator", profile.translator]
    if profile.model_profile and not profile.reviewer:
        argv += ["--skip-ai-review"]
    if profile.reviewer:
        argv += ["--reviewer", profile.reviewer]
    if pages:
        argv += ["--pages", pages]
    if dual:
        argv += ["--dual"]
    if glossaries:
        argv += ["--glossaries", str(glossaries)]
    if action == "run" and (from_stage or "parse") == "parse":
        # parse 阶段需要 PDF 位置参数（``bdt run <pdf> --from parse``）：新文档的源文件
        # 固定在 workdir 根下（W03 白名单里的 ``source.pdf``，上传时写在那里）。
        # ``from_stage`` 为空 = CLI 缺省 parse，同样必须带。
        argv.append(str(workdir / SOURCE_NAME))
    return argv


def argv_marker(argv: list[str]) -> str:
    """argv 的 sha256 前缀（身份指纹）；**命令原文不落盘**（可能内嵌密钥）。"""
    return hashlib.sha256("\x00".join(argv).encode("utf-8")).hexdigest()[:16]


# --------------------------------------------------------------------------- #
# 信封脱敏（W07 风险 #1：信封原样落盘会把 profile 命令与 debug token 带出去）
# --------------------------------------------------------------------------- #
def sanitize_envelope(text: str, profile: Profile) -> str:
    """把子进程信封里的**服务端秘密**换掉，返回可落盘/可回传的文本（纯函数）。

    三件事（顺序固定）：

    1. **全文替换命令字符串**：与 profile 的 translator/reviewer 命令（含 JSON 转义
       形式）以及带上 token 的 debug URL 逐字命中的地方换成占位符 —— 命令不只出现在
       ``data.config`` 里，还可能被引在 ``error.message``（如 ``<cmd> 退出码 1: ...``）；
    2. **结构化改写**：解析成功时把 ``data.config.translator/reviewer`` 直接写成
       ``<profile:<id>>``（形状与位置都确定，不靠字符串碰），并**删掉 ``data.debug.url``**
       （查看器 URL 带 token），保留 ``run_id``/``manifest``；
    3. **截断**到 :data:`MAX_ENVELOPE_BYTES`（契约就是"最多 4KB"）。

    解析失败（信封被截断/本来就不是 JSON）时退到只做第 1 步的兜底正则路径 —— 宁可留
    一份不好看的文本，也不把密钥写进 ``jobs.jsonl``。
    """
    scrubbed = _scrub_secrets(text, profile)
    try:
        payload = json.loads(scrubbed)
    except ValueError:
        return scrubbed[:MAX_ENVELOPE_BYTES]
    if isinstance(payload, dict):
        _scrub_payload(payload, profile)
        with contextlib.suppress(TypeError, ValueError):
            scrubbed = json.dumps(payload, ensure_ascii=False)
    return scrubbed[:MAX_ENVELOPE_BYTES]


def sanitize_payload(payload: dict, profile: Profile) -> None:
    """已解析信封的**就地**脱敏（与 :func:`sanitize_envelope` 同一套规则）。

    给 :func:`classify_exit` 用：它把 ``error.message`` 抄进 job 记录，而那条消息常常
    整条回显命令（``<cmd> 退出码 3: ...``）—— 落库前必须先把解析结果里的秘密抹掉，
    否则脱敏只做了信封文本那一半。
    """
    for key, value in list(payload.items()):
        payload[key] = _scrub_value(value, profile)
    _scrub_payload(payload, profile)


def _scrub_value(value: Any, profile: Profile) -> Any:
    """递归替换 JSON 值树里的命令字符串 / 带 token 的 URL（键名不动）。"""
    if isinstance(value, str):
        return _scrub_secrets(value, profile)
    if isinstance(value, dict):
        return {key: _scrub_value(item, profile) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrub_value(item, profile) for item in value]
    return value


def _scrub_secrets(text: str, profile: Profile) -> str:
    """文本级兜底：命令字符串（含 JSON 转义形式）与带 token 的 URL 一律换掉。"""
    for command in (profile.translator, profile.reviewer):
        if not command:
            continue
        placeholder = PROFILE_PLACEHOLDER.format(profile.id)
        text = text.replace(command, placeholder)
        escaped = json.dumps(command, ensure_ascii=False)[1:-1]
        if escaped != command:
            text = text.replace(escaped, placeholder)
    return _TOKEN_URL_RE.sub("<redacted-url>", text)


def _scrub_payload(payload: dict, profile: Profile) -> None:
    """结构改写：config 里的命令 → profile id；debug.url（带 token）删掉。"""
    data = payload.get("data")
    if not isinstance(data, dict):
        return
    config = data.get("config")
    if isinstance(config, dict):
        for field in ("translator", "reviewer"):
            if isinstance(config.get(field), str):
                config[field] = PROFILE_PLACEHOLDER.format(profile.id)
    debug = data.get("debug")
    if isinstance(debug, dict):
        debug.pop("url", None)


# --------------------------------------------------------------------------- #
# 进程与管道
# --------------------------------------------------------------------------- #
def spawn_job(argv: list[str], workdir: Path) -> subprocess.Popen:
    """在一个自有进程组里起子进程（取消时整组一起收）。

    ``start_new_session=True`` = ``setsid``：子进程成为新会话/进程组的组长，之后它
    自己拉起的 translator/reviewer 孙进程都留在同一组里（``os.killpg`` 才能一网打尽）。
    stdout/stderr 都走管道（读干由 :class:`PipeCapture` 负责）；stdin 给 ``DEVNULL``
    （``bdt run`` 不从 stdin 读输入，翻译命令的提示词走它自己的管道）。
    """
    return subprocess.Popen(  # noqa: S603 - argv 由服务端构造，不经 shell
        argv,
        cwd=str(workdir),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )


class PipeCapture:
    """后台线程把子进程的 stdout/stderr 读干，只保留末尾若干行。

    只 ``wait()`` 不读管道，子进程会在写满内核缓冲区（约 64KB）后卡死 —— 那会变成
    "假的慢"最后只能等超时收场。两个管道必须并发读（一个线程读一个）。
    """

    def __init__(
        self, proc: subprocess.Popen, *, max_lines: int = PIPE_TAIL_LINES
    ) -> None:
        self._lines: dict[str, deque[str]] = {
            "stdout": deque(maxlen=max_lines),
            "stderr": deque(maxlen=max_lines),
        }
        self._threads: list[threading.Thread] = []
        for name, stream in (("stdout", proc.stdout), ("stderr", proc.stderr)):
            if stream is None:
                continue
            thread = threading.Thread(
                target=self._read, args=(name, stream), daemon=True
            )
            thread.start()
            self._threads.append(thread)

    def _read(self, name: str, stream) -> None:
        try:
            for line in stream:
                self._lines[name].append(line)
        except (OSError, ValueError):  # 进程被杀 / 管道已关：读到哪算哪
            return
        finally:
            with contextlib.suppress(OSError, ValueError):
                stream.close()

    async def wait_closed(self, timeout: float) -> None:
        """等读线程收尾（``timeout`` 是合计上限）。

        用 ``is_alive`` 轮询而不是同步 ``join``：同步 join 会卡住事件循环（最多
        ``timeout`` 秒内所有 HTTP 请求都停摆），而放进执行器又会让事件循环关闭时
        多等一个线程。
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline and any(
            thread.is_alive() for thread in self._threads
        ):
            await asyncio.sleep(EXIT_POLL_SECONDS)

    def text(self, name: str) -> str:
        """某个管道的末尾输出（未结束时读到的是当前已读到的部分）。"""
        return "".join(self._lines[name])

    @property
    def stdout(self) -> str:
        return self.text("stdout")

    @property
    def stderr(self) -> str:
        """stderr 末尾（目前只用于诊断，不落盘：可能含命令与路径）。"""
        return self.text("stderr")


def capture_pipes(proc: subprocess.Popen) -> PipeCapture:
    """给 :func:`spawn_job` 起的子进程接上管道读线程。"""
    return PipeCapture(proc)


def _kill_group(pgid: int, sig: signal.Signals) -> None:
    """给整个进程组发信号（孙进程一并命中）；组已不存在 → 无操作。"""
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return


# --------------------------------------------------------------------------- #
# 信封解析与终态判定
# --------------------------------------------------------------------------- #
def stdout_envelope(stdout: str) -> tuple[str, dict] | None:
    """stdout 里**最后一个**可解析为 JSON 对象的行 → ``(原文, 解析结果)``。

    只逐行找，不做流式拼接：``bdt`` 的收尾信封是完整的一行（日志都走 stderr）。都
    没有 → ``None``（调用方按 :func:`classify_exit` 的 ``envelope_unparsed`` 处理）。
    """
    for line in reversed(stdout.splitlines()):
        text = line.strip()
        if not text.startswith("{"):
            continue
        try:
            payload = json.loads(text)
        except ValueError:
            continue
        if isinstance(payload, dict):
            return text[:MAX_ENVELOPE_BYTES], payload
    return None


class JobOutcome(NamedTuple):
    """终态判定结果（:func:`classify_exit` 的返回值）。"""

    status: str
    error_code: str | None
    error_message: str | None


def classify_exit(
    *,
    action: str,
    cancel_requested: bool,
    timed_out: bool,
    exit_code: int | None,
    envelope: dict | None,
) -> JobOutcome:
    """退出码 + 信封 → ``(status, error_code, error_message)``（纯函数，可单测）。

    优先级：超时 > 取消 > 无信封 > 信封 ``ok`` + 退出码 0 = 成功 > 信封错误/退出码。
    """
    if timed_out:
        return JobOutcome(
            "failed",
            "timed_out",
            f"job 超过 {job_timeout_seconds(action):.0f}s 未结束，已终止整个进程组",
        )
    if cancel_requested:
        return JobOutcome("canceled", "canceled", "已按取消请求终止整个进程组")
    if envelope is None:
        return JobOutcome(
            "failed",
            "envelope_unparsed",
            "子进程 stdout 没有可解析的 JSON 信封（末行不是 JSON 对象）",
        )
    if envelope.get("ok") and exit_code == 0:
        return JobOutcome("succeeded", None, None)
    error = envelope.get("error")
    if isinstance(error, dict):
        code = error.get("code")
        message = error.get("message")
        return JobOutcome(
            "failed",
            str(code) if code else f"exit_{exit_code}",
            str(message) if message else f"子进程退出码 {exit_code}",
        )
    return JobOutcome(
        "failed", f"exit_{exit_code}", f"子进程退出码 {exit_code}（信封没有 error 块）"
    )


# --------------------------------------------------------------------------- #
# 调度与生命周期
# --------------------------------------------------------------------------- #
class _RunningJob:
    """一个正在跑的 job 的进程句柄（只存在于事件循环里）。"""

    __slots__ = (
        "capture",
        "monitor",
        "pgid",
        "plan",
        "popen",
        "profile",
        "record",
        "retranslate",
        "runs_before",
        "workdir",
    )

    def __init__(
        self,
        record: JobRecord,
        popen: subprocess.Popen,
        capture: PipeCapture,
        pgid: int,
        workdir: Path,
        profile: Profile,
        plan: compile_mod.CompilePlan | None = None,
        retranslate: candidates_mod.CandidatePlan | None = None,
    ) -> None:
        self.record = record
        self.popen = popen
        self.capture = capture
        self.pgid = pgid
        self.workdir = workdir
        #: 这次 spawn 用的 profile（收尾信封脱敏要用它的命令原文，见 sanitize_envelope）。
        self.profile = profile
        #: ``compile`` job 的隔离上下文（真 workdir / 副本 / 捕获的 revision）；
        #: 其它 action 为 ``None``。
        self.plan = plan
        #: ``retranslate`` job（W11 候选生成）的隔离上下文（真 workdir / 副本 /
        #: 候选行）；其它 action 为 ``None``。
        self.retranslate = retranslate
        #: spawn 之前已存在的 run 归档；之后新出现的那个才是这次 job 的 run。
        #: 隔离副本里的归档随副本一起删掉（compile / retranslate），所以不抓 run_id。
        self.runs_before = (
            frozenset()
            if plan is not None or retranslate is not None
            else frozenset(list_run_ids(workdir))
        )
        self.monitor: asyncio.Task | None = None


def new_run_id(workdir: Path, before: frozenset[str]) -> str | None:
    """spawn 之后新出现的 run 归档 id（最新者）；没有新 run → ``None``。"""
    fresh = [rid for rid in list_run_ids(workdir) if rid not in before]
    return fresh[0] if fresh else None


class JobRunner:
    """job 的提交 / 排队 / 监控 / 取消（一个 ``bdt serve`` 一个实例）。

    构造时会调 :meth:`JobRegistry.load`：上一次运行的 ``queued``/``running`` 在这里
    变成 ``interrupted``（**不自动重跑**，也不对孤立的 pid 发信号）。
    ``drafts`` 是草稿读写入口（``compile`` 在隔离副本里物化草稿要用）—— 和草稿路由
    共用同一个 :class:`~babeldoc_tools.serve.draft.DraftRegistry`，因此同一个 did
    只有一把草稿写锁。
    """

    def __init__(self, store: DocumentStore, *, glossary: GlossaryStore | None = None) -> None:
        self.store = store
        self.registry = JobRegistry(store.store_base)
        self.drafts = DraftRegistry(store)
        #: 全局词表存储（W13）：翻译 job 的注入路径就取自它。缺省自建一份；
        #: :func:`babeldoc_tools.serve.app.create_app` 传进来的那一份与 ``/glossary`` 路由
        #: 共用（同一个文件 + 同一个模块级写锁）。
        self.glossary = glossary or GlossaryStore(store.store_base)
        #: 候选存储（W11）：与候选路由共用同一个 registry，因此同一个 did 的候选行
        #: 只有一把写锁；job 收尾（填/删候选行）与 HTTP 采用/拒绝走的是同一把。
        self.candidates = candidates_mod.CandidateRegistry(store)
        self._running: dict[str, _RunningJob] = {}
        #: 本次启动被标记为 interrupted 的历史 job（启动日志/诊断用）。
        self.recovered: list[JobRecord] = self.registry.load()

    # ------------------------------------------------------------------ 读
    def get(self, job_id: str) -> JobRecord | None:
        """按 id 取 job；没有 → ``None``。"""
        return self.registry.get(job_id)

    def require(self, job_id: str) -> JobRecord:
        """按 id 取 job；没有 → ``ToolError(job_not_found)``（HTTP 404）。"""
        record = self.registry.get(job_id)
        if record is None:
            raise ToolError("job_not_found", f"job 不存在：{job_id}", job_id=job_id)
        return record

    def list_for_did(self, did: str, *, status: str | None = None) -> list[JobRecord]:
        """某文档的 job，新 → 旧；``status`` 可选过滤。"""
        return self.registry.list_for_did(did, status=status)

    # ---------------------------------------------------------------- 提交
    async def submit(
        self,
        *,
        did: str,
        action: str,
        from_stage: str | None,
        pages: str | None,
        dual: bool,
        profile_id: str | None,
        requested_scope: str | None = None,
        effective_scope: str | None = None,
        downgrade_reason: str | None = None,
        trigger: str | None = None,
        paragraph_id: str | None = None,
        candidate_id: str | None = None,
        use_glossary: bool = False,
        reviewer_profile: str | None = None,
    ) -> JobRecord:
        """建 job（``queued``）→ 尽量立刻启动；同文档已有活动 job → 409 语义。

        返回时的 ``status`` 可能是 ``running``（有空槽位就马上起了）—— 202 响应体里
        的 ``status`` 是契约冻结的 ``queued``（前端本来就该轮询 ``GET /jobs/{jid}``）。
        ``compile`` 不需要 provider（``profile_id=None``）：它只跑 apply+build；
        ``retranslate``（W11）必须带 ``paragraph_id`` + ``candidate_id``（候选已经建好）。
        ``use_glossary`` 只对 ``action=run`` 有意义（翻译阶段才注入），其余 action 一律记
        ``False`` —— 免得记录里出现一个实际不起作用的“已注入”。
        """
        # 先过 store 的路径边界（不在服务范围内/越界 → 400/404，不进队列）。
        self.store.resolve(did)
        if profile_id is not None and resolve_profile(
            self.store.store_base, profile_id
        ) is None:
            raise ToolError(
                "unknown_profile",
                f"未知 profile：{profile_id}",
                profile=profile_id,
                available=list_profile_ids(self.store.store_base),
            )
        if reviewer_profile is not None:
            selected = resolve_profile(self.store.store_base, profile_id or "")
            reviewer = resolve_profile(self.store.store_base, reviewer_profile)
            if not selected or not selected.model_profile or not reviewer or not reviewer.model_profile:
                raise ToolError("forbidden_field", "reviewer_profile requires model configurations")
        async with self.registry.lock:
            active = self.registry.active_for_did(did)
            if active is not None:
                raise ToolError(
                    "document_busy",
                    f"文档 {did} 已有活动 job：{active.job_id}（{active.status}）",
                    job_id=active.job_id,
                    status=active.status,
                    action=active.action,
                )
            record = self.registry.create(
                did=did,
                action=action,
                from_stage=from_stage,
                profile=profile_id,
                reviewer_profile=reviewer_profile,
                pages=pages,
                dual=dual,
                requested_scope=requested_scope,
                effective_scope=effective_scope,
                downgrade_reason=downgrade_reason,
                trigger=trigger,
                paragraph_id=paragraph_id,
                candidate_id=candidate_id,
                use_glossary=use_glossary,
            )
        await self._pump()
        return record

    async def cancel(self, job_id: str) -> JobRecord:
        """幂等取消，返回取消后的 job 快照。

        - ``queued``：没有子进程可杀，直接置 ``canceled``；
        - ``running``：``os.killpg`` SIGTERM → 宽限 → SIGKILL，**等进程退出**（监控任务
          收尾、释放文档槽）再返回；
        - 已终态：原样返回（不报错，也不改状态）；
        - ``running`` 但没有本进程的进程句柄（重启恢复留下的）：不发信号，如实置
          ``interrupted``（理由 ``process_not_owned``）。
        """
        self.require(job_id)
        async with self.registry.lock:
            record = self.require(job_id)
            if record.status == "queued":
                self.registry.mark_cancel_requested(record)
                canceled = self.registry.mark_finished(
                    record,
                    status="canceled",
                    error_code="canceled",
                    error_message="排队中取消：没有启动过子进程",
                )
                # 没启动就没有隔离副本/收尾，候选行得在这里删（否则永远挂一个"生成中"）。
                await self._drop_candidate(record)
                return canceled
        if record.status in TERMINAL_STATUSES:
            return record
        running = self._running.get(job_id)
        if running is None:
            # 没有句柄 = 两种可能：监控任务刚刚收尾（句柄在终态之后才摘掉），或者这条
            # running 记录不是本进程起的（重启恢复）。两种都**不发信号** —— 孤立 pid
            # 可能已经是别人的进程。前者以监控落的真实终态为准，后者如实置 interrupted。
            current = self.require(job_id)
            if (
                current.status in TERMINAL_STATUSES
                or current.boot_id == self.registry.boot_id
            ):
                return current
            return self.registry.mark_finished(
                record,
                status="interrupted",
                interrupted_reason="process_not_owned",
                error_code="process_not_owned",
                error_message="这个 job 的进程不是本进程起的（重启恢复），不发信号",
            )
        self.registry.mark_cancel_requested(record)
        await self._terminate(running)
        if running.monitor is not None:
            await running.monitor  # 等监控收尾：置 canceled + 释放文档槽都在那里
        return self.registry.get(job_id) or record

    async def _drop_candidate(self, record: JobRecord) -> None:
        """删掉该 job 的候选行（没启动过子进程时的收尾：不留半条候选）。"""
        if not record.candidate_id:
            return
        with contextlib.suppress(ToolError):
            await self.candidates.for_did(record.did).drop(record.candidate_id)

    # -------------------------------------------------------------- 调度
    def _glossary_path(self, record: JobRecord) -> str | None:
        """这个 job 该注入的词表 CSV 路径；不注入 → ``None``。

        四条规则（brief 红线，W13）：

        - 客户端没开 ``use_glossary`` → 不注入；
        - 只有 ``action=run`` 且真的会跑 translate 阶段（``from`` 缺省/parse/translate）
          才注入 —— ``compile``/``retranslate``/``check`` 以及 ``run --from apply`` 之后的
          起点都不跑翻译，注入毫无意义；**重译候选不注入**：它走 ``translator-repair``
          模板，候选要保持段落上下文自由；
        - 词表为空（文件不存在或没有条目）→ 不注入；
        - 路径恒为服务端自己的 ``<store_base>/.bdt-serve/glossary.csv``，客户端给不了。
        """
        if not record.use_glossary or record.action != "run":
            return None
        if (record.from_stage or "parse") not in ("parse", "translate"):
            return None
        return self.glossary.injection_path()

    async def _pump(self) -> None:
        """把排队的 job 尽量补进空槽（准入锁内：槽位判断与启动跨 await 原子）。

        ``_start`` 里 ``compile`` 要先建隔离副本（可能拷几百 MB），所以启动这一步是
        异步的（拷贝在 worker 线程里），整个准入锁在拷贝期间持有 —— 这本来就是
        "槽位已占" 的状态，别的提交本来就该等。
        """
        async with self.registry.lock:
            while len(self._running) < self.registry.max_concurrent:
                record = self.registry.pop_queued()
                if record is None:
                    return
                await self._start(record)

    async def _start(self, record: JobRecord) -> None:
        """启动 job：spawn 子进程 + 起监控任务；起不来就如实置终态（不进 running）。

        ``compile`` 走单独的启动路径（先快照/物化），前置失败不 spawn 子进程。
        """
        try:
            workdir = self.store.resolve(record.did)
        except ToolError as exc:
            self.registry.mark_finished(
                record,
                status="failed",
                error_code=exc.code,
                error_message=exc.message,
            )
            return
        if record.action == "compile":
            await self._start_compile(record, workdir)
            return
        if record.action == "retranslate":
            await self._start_retranslate(record, workdir)
            return
        try:
            profile = resolve_profile(self.store.store_base, record.profile or "")
            if profile is None:
                self.registry.mark_finished(
                    record,
                    status="failed",
                    error_code="unknown_profile",
                    error_message=f"profile 已不存在：{record.profile}",
                )
                return
            if record.reviewer_profile:
                reviewer = resolve_profile(self.store.store_base, record.reviewer_profile)
                if reviewer is None or not reviewer.model_profile:
                    raise ToolError("unknown_model", "Reviewer model configuration no longer exists")
                profile.reviewer = reviewer.translator
            argv = build_job_argv(
                workdir=workdir,
                action=record.action,
                from_stage=record.from_stage,
                pages=record.pages,
                dual=record.dual,
                profile=profile,
                glossaries=self._glossary_path(record),
            )
            proc = spawn_job(argv, workdir)
        except (ToolError, OSError) as exc:
            self.registry.mark_finished(
                record,
                status="failed",
                error_code="spawn_failed",
                error_message=f"无法启动子进程：{exc}",
            )
            return
        self._launch(record, proc, workdir, profile, argv)

    async def _start_compile(self, record: JobRecord, workdir: Path) -> None:
        """``compile`` 的启动：隔离副本（worker 线程里拷）→ spawn 子进程。

        准备阶段失败（找不到源 PDF / state.pkl 坏 / 草稿不合法）在路由层就是 202 之后
        的异步失败：**不 spawn**、真 workdir 未动，job 如实置 ``failed`` + error_code，
        并把同一个 error_code 写进 compile.json（详情端点也看得到）。
        """
        try:
            plan = await asyncio.to_thread(
                compile_mod.prepare_compile, record, workdir
            )
        except ToolError as exc:
            compile_mod.write_failed_state(
                workdir,
                error_code=exc.code,
                message=exc.message,
                revision_attempted=0,
                job_id=record.job_id,
                started_at=record.created_at,
            )
            self.registry.mark_finished(
                record,
                status="failed",
                error_code=exc.code,
                error_message=exc.message,
            )
            return
        except OSError as exc:
            compile_mod.write_failed_state(
                workdir,
                error_code="snapshot_failed",
                message=f"隔离副本创建失败：{exc}",
                revision_attempted=0,
                job_id=record.job_id,
                started_at=record.created_at,
            )
            self.registry.mark_finished(
                record,
                status="failed",
                error_code="snapshot_failed",
                error_message=f"隔离副本创建失败：{exc}",
            )
            return
        try:
            argv = compile_mod.build_compile_argv(plan.isolated)
            proc = spawn_job(argv, plan.isolated)
        except OSError as exc:
            compile_mod.discard_plan(
                plan,
                error_code="spawn_failed",
                message=f"无法启动子进程：{exc}",
            )
            self.registry.mark_finished(
                record,
                status="failed",
                error_code="spawn_failed",
                error_message=f"无法启动子进程：{exc}",
            )
            return
        self._launch(
            record,
            proc,
            workdir,
            Profile(id=compile_mod.COMPILE_PROFILE_ID),
            argv,
            plan=plan,
        )

    async def _start_retranslate(self, record: JobRecord, workdir: Path) -> None:
        """``retranslate``（W11 候选生成）的启动：隔离副本 → spawn ``bdt translate --ids``。

        与 ``run`` 的差别：translator 命令来自 profile（客户端只说 id），整个生成跑在
        副本里（``retranslate_blocks`` 会写 ``translated.md``／``prompt.retry.md``），
        真 workdir 分毫未动。任何前置失败都**不 spawn**、删副本、删候选行，job 如实置
        ``failed``（``queued`` 已回复，失败只能在 job 记录里看到）。
        """
        store = self.candidates.for_did(record.did)
        profile = resolve_profile(self.store.store_base, record.profile or "")
        if profile is None or not profile.translator:
            # 路由层已经校过；提交与启动之间 profile 被删/被改空 → 不 spawn，如实报错。
            await self._fail_retranslate_start(
                record,
                store,
                error_code="profile_missing",
                error_message=f"profile {record.profile} 已不存在或没有 translator 命令",
            )
            return
        try:
            plan = await asyncio.to_thread(
                candidates_mod.prepare_candidates, record, workdir, store
            )
        except ToolError as exc:
            await self._fail_retranslate_start(
                record, store, error_code=exc.code, error_message=exc.message
            )
            return
        except OSError as exc:
            await self._fail_retranslate_start(
                record,
                store,
                error_code="snapshot_failed",
                error_message=f"隔离副本创建失败：{exc}",
            )
            return
        argv = candidates_mod.build_candidates_argv(
            plan.isolated, pid=plan.pid, translator=profile.translator
        )
        try:
            # cwd = 副本：子进程里任何相对路径都落在隔离目录内（与 compile 同一口径）。
            # profile 的 translator 命令经 PUT /profiles 已落成绝对路径。
            proc = spawn_job(argv, plan.isolated)
        except OSError as exc:
            outcome = await candidates_mod.discard_plan(
                plan,
                error_code="spawn_failed",
                error_message=f"无法启动子进程：{exc}",
            )
            self.registry.mark_finished(
                record,
                status=outcome.status,
                error_code=outcome.error_code,
                error_message=outcome.error_message,
            )
            return
        self._launch(record, proc, workdir, profile, argv, retranslate=plan)

    async def _fail_retranslate_start(
        self,
        record: JobRecord,
        store: candidates_mod.CandidateStore,
        *,
        error_code: str,
        error_message: str,
    ) -> None:
        """候选生成没真正开始（不 spawn）：删候选行 + 如实置 ``failed``。

        与 :func:`~babeldoc_tools.serve.candidates.discard_plan` 的区别：那条路已经建了
        隔离副本（要一起删）；这里的副本根本还没建（或建完就删了）。
        """
        if record.candidate_id:
            with contextlib.suppress(ToolError):
                await store.drop(record.candidate_id)
        self.registry.mark_finished(
            record, status="failed", error_code=error_code, error_message=error_message
        )

    def _launch(
        self,
        record: JobRecord,
        proc: subprocess.Popen,
        workdir: Path,
        profile: Profile,
        argv: list[str],
        *,
        plan: compile_mod.CompilePlan | None = None,
        retranslate: candidates_mod.CandidatePlan | None = None,
    ) -> None:
        """给刚 spawn 的子进程接管道、记身份、起监控任务（argv 只用于指纹）。"""
        capture = capture_pipes(proc)
        pgid = os.getpgid(proc.pid)
        running = _RunningJob(
            record,
            proc,
            capture,
            pgid,
            workdir,
            profile,
            plan=plan,
            retranslate=retranslate,
        )
        self.registry.mark_started(
            record, pid=proc.pid, pgid=pgid, spawn_marker=argv_marker(argv)
        )
        running.monitor = asyncio.create_task(self._watch(running))
        self._running[record.job_id] = running

    async def _watch(self, running: _RunningJob) -> None:
        """等子进程退出 → 落终态 → 释放文档槽并让排队者上位（只释放一次）。

        ``compile`` job 的终态由 :func:`babeldoc_tools.serve.compile.settle_compile`
        判定（发布成功 = ``succeeded``）：它同时负责"发布或保留上一版"与清理隔离副本。
        """
        record = running.record
        try:
            exit_code, timed_out = await self._wait_exit(running)
            await running.capture.wait_closed(PIPE_JOIN_SECONDS)
            envelope = stdout_envelope(running.capture.stdout)
            if envelope is not None:
                # 先脱敏**解析结果**：classify_exit 会把 error.message 抄进 job 记录（可能
                # 回显整条命令），落库的那份必须已经是干净的。
                sanitize_payload(envelope[1], running.profile)
            canceled = record.cancel_requested_at is not None
            payload = envelope[1] if envelope else None
            if running.plan is not None:
                status, error_code, error_message = self._compile_outcome(
                    running.plan,
                    action=record.action,
                    envelope=payload,
                    exit_code=exit_code,
                    timed_out=timed_out,
                    canceled=canceled,
                )
            elif running.retranslate is not None:
                # 候选生成的账：终态判定仍走既有规则（classify_exit），候选侧（填/删
                # 候选行、删副本）由 settle_candidates 负责；取不到译文时它把这次 job
                # 记成 failed(candidate_missing)，不报一个空成功。
                base = classify_exit(
                    action=record.action,
                    cancel_requested=canceled,
                    timed_out=timed_out,
                    exit_code=exit_code,
                    envelope=payload,
                )
                settled = await candidates_mod.settle_candidates(
                    running.retranslate,
                    status=base.status,
                    error_code=base.error_code,
                    error_message=base.error_message,
                )
                status = settled.status
                error_code = settled.error_code
                error_message = settled.error_message
            else:
                outcome = classify_exit(
                    action=record.action,
                    cancel_requested=canceled,
                    timed_out=timed_out,
                    exit_code=exit_code,
                    envelope=payload,
                )
                status = outcome.status
                error_code = outcome.error_code
                error_message = outcome.error_message
            # 落盘前脱敏：信封可能带 profile 命令（含密钥）与 debug 查看器 token URL。
            text = (
                sanitize_envelope(envelope[0], running.profile) if envelope else None
            )
            self.registry.mark_finished(
                record,
                status=status,
                exit_code=exit_code,
                envelope=text,
                error_code=error_code,
                error_message=error_message,
                # compile 的 run 归档住在隔离副本里，随副本一起删 —— 不报一个
                # 已经不存在、而且是真 workdir 里别的 run 的 run_id。
                run_id=(
                    None
                    if running.plan is not None or running.retranslate is not None
                    else new_run_id(running.workdir, running.runs_before)
                ),
            )
        except Exception as exc:  # noqa: BLE001 - 监控自身出错也必须落终态
            self.registry.mark_finished(
                record,
                status="failed",
                error_code="monitor_failed",
                error_message=f"{type(exc).__name__}: {exc}",
            )
        finally:
            self._running.pop(record.job_id, None)
            await self._pump()

    def _compile_outcome(
        self,
        plan: compile_mod.CompilePlan,
        *,
        action: str,
        envelope: dict | None,
        exit_code: int | None,
        timed_out: bool,
        canceled: bool,
    ) -> tuple[str, str | None, str | None]:
        """``compile`` job 的 ``(status, error_code, error_message)``。

        发布成功（build ok 且副本里确实有新 PDF）→ ``succeeded``，即使子进程因
        check/review 质量门禁 exit 1 —— "编译成功 ≠ 质量通过"。未发布则按普通的退出码/
        信封规则如实报失败（信封里保留门禁/构建的 error_code）。
        """
        settled = compile_mod.settle_compile(
            plan,
            exit_code=exit_code,
            envelope=envelope,
            timed_out=timed_out,
            canceled=canceled,
        )
        if settled.published:
            return "succeeded", None, None
        outcome = classify_exit(
            action=action,
            cancel_requested=canceled,
            timed_out=timed_out,
            exit_code=exit_code,
            envelope=envelope,
        )
        if outcome.status == "succeeded":
            # 子进程自己说成功但没发布（build ok 却无产物）：不当作成功。
            return (
                "failed",
                settled.error_code or "build_output_missing",
                "build 阶段未产出可发布的 PDF（隔离副本 output/ 为空）",
            )
        return (
            outcome.status,
            settled.error_code or outcome.error_code,
            outcome.error_message,
        )

    async def _wait_exit(self, running: _RunningJob) -> tuple[int | None, bool]:
        """等退出；超时 → 走取消路径（杀进程组）并回报 ``timed_out``。

        用 ``popen.poll()`` 轮询而不是 ``await asyncio.to_thread(popen.wait)``：
        ``to_thread`` 占用事件循环的默认执行器，而 ``asyncio.run`` / anyio 的事件循环
        关闭会 ``shutdown_default_executor()`` **等所有执行器线程结束** —— 那会让
        Ctrl-C 或测试客户端退出在一个还在跑的 job 上卡到子进程自己退出。轮询没有这个
        耦合，退出码同样由 ``Popen`` 缓存（``poll`` 负责收尸，不会留僵尸）。
        """
        timeout = job_timeout_seconds(running.record.action)
        deadline = time.monotonic() + timeout
        while True:
            exit_code = running.popen.poll()
            if exit_code is not None:
                return exit_code, False
            if time.monotonic() >= deadline:
                return await self._terminate(running), True
            await asyncio.sleep(EXIT_POLL_SECONDS)

    async def _terminate(self, running: _RunningJob) -> int | None:
        """SIGTERM 整个进程组 → 宽限 → SIGKILL；返回子进程退出码（拿不到就 ``None``）。

        两段等待都有上限：SIGKILL 对普通进程不可屏蔽，但卡在不可中断 IO 的进程可能
        超过宽限；那时不再无限等（job 仍按 canceled/timed_out 如实收尾，不假装进程
        一定已死）。
        """
        _kill_group(running.pgid, signal.SIGTERM)
        deadline = time.monotonic() + CANCEL_GRACE_SECONDS
        while running.popen.poll() is None and time.monotonic() < deadline:
            await asyncio.sleep(CANCEL_POLL_SECONDS)
        if running.popen.poll() is None:
            _kill_group(running.pgid, signal.SIGKILL)
        deadline = time.monotonic() + CANCEL_GRACE_SECONDS
        while running.popen.poll() is None and time.monotonic() < deadline:
            await asyncio.sleep(CANCEL_POLL_SECONDS)
        return running.popen.returncode
