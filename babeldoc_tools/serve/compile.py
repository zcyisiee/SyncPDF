"""草稿编译：隔离副本 → apply+build → 原子发布（``docs/frontend/api.md`` §3.2/§3.4，W09）。

一次 compile 的生命周期（每一步都可失败，失败**绝不**碰真 workdir 的产物）::

    .bdt-serve/compile-<job_id>/          ① 快照（copytree 排除 .bdt-serve/debug/output，
       agent/ 全套产物 + 源 PDF 副本         latex_cache 用目录级 symlink 复用真缓存）
       agent/translated.md                 ② 物化草稿（merge_translated_markdown +
       agent/layout_overrides.json             layout_overrides.apply_patch）
       agent/state.pkl（temp_pdf_path 重指）
       output/*.pdf                         ③ mv 回真 workdir output/（同名 os.replace）
                                            ④ 无论成败删掉隔离目录

判据（主控批准的语义）：**信封 ``data.stages`` 里 ``build`` 阶段 ok 且副本
``output/`` 确实有新 PDF** 才算编译成功。``bdt run`` 从 apply 起会一路走到
check/review/report，而 check/review 是质量门禁不是编译：检查不过时进程 exit 1、
信封 ``ok=false``，但 build 已经产出新 PDF —— 那种情况仍然发布并把
``compile.json.status`` 记 ``ok``；门禁结论留在 job envelope 里，``pipeline_ok``
一律不动（"编译成功 ≠ 质量通过"，EXECUTION.md 纠偏 8）。两条判据必须同时满足：
信封解析失败即使副本里有 PDF 也**不发布**（宁可留住上一版）。

``compile.json`` 的 ``revision`` / ``artifact`` **恒指最近一次成功发布**：
``status``/``error_code`` 描述最近一次尝试，``stale = draft.revision > revision``
由详情端点算（见 :func:`compile_status`）。所以编译失败时旧 PDF 仍在、仍可下载，
只是被标成 failed + stale。

这里不 import :mod:`babeldoc_tools.serve.runner`（它会反过来 import 本模块）；
:class:`CompileService` 通过构造参数拿到 runner，只调它的 ``submit``。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import pickle
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING
from typing import Any
from typing import NamedTuple

from babeldoc.tools.agent import layout_overrides
from babeldoc.tools.agent import markdown_view

from babeldoc_tools.common import ToolError
from babeldoc_tools.serve.draft import DraftRegistry
from babeldoc_tools.serve.draft import read_draft
from babeldoc_tools.serve.jobs import JobRecord
from babeldoc_tools.serve.jobs import utc_now
from babeldoc_tools.serve.schemas import CompileStatus
from babeldoc_tools.serve.store import STATE_DIR
from babeldoc_tools.serve.store import DocumentStore
from babeldoc_tools.translate import merge_translated_markdown

if TYPE_CHECKING:
    from babeldoc_tools.serve.draft import DraftDoc
    from babeldoc_tools.serve.runner import JobRunner

__all__ = [
    "BUILD_STAGE",
    "CACHE_DIR_NAME",
    "COMPILE_DIR_PREFIX",
    "COMPILE_FILE",
    "COMPILE_FROM_STAGE",
    "DEBOUNCE_SECONDS",
    "CompileOutcome",
    "CompilePlan",
    "CompileService",
    "build_compile_argv",
    "compile_state_path",
    "compile_status",
    "discard_plan",
    "draft_body_to_markdown",
    "prepare_compile",
    "read_compile_state",
    "relocate_source_pdf",
    "build_stage_ok",
    "resolve_source_pdf",
    "settle_compile",
    "sweep_isolated",
    "write_compile_state",
    "write_failed_state",
]

#: ``<workdir>/.bdt-serve/compile.json``（编译状态，详情端点的 ``compile`` 字段真源）。
COMPILE_FILE = "compile.json"
#: 隔离副本目录名前缀（``<workdir>/.bdt-serve/compile-<job_id>/``）。
COMPILE_DIR_PREFIX = "compile-"
#: compile 的起点阶段（``bdt run --from apply``：apply 写回 IR + build 出 PDF）。
COMPILE_FROM_STAGE = "apply"
#: 快照后要判定成败的阶段名。
BUILD_STAGE = "build"
#: build 产物目录（``layout.build_pdf`` 缺省 ``output_dir = <workdir>/output``，已核实）。
BUILD_OUTPUT_DIR = "output"
#: ``agent/`` 目录名与 IR 状态文件名（编译需要它们算输出文件名与源 PDF 目录）。
AGENT_DIR_NAME = "agent"
STATE_PICKLE = "state.pkl"
#: 源 PDF 在副本内的落地名（放在 ``<副本>/<pdf stem>/`` 下 —— 必须与真 workdir 的
#: 布局一致，因为 ``TranslationConfig`` 用 ``<workdir>/<pdf stem>/`` 当工作目录，
#: 而输出文件名取自 ``state["pdf_path"]`` 的 stem：改 pdf_path 会改产物名）。
SOURCE_PDF_NAME = "input.pdf"
#: XeLaTeX 贴片缓存目录名（与 ``babeldoc...latex_bbox.stamp_cache.CACHE_DIR_NAME``
#: 一致；不 import 那个模块是为了避免 serve 启动时拉起排版/渲染依赖）。
CACHE_DIR_NAME = "latex_cache"
#: 快照排除的顶层名字（``.bdt-serve`` = 服务自己的状态；``debug`` = run 归档；
#: ``output`` = 旧产物，build 会在副本里重建）。
SNAPSHOT_SKIP = (STATE_DIR, "debug", BUILD_OUTPUT_DIR)

#: 草稿保存后的服务端防抖窗口（EXECUTION.md 纠偏 7）；测试 monkeypatch 这个模块属性。
DEBOUNCE_SECONDS = 1.5

#: compile job 用的 profile 占位（compile 不需要 provider，但信封脱敏要有 Profile）。
COMPILE_PROFILE_ID = "compile"

#: 详情 ``compile.status`` 的取值域（api.md §3.2）。
_COMPILE_STATUSES = ("none", "running", "ok", "failed")


# --------------------------------------------------------------------------- #
# compile.json 读写
# --------------------------------------------------------------------------- #
def compile_state_path(workdir: Path | str) -> Path:
    """``<workdir>/.bdt-serve/compile.json``。"""
    return Path(workdir) / STATE_DIR / COMPILE_FILE


def read_compile_state(workdir: Path | str) -> dict | None:
    """读编译状态；缺失 / 坏 JSON / 不是对象 → ``None``（不猜）。"""
    try:
        payload = json.loads(compile_state_path(workdir).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def write_compile_state(workdir: Path | str, payload: dict) -> Path:
    """原子写编译状态（同目录 tmp + ``os.replace``）。"""
    path = compile_state_path(workdir)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    tmp.replace(path)
    return path


def _last_published(workdir: Path | str) -> tuple[int, dict | None]:
    """上一次成功发布的 ``(revision, artifact)``；没有 → ``(0, None)``。"""
    previous = read_compile_state(workdir) or {}
    revision = previous.get("revision")
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        revision = 0
    artifact = previous.get("artifact")
    return revision, artifact if isinstance(artifact, dict) else None


def compile_status(workdir: Path | str) -> CompileStatus:
    """详情端点的 ``compile`` 字段（api.md §3.2）：compile.json + draft revision。

    ``revision``/``artifact`` 是最近一次**成功发布**的那份；``status``/``failed``
    描述最近一次尝试。``stale = 当前草稿 revision > 已编译 revision`` —— 草稿一动，
    可下载的 PDF 就自动被标成旧版（前端必须显式提示，不得当成最新）。
    """
    payload = read_compile_state(workdir)
    draft_revision = read_draft(workdir).revision
    if payload is None:
        return CompileStatus(stale=draft_revision > 0)
    status = payload.get("status")
    if status not in _COMPILE_STATUSES or status == "none":
        status = "none"
    revision, artifact = _last_published(workdir)
    return CompileStatus(
        status=status,
        revision=revision,
        stale=draft_revision > revision,
        artifact=artifact,
    )


# --------------------------------------------------------------------------- #
# 源 PDF 重指（纯函数，可单测）
# --------------------------------------------------------------------------- #
def relocate_source_pdf(state: dict, source_pdf: Path) -> dict:
    """把 IR 状态里的源 PDF 引用重指到副本内的路径（返回新 dict，不改入参）。

    **只改 ``temp_pdf_path``**：build 用它在 ``latex_source_pdf_path`` /
    ``PDFCreater`` 里读源 PDF；``pdf_path`` 必须保持原样，因为产物文件名取自它的
    stem（``pdf_creater.write`` 用 ``Path(config.input_file).stem``），改了就会把
    ``paper.mono.pdf`` 发布成另一个名字。
    """
    moved = dict(state)
    moved["temp_pdf_path"] = str(source_pdf)
    return moved


def resolve_source_pdf(
    state: dict,
    workdir: Path | str,
    *,
    cwd: Path | str | None = None,
) -> Path | None:
    """state 记录的源 PDF → 磁盘上真实存在的绝对路径；找不到 → ``None``。

    候选顺序（老 workdir 的记录是**相对 parse 时 cwd** 的路径，不能只按一个基准解释）：

    1. 记录值本身：绝对路径，或相对 ``cwd``（缺省 = 当前进程 cwd —— 本地 serve 通常
       就从仓库根启动，而历史 run 也是从那里跑的）；
    2. ``<workdir>/<记录值>``（parse 时 cwd 就是 workdir 的那种 run）；
    3. ``<workdir>/<pdf stem>/input.pdf`` 与 ``<workdir>/input.pdf``（
       ``TranslationConfig`` 的工作目录约定）。
    """
    workdir = Path(workdir)
    base = Path(cwd) if cwd is not None else Path.cwd()
    candidates: list[Path] = []
    recorded = state.get("temp_pdf_path")
    if isinstance(recorded, str) and recorded:
        path = Path(recorded)
        if path.is_absolute():
            candidates.append(path)
        else:
            candidates.append(base / path)
            candidates.append(workdir / path)
    pdf_path = state.get("pdf_path")
    if isinstance(pdf_path, str) and pdf_path:
        stem = Path(pdf_path).stem
        if stem:
            candidates.append(workdir / stem / SOURCE_PDF_NAME)
    candidates.append(workdir / SOURCE_PDF_NAME)
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate.resolve()
        except OSError:
            continue
    return None


def _source_pdf_destination(isolated: Path, pdf_path: Any) -> Path:
    """副本内源 PDF 的落地路径：``<副本>/<pdf stem>/input.pdf``。

    与 ``TranslationConfig`` 的 ``working_dir = <workdir>/<input stem>`` 一致，
    也让 ``state["temp_pdf_path"]`` 与 build 期望的目录对得上。
    """
    stem = Path(str(pdf_path)).stem if isinstance(pdf_path, str) and pdf_path else ""
    return isolated / (stem or "input") / SOURCE_PDF_NAME


# --------------------------------------------------------------------------- #
# 快照
# --------------------------------------------------------------------------- #
def sweep_isolated(workdir: Path | str) -> list[str]:
    """清掉上一次残留的隔离副本（服务崩溃/被 kill 留下的）；返回被删的目录名。"""
    base = Path(workdir) / STATE_DIR
    removed: list[str] = []
    with contextlib.suppress(OSError):
        for entry in base.glob(f"{COMPILE_DIR_PREFIX}*"):
            if not entry.is_dir():
                continue
            shutil.rmtree(entry, ignore_errors=True)
            removed.append(entry.name)
    return removed


def _snapshot_ignore(directory: str, names: list[str]) -> list[str]:
    """copytree 的 ignore：排除状态/归档/旧产物与 ``latex_cache``（后者改用 symlink）。"""
    ignored: list[str] = []
    for name in names:
        if name in SNAPSHOT_SKIP:
            ignored.append(name)
        elif name == CACHE_DIR_NAME and (Path(directory) / name).is_dir():
            ignored.append(name)
    return ignored


def _link_cache_dirs(workdir: Path, isolated: Path) -> list[str]:
    """把 ``latex_cache`` 目录在副本里做成指向真 workdir 的 symlink。

    5.4G 的 XeLaTeX 贴片缓存每编译一次全量拷贝不可接受；缓存是内容寻址 +
    tmp/os.replace 写入（只新增文件，从不原地改写），因此共享既安全又能跨副本命中。
    真 workdir 里没有这个目录时跳过（build 会在副本里新建）。
    """
    linked: list[str] = []
    for source in sorted(workdir.rglob(CACHE_DIR_NAME)):
        if not source.is_dir() or source.is_symlink():
            continue
        relative = source.relative_to(workdir)
        target = isolated / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            target.symlink_to(source.resolve(), target_is_directory=True)
            linked.append(str(relative))
    return linked


# --------------------------------------------------------------------------- #
# argv 与物化
# --------------------------------------------------------------------------- #
def build_compile_argv(isolated: Path) -> list[str]:
    """compile 的唯一 argv 来源：``bdt run --from apply``（apply → build → …）。

    入口固定 ``sys.executable -m babeldoc_tools run``（模块入口，不依赖 PATH 上的
    ``bdt``）。**不带** provider 参数：compile 不调翻译/审查。**不带** ``--debug``：
    ``--debug`` 会在副本里拉起一个 detached 的 debug 查看器并且把 run 归档写进马上
    要被删掉的隔离目录（归档/查看器都会成为无主残留），而编译诊断（阶段结果、
    error_code、信封）本来就在 job envelope 里。``--from apply`` 之后的
    check/review/report 由 CLI 自己按质量门禁决定成败，:func:`settle_compile`
    只看 build 阶段。
    """
    return [
        sys.executable,
        "-m",
        "babeldoc_tools",
        "run",
        "--workdir",
        str(isolated),
        "--from",
        COMPILE_FROM_STAGE,
    ]


def draft_body_to_markdown(target: str) -> str:
    """草稿 ``target``（canonical 形式，与 ``/paragraphs`` 同一表示）→ markdown 锚点形式。

    ``translated.jsonl`` / ``GET /paragraphs`` 里的译文是 **canonical IR 占位符**
    （``<style id='1'>`` / ``</style>`` / ``{v3}``），而 ``agent/translated.md`` 存的是
    等价的**短锚点**（``[[S1]]`` / ``[[/S1]]`` / ``[[F3]]``）—— 两者一一映射，由
    :func:`babeldoc.tools.agent.markdown_view.canonical_to_markdown` 互转（同时做
    断词/空格类文本修复）。草稿一律存 canonical（前端看到的就是 ``/paragraphs`` 的值），
    写回 ``translated.md`` 前在这里转一次；已经是锚点形式的文本原样通过（幂等）。
    """
    return markdown_view.canonical_to_markdown(target)


def _materialize_draft(isolated: Path, doc: DraftDoc) -> int:
    """把草稿写进隔离副本：译文（translated.md）+ 排版（layout_overrides.json）。

    译文走 ``translate.merge_translated_markdown``（唯一实现：按 sheet 顺序重排合并），
    label 留空 → 该函数自己从 ``anchors.json`` 取（不在这里复制那份逻辑）；body 先过
    :func:`draft_body_to_markdown`（canonical → 短锚点）。
    排版走 ``layout_overrides.apply_patch``（复用它的键名/范围校验与 history），
    叠加在工作区既有覆盖之上：草稿没有提到的段落保持原样。
    """
    targets = {
        pid: (draft_body_to_markdown(para.target), "")
        for pid, para in doc.paragraphs.items()
        if para.target is not None
    }
    merge_translated_markdown(isolated, targets)
    patches = {
        pid: para.layout for pid, para in doc.paragraphs.items() if para.layout
    }
    if patches:
        result = layout_overrides.apply_patch(
            isolated,
            {"version": layout_overrides.VERSION, "paragraphs": patches},
            reason="bdt serve draft",
        )
        if not result.get("ok"):
            raise ToolError(
                "draft_invalid",
                "草稿的排版覆盖不合法，已中止编译",
                errors=list(result.get("errors") or []),
            )
    return len(targets) + len(patches)


# --------------------------------------------------------------------------- #
# 一次 compile 的准备与收尾
# --------------------------------------------------------------------------- #
@dataclass
class CompilePlan:
    """一次 compile 的隔离上下文（内存态；终态后随 job 句柄释放）。"""

    job_id: str
    did: str
    #: 真 workdir（发布目标）。
    workdir: Path
    #: 隔离副本（编译实际发生的地方）。
    isolated: Path
    #: 这次编译捕获的草稿 revision（写进 compile.json）。
    revision: int
    #: 物化的段落数（诊断用）。
    materialized: int
    #: 副本内的源 PDF 路径。
    source_pdf: Path
    #: 复用到的 latex_cache 相对路径（诊断用）。
    cache_dirs: tuple[str, ...]
    started_at: str
    started_monotonic: float


def prepare_compile(record: JobRecord, workdir: Path | str) -> CompilePlan:
    """建隔离副本 + 物化草稿 + 重指源 PDF；失败会清掉副本并抛 ``ToolError``。

    同步、纯文件操作（调用方放进线程跑，别卡事件循环）。前置失败（找不到源 PDF、
    state.pkl 缺失/损坏）**不 spawn 子进程**：编译没开始，真 workdir 分毫未动。
    """
    workdir = Path(workdir)
    sweep_isolated(workdir)
    isolated = workdir / STATE_DIR / f"{COMPILE_DIR_PREFIX}{record.job_id}"
    try:
        shutil.copytree(workdir, isolated, ignore=_snapshot_ignore)
        cache_dirs = _link_cache_dirs(workdir, isolated)
        doc = read_draft(workdir)
        materialized = _materialize_draft(isolated, doc)
        state_file = isolated / AGENT_DIR_NAME / STATE_PICKLE
        if not state_file.is_file():
            raise ToolError(
                "source_pdf_missing",
                f"隔离副本缺 {AGENT_DIR_NAME}/{STATE_PICKLE}：没有可编译的 IR 状态",
                workdir=str(workdir),
            )
        try:
            with state_file.open("rb") as handle:
                state = pickle.load(handle)  # noqa: S301 - workdir 私有产物
        except Exception as exc:  # noqa: BLE001 - 坏 state 一律前置失败
            raise ToolError(
                "source_pdf_missing",
                f"{AGENT_DIR_NAME}/{STATE_PICKLE} 不可读：{type(exc).__name__}: {exc}",
                workdir=str(workdir),
            ) from exc
        if not isinstance(state, dict):
            raise ToolError(
                "source_pdf_missing",
                f"{AGENT_DIR_NAME}/{STATE_PICKLE} 不是 IR 状态字典",
                workdir=str(workdir),
            )
        source = resolve_source_pdf(state, workdir)
        if source is None:
            raise ToolError(
                "source_pdf_missing",
                "state.pkl 记录的源 PDF 找不到（编译需要它做 LaTeX 贴片与版面量测）",
                recorded=state.get("temp_pdf_path"),
            )
        destination = _source_pdf_destination(isolated, state.get("pdf_path"))
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.resolve() != source:
            shutil.copy2(source, destination)
        with state_file.open("wb") as handle:
            pickle.dump(relocate_source_pdf(state, destination), handle)
        plan = CompilePlan(
            job_id=record.job_id,
            did=record.did,
            workdir=workdir,
            isolated=isolated,
            revision=doc.revision,
            materialized=materialized,
            source_pdf=destination,
            cache_dirs=tuple(cache_dirs),
            started_at=utc_now(),
            started_monotonic=time.monotonic(),
        )
    except BaseException:
        shutil.rmtree(isolated, ignore_errors=True)
        raise
    # 状态先置 running：详情端点在整个编译期间都能看到"正在编译"（api.md §3.2）。
    revision, artifact = _last_published(workdir)
    write_compile_state(
        workdir,
        {
            "status": "running",
            "revision": revision,
            "artifact": artifact,
            "revision_attempted": plan.revision,
            "job_id": record.job_id,
            "started_at": plan.started_at,
        },
    )
    return plan


class CompileOutcome(NamedTuple):
    """一次 compile 的收尾结论（job 终态与 compile.json 都由它决定）。"""

    published: bool
    status: str
    revision: int
    artifact: dict | None
    error_code: str | None


def build_stage_ok(envelope: dict | None) -> bool:
    """信封 ``data.stages`` 里有没有 ``build`` 阶段 ``status=ok``（容忍 ``ok=false``）。

    进程可能因为 check/review 门禁 exit 1 而信封 ``ok=false``，但 ``data.stages``
    仍然完整 —— 这正是"编译成功、质量未过"的可观测证据。
    """
    if not isinstance(envelope, dict):
        return False
    data = envelope.get("data")
    if not isinstance(data, dict):
        return False
    stages = data.get("stages")
    if not isinstance(stages, list):
        return False
    for entry in stages:
        if not isinstance(entry, dict):
            continue
        if entry.get("stage") == BUILD_STAGE and entry.get("status") == "ok":
            return True
    return False


def _envelope_error(envelope: dict | None) -> tuple[str | None, str | None]:
    """信封 ``error`` 块的 ``(code, message)``；没有 → ``(None, None)``。"""
    if not isinstance(envelope, dict):
        return None, None
    error = envelope.get("error")
    if not isinstance(error, dict):
        return None, None
    code = error.get("code")
    message = error.get("message")
    return (
        str(code) if code is not None else None,
        str(message) if message is not None else None,
    )


def write_failed_state(
    workdir: Path | str,
    *,
    error_code: str,
    message: str | None,
    revision_attempted: int,
    job_id: str,
    started_at: str,
) -> CompileOutcome:
    """记一次失败的编译尝试；``revision``/``artifact`` 保留上一版（旧 PDF 仍可下载）。"""
    revision, artifact = _last_published(workdir)
    write_compile_state(
        workdir,
        {
            "status": "failed",
            "revision": revision,
            "artifact": artifact,
            "revision_attempted": revision_attempted,
            "job_id": job_id,
            "error_code": error_code,
            "error_message": message,
            "started_at": started_at,
            "finished_at": utc_now(),
        },
    )
    return CompileOutcome(False, "failed", revision, artifact, error_code)


def _write_failure(
    plan: CompilePlan,
    *,
    error_code: str,
    message: str | None,
) -> CompileOutcome:
    """按隔离上下文记失败（:func:`write_failed_state` 的薄封装）。"""
    return write_failed_state(
        plan.workdir,
        error_code=error_code,
        message=message,
        revision_attempted=plan.revision,
        job_id=plan.job_id,
        started_at=plan.started_at,
    )


def _publish(plan: CompilePlan, pdfs: list[Path]) -> dict:
    """把副本 ``output/*.pdf`` 挪回真 workdir（同名 ``os.replace`` = 原子替换）。"""
    target_dir = plan.workdir / BUILD_OUTPUT_DIR
    target_dir.mkdir(parents=True, exist_ok=True)
    for source in pdfs:
        source.replace(target_dir / source.name)
    primary = next(
        (path for path in pdfs if ".mono." in path.name), pdfs[0]
    )
    published = target_dir / primary.name
    artifact = {
        "name": primary.name,
        "size": published.stat().st_size,
        "revision": plan.revision,
    }
    write_compile_state(
        plan.workdir,
        {
            "status": "ok",
            "revision": plan.revision,
            "artifact": artifact,
            "job_id": plan.job_id,
            "started_at": plan.started_at,
            "finished_at": utc_now(),
            "duration_s": round(time.monotonic() - plan.started_monotonic, 2),
            "files": sorted(path.name for path in pdfs),
        },
    )
    return artifact


def settle_compile(
    plan: CompilePlan,
    *,
    exit_code: int | None,
    envelope: dict | None,
    timed_out: bool,
    canceled: bool,
) -> CompileOutcome:
    """收尾一次 compile：判定成败 → 发布或保留上一版 → 删隔离副本。

    优先级：取消 > 超时 > （build ok 且副本有新 PDF）发布 > 失败。取消/超时/失败
    一律只删副本（真 workdir 一个字节都不动）。任何内部异常都转成
    ``publish_failed`` 的失败态，绝不让异常冒泡成"job 卡在 running"。
    """
    try:
        if canceled:
            return _write_failure(
                plan, error_code="canceled", message="编译已取消：隔离副本已删除"
            )
        if timed_out:
            return _write_failure(
                plan,
                error_code="timed_out",
                message="编译超时：已终止子进程组，隔离副本已删除",
            )
        if not build_stage_ok(envelope):
            code, message = _envelope_error(envelope)
            if envelope is None:
                code, message = "envelope_unparsed", "子进程 stdout 没有可解析的信封"
            return _write_failure(
                plan,
                error_code=code or "build_failed",
                message=message
                or f"build 阶段未成功（子进程退出码 {exit_code}）",
            )
        pdfs = sorted((plan.isolated / BUILD_OUTPUT_DIR).glob("*.pdf"))
        if not pdfs:
            # 信封说 build ok 但副本里没有 PDF：不发布（判据必须两条同时满足）。
            return _write_failure(
                plan,
                error_code="build_output_missing",
                message=f"build 阶段 ok，但副本 {BUILD_OUTPUT_DIR}/ 下没有 PDF",
            )
        artifact = _publish(plan, pdfs)
        return CompileOutcome(True, "ok", plan.revision, artifact, None)
    except Exception as exc:  # noqa: BLE001 - 收尾自身出错也必须落终态
        with contextlib.suppress(Exception):
            return _write_failure(
                plan,
                error_code="publish_failed",
                message=f"{type(exc).__name__}: {exc}",
            )
        return CompileOutcome(False, "failed", 0, None, "publish_failed")
    finally:
        shutil.rmtree(plan.isolated, ignore_errors=True)


def discard_plan(
    plan: CompilePlan, *, error_code: str, message: str | None
) -> CompileOutcome:
    """编译**没真正开始**（如 spawn 失败）：记失败状态 + 删副本，不动真 workdir。"""
    try:
        return _write_failure(plan, error_code=error_code, message=message)
    finally:
        shutil.rmtree(plan.isolated, ignore_errors=True)


# --------------------------------------------------------------------------- #
# 服务：防抖 + 提交
# --------------------------------------------------------------------------- #
def _log(message: str) -> None:
    """防抖/编译日志走 stderr（stdout 是 ``bdt`` 的单行 JSON 约定，不动）。"""
    sys.stderr.write(f"bdt serve: {message}\n")
    sys.stderr.flush()


class CompileService:
    """草稿编译的调度面：防抖计时器 + ``action=compile`` 的提交。

    防抖规则（EXECUTION.md 纠偏 7）：草稿写成功 → 1.5s 后若无活动 job 就自动编译；
    期间再写一次就重置计时器；到点时已有活动 job 则**跳过**（不重复编译，人工/显式
    compile 优先）。定时器是纯服务端 ``asyncio.Task``，浏览器断开不影响；
    serve 重启后定时器丢失（下次写草稿再触发）——内存态定时器不做持久化。
    """

    def __init__(self, store: DocumentStore, runner: JobRunner) -> None:
        self.store = store
        self.runner = runner
        self.drafts: DraftRegistry = runner.drafts
        #: did → 未触发的防抖任务（只在该事件循环里读写）。
        self._timers: dict[str, asyncio.Task] = {}
        self.reconcile()

    # ------------------------------------------------------------ 生命周期
    def reconcile(self) -> list[str]:
        """启动核对：上次进程留在 ``running`` 的编译状态 → ``failed``。

        serve 重启后所有非终态 job 都被 :class:`~babeldoc_tools.serve.jobs.JobRegistry`
        改成 ``interrupted``（不自动重跑），编译状态文件要跟着落定，否则详情端点会
        永远显示"正在编译"。没有需要改的就不写盘。
        """
        fixed: list[str] = []
        try:
            dids = self.store.list_dids()
        except ToolError:
            return fixed
        for did in dids:
            try:
                workdir = self.store.resolve(did)
            except ToolError:
                continue
            state = read_compile_state(workdir)
            if state is None or state.get("status") != "running":
                continue
            write_failed_state(
                workdir,
                error_code="server_restart",
                message="serve 重启：不在跑着的编译一律不自动重跑，请重新编译",
                revision_attempted=state.get("revision_attempted") or 0,
                job_id=str(state.get("job_id") or ""),
                started_at=str(state.get("started_at") or utc_now()),
            )
            sweep_isolated(workdir)
            fixed.append(did)
        if fixed:
            _log(f"启动核对：{len(fixed)} 个中断的编译状态已置 failed：{fixed}")
        return fixed

    def shutdown(self) -> None:
        """取消未触发的防抖任务（serve 生命周期结束；到点没跑的编译不补跑）。"""
        for did, task in list(self._timers.items()):
            if not task.done():
                task.cancel()
                _log(f"关停：取消 {did} 未触发的防抖编译")
        self._timers.clear()

    # -------------------------------------------------------------- 防抖
    def schedule(self, did: str) -> None:
        """草稿写成功后重置该 did 的防抖计时器（1.5s 后自动编译）。"""
        previous = self._timers.pop(did, None)
        if previous is not None and not previous.done():
            previous.cancel()
        task = asyncio.get_running_loop().create_task(self._debounced(did))
        self._timers[did] = task
        task.add_done_callback(lambda done, key=did: self._forget(key, done))

    def _forget(self, did: str, task: asyncio.Task) -> None:
        """任务结束后摘掉登记（只摘自己，别误删后建的新计时器）。"""
        if self._timers.get(did) is task:
            self._timers.pop(did, None)

    async def _debounced(self, did: str) -> None:
        """等过防抖窗口，然后（无活动 job 时）自动提交一次全量编译。"""
        await asyncio.sleep(DEBOUNCE_SECONDS)
        active = self.runner.registry.active_for_did(did)
        if active is not None:
            _log(f"防抖跳过 {did}：已有活动 job {active.job_id}（{active.status}）")
            return
        try:
            record = await self.request_compile(did, scope="full")
        except ToolError as exc:
            _log(f"防抖编译未提交 {did}：{exc.code} {exc.message}")
            return
        _log(f"防抖触发 {did}：compile job {record.job_id}（revision 随草稿捕获）")

    # -------------------------------------------------------------- 提交
    async def request_compile(
        self,
        did: str,
        *,
        scope: str = "full",
        base_revision: int | None = None,
    ) -> JobRecord:
        """建一个 compile job（同文档串行、全局限流由 :class:`JobRunner` 管）。

        ``base_revision``（显式 POST 可带）与当前草稿不一致 → ``409 revision_conflict``：
        请求要编译的那一版已经不是最新版，宁可让前端刷新重试。v1 的页级编译按已批准
        设计**回退全量**，把 requested/effective/reason 记进 job 记录（api.md §3.4）。
        """
        self.store.resolve(did)
        draft = self.drafts.for_did(did).read()
        if base_revision is not None and base_revision != draft.revision:
            raise ToolError(
                "revision_conflict",
                f"base_revision={base_revision} 与当前草稿 revision={draft.revision} 不符",
                current_revision=draft.revision,
                base_revision=base_revision,
            )
        requested = scope or "full"
        effective = "full"
        reason = (
            None
            if requested == "full"
            else "v1 页级编译按已批准设计回退全量（requested_scope=pages）"
        )
        return await self.runner.submit(
            did=did,
            action="compile",
            from_stage=COMPILE_FROM_STAGE,
            pages=None,
            dual=False,
            profile_id=None,
            requested_scope=requested,
            effective_scope=effective,
            downgrade_reason=reason,
        )
