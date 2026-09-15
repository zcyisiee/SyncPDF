"""``bdt run``：把各阶段串成一条可续跑的编排。

阶段顺序::

    parse → translate → apply → build → check → review → report

设计要点（U2 Part 2；reviewer 调用由 U3 注入）：

- **不做状态机**：一个显式阶段元组 + 一个 ``agent/run_state.json`` 私有状态文件。
- **阶段产物校验**：每个要执行的阶段先检查所需 artifact 是否存在，缺失则以
  ``missing_artifact`` 结构化错误退出（exit 1），并指明该跑哪一步；绝不靠"跳过"
  制造成功。
- **哈希失效**：``run_state.json`` 记录每个阶段运行时的输入 sha256（
  ``document.md`` / ``translated.md`` / ``layout_overrides.json`` / PDF / 外部
  ``--markdown``）。``--from`` 续跑时，若被跳过阶段的输入哈希与记录不符，说明上游
  产物被改过，**报错并给出明确指引**（不静默沿用旧产物，也不静默回退长跑）。
- 任一步失败：以该步错误 JSON 退出，已完成步骤的产物留在 workdir 供续跑。

**停止语义（U3）**：``--prompt-only`` 让编排停在 translate 步；``review`` 阶段没有
``--reviewer`` 时停在等待状态（``stopped_at=review`` / ``waiting_for_reviewer``，
exit 0；U4 会收紧为质量门禁失败）。两者都不算失败，但也不返回最终成功。

``agent/run_state.json`` 是 run 的私有状态，别的工具不读它。
"""

from __future__ import annotations

import datetime
import hashlib
import time
from pathlib import Path

from babeldoc_tools import common
from babeldoc_tools import layout
from babeldoc_tools import parse
from babeldoc_tools import registry
from babeldoc_tools import report as report_tool
from babeldoc_tools import review
from babeldoc_tools import translate

#: 显式阶段列表；``review`` 由 :func:`_run_review_stage` 在 check 之后执行。
STAGES = ("parse", "translate", "apply", "build", "check", "review", "report")

STATE_FILE = "run_state.json"
STATE_VERSION = 1

#: 每个阶段的输入依赖（键名同 :func:`_current_input_hashes` 的返回值）。
#: 只用于 ``--from`` 续跑时判断"被跳过的阶段是否已失效"。
STAGE_INPUTS: dict[str, tuple[str, ...]] = {
    "parse": ("pdf",),
    "translate": ("document_md", "markdown_source"),
    "apply": ("document_md", "translated_md"),
    "build": ("translated_md", "layout_overrides"),
    "check": ("translated_md", "layout_overrides"),
    "review": ("translated_md", "layout_overrides"),
    "report": ("translated_md", "layout_overrides"),
}

#: 阶段完成后写入 state 的关键产物（label → workdir 相对路径）。
STAGE_ARTIFACTS = {
    "parse": {"document_md": ("agent", "document.md"), "anchors": ("agent", "anchors.json")},
    "translate": {"translated_md": ("agent", "translated.md")},
    "apply": {"apply_report": ("agent", "apply_report.json")},
    "check": {"review_verdict": ("agent", "review_verdict.json")},
    "review": {"agent_review": ("agent", "agent_review.json")},
    "report": {},
}


# --------------------------------------------------------------------------- #
# 小工具
# --------------------------------------------------------------------------- #
def _now() -> str:
    return datetime.datetime.now().isoformat(timespec="seconds")


def _sha256_file(path) -> str | None:
    """文件内容的 sha256；路径为空或文件不存在时返回 ``None``。"""
    if path is None:
        return None
    candidate = Path(path)
    if not candidate.is_file():
        return None
    digest = hashlib.sha256()
    with candidate.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rel(workdir: Path, path) -> str:
    """workdir 内路径记成相对路径，其余保持绝对路径（便于迁移/比对）。"""
    if not path:
        return ""
    candidate = Path(path)
    try:
        return str(candidate.resolve().relative_to(workdir.resolve()))
    except ValueError:
        return str(candidate)


def _find_pdf(workdir: Path, kind: str) -> Path | None:
    for base in (workdir / "output", workdir):
        if not base.is_dir():
            continue
        for path in sorted(base.glob(f"*.{kind}.pdf")):
            return path
    return None


def _resolve_markdown(workdir: Path, markdown: str | None) -> Path | None:
    """``--markdown`` 解析：``self`` = 用 ``agent/document.md`` 当译文（自译，不调模型）。"""
    if not markdown:
        return None
    if markdown == "self":
        return common.agent_dir(workdir) / "document.md"
    return Path(markdown)


def _current_input_hashes(workdir: Path, pdf, markdown) -> dict[str, str | None]:
    agent = common.agent_dir(workdir)
    hashes: dict[str, str | None] = {
        "document_md": _sha256_file(agent / "document.md"),
        "translated_md": _sha256_file(agent / "translated.md"),
        "layout_overrides": _sha256_file(agent / "layout_overrides.json"),
    }
    if pdf:
        hashes["pdf"] = _sha256_file(pdf)
    source = _resolve_markdown(workdir, markdown)
    if source is not None:
        hashes["markdown_source"] = _sha256_file(source)
    return hashes


# --------------------------------------------------------------------------- #
# run_state.json
# --------------------------------------------------------------------------- #
def state_path(workdir) -> Path:
    return common.agent_dir(workdir) / STATE_FILE


def load_state(workdir) -> dict:
    state = common.read_json(state_path(workdir), default=None)
    if not isinstance(state, dict):
        return {}
    return state


def _save_state(workdir: Path, state: dict) -> None:
    state["version"] = STATE_VERSION
    state["updated_at"] = _now()
    common.write_json(state_path(workdir), state)


def _record(
    state: dict,
    stage: str,
    *,
    inputs: dict,
    artifacts: dict,
    duration: float,
    detail: dict | None = None,
    status: str = "ok",
) -> dict:
    entry = {
        "status": status,
        "ok": True,
        "at": _now(),
        "duration_s": round(duration, 2),
        "inputs": {key: inputs.get(key) for key in STAGE_INPUTS.get(stage, ())},
        "artifacts": artifacts,
        "detail": detail or {},
    }
    state.setdefault("stages", {})[stage] = entry
    return entry


def _stage_inputs_snapshot(stage: str, current: dict) -> dict:
    return {key: current[key] for key in STAGE_INPUTS.get(stage, ()) if key in current}


def find_stale_stage(state: dict, current: dict, upto: int) -> dict | None:
    """找出第一个"被跳过但输入已变"的阶段（最早者优先），无则返回 ``None``。"""
    recorded = state.get("stages") or {}
    for index, stage in enumerate(STAGES[:upto]):
        entry = recorded.get(stage)
        if not isinstance(entry, dict) or not entry.get("ok"):
            continue
        for key, old in (entry.get("inputs") or {}).items():
            if key not in current:
                continue
            new = current[key]
            if old != new:
                return {
                    "index": index,
                    "stage": stage,
                    "input": key,
                    "recorded": old,
                    "current": new,
                }
    return None


def _stale_error(workdir: Path, stale: dict, from_stage: str) -> dict:
    old = (stale["recorded"] or "（缺失）")[:12]
    new = (stale["current"] or "（缺失）")[:12]
    suggestion = stale["stage"]
    return {
        "ok": False,
        "error": {
            "code": "stale_upstream",
            "message": (
                f"上游产物已变更：{stale['stage']} 阶段的输入 {stale['input']} 的 sha256 "
                f"与 run_state.json 记录不符（{old} → {new}）。"
                f"被跳过的阶段不得沿用旧产物，请从最早受影响阶段重跑："
                f"bdt run --workdir {workdir} --from {suggestion}"
            ),
            "stage": stale["stage"],
            "input": stale["input"],
            "recorded_sha256": stale["recorded"],
            "current_sha256": stale["current"],
            "suggested_from": suggestion,
            "requested_from": from_stage,
        },
    }


# --------------------------------------------------------------------------- #
# 阶段前置校验
# --------------------------------------------------------------------------- #
def _preflight(stage: str, workdir: Path, pdf) -> dict | None:
    """返回 ``None`` 表示通过；否则返回 ``(code, message, extra)`` 三元组。"""
    agent = common.agent_dir(workdir)
    if stage == "parse":
        if not pdf:
            return {
                "code": "missing_pdf",
                "message": "parse 阶段需要 PDF 路径：bdt run <pdf> --workdir <WD>",
            }
        if not Path(pdf).exists():
            return {"code": "pdf_missing", "message": f"PDF 不存在: {pdf}"}
    elif stage == "translate":
        if not (agent / "document.md").exists():
            return {
                "code": "missing_artifact",
                "message": (
                    f"缺 {_rel(workdir, agent / 'document.md')}：请先跑 "
                    f"bdt run --workdir {workdir} --from parse（或 bdt parse）"
                ),
                "artifact": "agent/document.md",
                "run_from": "parse",
            }
    elif stage == "apply":
        if not (agent / "translated.md").exists():
            return {
                "code": "missing_artifact",
                "message": (
                    f"缺 {_rel(workdir, agent / 'translated.md')}：请先跑 "
                    f"bdt run --workdir {workdir} --from translate（或 bdt translate）"
                ),
                "artifact": "agent/translated.md",
                "run_from": "translate",
            }
    elif stage == "build":
        if not (agent / "apply_report.json").exists():
            return {
                "code": "missing_artifact",
                "message": (
                    f"缺 {_rel(workdir, agent / 'apply_report.json')}：请先跑 "
                    f"bdt run --workdir {workdir} --from apply（或 bdt apply）"
                ),
                "artifact": "agent/apply_report.json",
                "run_from": "apply",
            }
    elif stage == "check":
        if not (_find_pdf(workdir, "mono") or _find_pdf(workdir, "dual")):
            return {
                "code": "missing_artifact",
                "message": (
                    f"缺 build 产物（{_rel(workdir, workdir / 'output')}/*.mono.pdf 或 "
                    f"*.dual.pdf）：请先跑 bdt run --workdir {workdir} --from build"
                ),
                "artifact": "output/*.mono.pdf",
                "run_from": "build",
            }
    elif stage == "review":
        if not (agent / "review_verdict.json").exists():
            return {
                "code": "missing_artifact",
                "message": (
                    f"缺 {_rel(workdir, agent / 'review_verdict.json')}：请先跑 "
                    f"bdt run --workdir {workdir} --from check"
                ),
                "artifact": "agent/review_verdict.json",
                "run_from": "check",
            }
    return None


# --------------------------------------------------------------------------- #
# 阶段执行
# --------------------------------------------------------------------------- #
def _run_stage(stage: str, workdir: Path, cfg: dict) -> dict:
    """执行单个阶段，返回 ``registry.invoke`` 信封（``cfg["pdf"]`` 为源 PDF）。"""
    if stage == "parse":
        return registry.invoke(
            parse.parse_document,
            pdf=cfg["pdf"],
            workdir=str(workdir),
            layout=cfg["layout"],
            pages=cfg["pages"],
            lang_in=cfg["lang_in"],
            lang_out=cfg["lang_out"],
            mineru_token=cfg["mineru_token"],
            mineru_json=cfg["mineru_json"],
            mineru_cache_key=cfg["mineru_cache_key"],
            layout_coverage_threshold=cfg["layout_coverage_threshold"],
            mineru_use_ocr_text=cfg["mineru_use_ocr_text"],
        )
    if stage == "translate":
        return registry.invoke(
            translate.translate_document,
            workdir=str(workdir),
            ids=cfg["ids"] or None,
            feedback=cfg["feedback"],
            markdown=cfg["markdown_path"],
            prompt_only=cfg["prompt_only"],
            translator=cfg["translator"],
            timeout=cfg["timeout"],
            retry_missing=cfg["retry_missing"],
        )
    if stage == "apply":
        return registry.invoke(translate.apply_translation, workdir=str(workdir))
    if stage == "build":
        return registry.invoke(
            layout.build_pdf,
            workdir=str(workdir),
            output_dir=cfg["output_dir"],
            dual=cfg["dual"],
            watermark=cfg["watermark"],
            latex_bbox=cfg["latex_bbox"],
            latex_bbox_mode=cfg["latex_bbox_mode"],
            render=cfg["render"],
            stats=cfg["stats"],
        )
    if stage == "check":
        mono = _find_pdf(workdir, "mono")
        dual = _find_pdf(workdir, "dual")
        return registry.invoke(
            review.review_document,
            workdir=str(workdir),
            mono=str(mono) if mono else None,
            dual=str(dual) if dual else None,
            source_pdf=cfg["source_pdf"],
            skip_pdf_checks=cfg["skip_pdf_checks"],
        )
    if stage == "review":
        return registry.invoke(_review_with_agent, workdir=str(workdir), cfg=cfg)
    if stage == "report":
        return registry.invoke(
            report_tool.report,
            workdir=str(workdir),
            output_dir=cfg["output_dir"],
            title=cfg["title"],
            notes=cfg["notes"],
        )
    raise common.ToolError("unknown_stage", f"未知阶段: {stage}")  # pragma: no cover


def _stage_artifacts(stage: str, workdir: Path, data: dict, cfg: dict) -> dict:
    artifacts = {
        label: _rel(workdir, workdir.joinpath(*parts))
        for label, parts in STAGE_ARTIFACTS.get(stage, {}).items()
    }
    if stage == "build":
        output_dir = Path(cfg["output_dir"] or (workdir / "output"))
        for label, key in (("mono_pdf", "mono_pdf"), ("dual_pdf", "dual_pdf")):
            if data.get(key):
                artifacts[label] = _rel(workdir, data[key])
        if not data.get("dual_pdf"):
            artifacts.setdefault("output_dir", _rel(workdir, output_dir))
    elif stage == "check":
        if data.get("report"):
            artifacts["review_verdict"] = _rel(workdir, data["report"])
    elif stage == "review":
        if data.get("agent_review"):
            artifacts["agent_review"] = _rel(workdir, data["agent_review"])
        if data.get("prompt"):
            artifacts["review_prompt"] = _rel(workdir, data["prompt"])
    elif stage == "report":
        if data.get("report"):
            artifacts["final_report"] = _rel(workdir, data["report"])
    return artifacts


# --------------------------------------------------------------------------- #
# reviewer 契约（U3）：check 之后由外部审查命令给出结构化 verdict
# --------------------------------------------------------------------------- #
def _find_render_images(workdir: Path, output_dir) -> list[str]:
    """找一个 workdir 里已有的渲染图（build --render 的产物）。"""
    bases = [Path(output_dir)] if output_dir else []
    bases += [workdir / "output", workdir]
    for base in bases:
        if not base.is_dir():
            continue
        for candidate in (base / "render", base):
            if candidate.is_dir():
                images = sorted(str(path) for path in candidate.glob("*.png"))
                if images:
                    return images
    return []


def _source_pdf_path(workdir: Path, cfg: dict) -> str | None:
    if cfg.get("source_pdf"):
        return str(cfg["source_pdf"])
    if cfg.get("pdf"):
        return str(cfg["pdf"])
    try:
        import pickle

        with (common.agent_dir(workdir) / "state.pkl").open("rb") as handle:
            state = pickle.load(handle)  # noqa: S301 - 本地 workdir 私有产物
        return state.get("pdf_path")
    except Exception:  # noqa: BLE001 - 源 PDF 仅用于提示词展示，取不到不阻断
        return None


def _review_prompt(workdir: Path, cfg: dict) -> str:
    """组装审查提示词：把 reviewer 需要的全部路径写清楚，不要求它猜目录。"""
    agent = common.agent_dir(workdir)
    mono = _find_pdf(workdir, "mono")
    dual = _find_pdf(workdir, "dual")
    images = _find_render_images(workdir, cfg.get("output_dir"))
    layout_lint = agent / "layout_lint.json"
    optional = [
        ("apply_report.json", agent / "apply_report.json"),
        ("layout_geometry.json", agent / "layout_geometry.json"),
        ("reconstruct_report.json", agent / "reconstruct_report.json"),
        ("layout_overrides.json", agent / "layout_overrides.json"),
    ]
    lines = [
        "## 本次审查输入（由 bdt run 的 review 阶段填入，路径均为绝对路径）",
        "",
        f"- 工作目录：`{workdir}`",
        f"- 源 PDF：`{_source_pdf_path(workdir, cfg) or '（未找到）'}`",
        f"- mono PDF：`{mono or '（未生成）'}`",
        f"- dual PDF：`{dual or '（未生成）'}`",
        f"- 结构审查结果：`{agent / 'review_verdict.json'}`",
        f"- 排版 lint：`{layout_lint if layout_lint.exists() else '（check 阶段未产出 layout_lint.json）'}`",
    ]
    for label, path in optional:
        if path.exists():
            lines.append(f"- {label}：`{path}`")
    if images:
        lines.append("- 渲染图：")
        lines.extend(f"  - `{path}`" for path in images[:20])
    else:
        lines.append("- 渲染图：（无；如需视觉定位，可先跑 `bdt build --render 1,2`）")
    lines += [
        "",
        "## 输出协议（必须遵守）",
        "只输出一个 JSON 对象，不要代码围栏、不要额外解释：",
        '{"verdict": "pass" | "needs_fix", "findings": ['
        '{"sev": "P0|P1|P2", "check": "<编号>", "page": <页号或 null>, '
        '"evidence": "<工具输出片段/坐标/文本>", "fix": "retranslate:<id> | '
        'layout:<patch> | accept:<理由> | none"}]}',
        'verdict 只能是 "pass" 或 "needs_fix"；findings 必须存在（可为空数组）。',
        "",
    ]
    guidance = _reviewer_guidance()
    return (guidance + "\n\n" + "\n".join(lines)) if guidance else "\n".join(lines)


def _reviewer_guidance() -> str:
    """复用 reviewer-protocol 提示词（内容不改），取不到就用内置最小说明。"""
    try:
        return common.load_prompt("reviewer-protocol")
    except common.ToolError:
        return "你是严格的文档翻译结构与协议审查员，依据工具输出给出结论。"


def _parse_review_json(stdout: str) -> dict:
    import json
    import re

    text = stdout.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n|```$", "", text).strip()
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise common.ToolError(
            "reviewer_invalid_json", f"reviewer 输出不是合法 JSON: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise common.ToolError(
            "reviewer_invalid_json", "reviewer 输出必须是 JSON 对象"
        )
    verdict = payload.get("verdict")
    if verdict not in ("pass", "needs_fix"):
        raise common.ToolError(
            "reviewer_invalid_json",
            f'reviewer 输出的 verdict 必须是 "pass" 或 "needs_fix"，得到 {verdict!r}',
        )
    findings = payload.get("findings")
    if not isinstance(findings, list):
        raise common.ToolError(
            "reviewer_invalid_json", "reviewer 输出的 findings 必须是数组"
        )
    return payload


def _review_with_agent(workdir: str, *, cfg: dict) -> dict:
    """调用 ``--reviewer``：stdin 传审查提示词，stdout 收结构化 verdict。"""
    workdir_path = Path(workdir)
    agent = common.agent_dir(workdir_path)
    command = cfg.get("reviewer")
    if not command:
        # 由调用方（run_pipeline）在无 reviewer 时提前停下；这里兜底防止漏网。
        raise common.ToolError("reviewer_missing", "未指定 --reviewer 命令")

    prompt_text = _review_prompt(workdir_path, cfg)
    prompt_file = agent / "review_prompt.md"
    prompt_file.write_text(prompt_text, encoding="utf-8")

    try:
        # 与 run_translator 共用同一 stdin/stdout 协议 helper（shlex.split 后不经 shell）
        stdout = common._run_subprocess(
            prompt_text, command, int(cfg.get("timeout") or 1800), "reviewer"
        )
    except common.ToolError as exc:
        if exc.code == "reviewer_empty":
            raise common.ToolError(
                "reviewer_invalid_json", "reviewer 的 stdout 为空：无法得到 verdict"
            ) from exc
        raise

    payload = _parse_review_json(stdout)
    payload.setdefault("findings", [])
    payload["prompt"] = str(prompt_file)
    payload["command"] = command
    result_path = common.write_json(agent / "agent_review.json", payload)
    payload["agent_review"] = str(result_path)
    return payload


# --------------------------------------------------------------------------- #
# 编排入口
# --------------------------------------------------------------------------- #
def run_pipeline(
    workdir: str,
    pdf: str | None = None,
    *,
    from_stage: str = "parse",
    output_dir: str | None = None,
    layout_backend: str = "mineru",
    pages: str | None = None,
    lang_in: str = "en",
    lang_out: str = "zh",
    mineru_token: str | None = None,
    mineru_json: str | None = None,
    mineru_cache_key: str | None = None,
    layout_coverage_threshold: float = 0.005,
    mineru_use_ocr_text: bool = False,
    ids: list[str] | None = None,
    feedback: str | None = None,
    markdown: str | None = None,
    prompt_only: bool = False,
    timeout: int = 1800,
    translator: str | None = None,
    reviewer: str | None = None,
    retry_missing: bool = True,
    dual: bool = False,
    watermark: bool = False,
    latex_bbox: bool = True,
    latex_bbox_mode: str | None = None,
    render: str | None = None,
    stats: bool = True,
    skip_pdf_checks: bool = False,
    source_pdf: str | None = None,
    title: str | None = None,
    notes: str | None = None,
) -> dict:
    """按阶段顺序执行并返回完整 JSON 信封（``ok`` / ``data`` 或 ``error``）。"""
    if from_stage not in STAGES:
        return registry.error_payload(
            "bad_from_stage", f"--from 必须是 {list(STAGES)} 之一，得到 {from_stage!r}"
        )
    workdir_path = Path(workdir)
    start_index = STAGES.index(from_stage)
    markdown_path = None
    if markdown:
        resolved = _resolve_markdown(workdir_path, markdown)
        # ``--markdown self`` 指向 agent/document.md，parse 之后才存在：留给
        # translate 阶段的前置校验处理（缺 document.md 会报 missing_artifact）。
        if (
            markdown != "self"
            and resolved is not None
            and not resolved.exists()
        ):
            return registry.error_payload(
                "translated_md_missing", f"--markdown 指向的文件不存在: {resolved}"
            )
        markdown_path = str(resolved) if resolved is not None else None

    cfg = {
        "pdf": pdf,
        "layout": layout_backend,
        "pages": pages,
        "lang_in": lang_in,
        "lang_out": lang_out,
        "mineru_token": mineru_token,
        "mineru_json": mineru_json,
        "mineru_cache_key": mineru_cache_key,
        "layout_coverage_threshold": layout_coverage_threshold,
        "mineru_use_ocr_text": mineru_use_ocr_text,
        "ids": ids or [],
        "feedback": feedback,
        "markdown_path": markdown_path,
        "prompt_only": prompt_only,
        "timeout": timeout,
        "translator": translator,
        "reviewer": reviewer,
        "retry_missing": retry_missing,
        "output_dir": output_dir,
        "dual": dual,
        "watermark": watermark,
        "latex_bbox": latex_bbox,
        "latex_bbox_mode": latex_bbox_mode,
        "render": render,
        "stats": stats,
        "skip_pdf_checks": skip_pdf_checks,
        "source_pdf": source_pdf,
        "title": title,
        "notes": notes,
    }

    state = load_state(workdir_path)
    current = _current_input_hashes(workdir_path, pdf, markdown)

    # ---- 续跑：被跳过阶段的输入若被改过，报错并给出明确指引 ----------------- #
    stale = find_stale_stage(state, current, start_index)
    if stale is not None:
        return _stale_error(workdir_path, stale, from_stage)

    stage_results: list[dict] = [
        {"stage": stage, "status": "skipped"} for stage in STAGES[:start_index]
    ]
    state["pdf"] = pdf
    state["config"] = {
        "from": from_stage,
        "layout": layout_backend,
        "translator": translator,
        "reviewer": reviewer,
        "markdown": markdown,
        "prompt_only": prompt_only,
        "timeout": timeout,
        "dual": dual,
        "output_dir": output_dir,
    }

    stopped: dict | None = None
    check_data: dict | None = None
    agent_review: dict | None = None
    for index in range(start_index, len(STAGES)):
        stage = STAGES[index]

        # 各阶段的前置产物校验（含 review 需要 check 的 review_verdict.json）
        failure = _preflight(stage, workdir_path, pdf)
        if failure is not None:
            return _fail(failure, stage, stage_results, workdir_path, state, from_stage)

        if stage == "review" and not reviewer:
            # ---- 没有 --reviewer：停在等待状态，不返回最终成功 ------------------ #
            # U4 会把这里收紧为"质量门禁失败"（review 未执行即不可交付）；本任务只
            # 提供结构化停止点，exit 0，方便 Agent 侧拿到 review_prompt 后再决定。
            current = _current_input_hashes(workdir_path, pdf, markdown)
            entry = _record(
                state,
                "review",
                inputs=_stage_inputs_snapshot("review", current),
                artifacts={},
                duration=0.0,
                detail={"waiting_for_reviewer": True},
                status="waiting",
            )
            _save_state(workdir_path, state)
            stage_results.append(
                {
                    "stage": "review",
                    "status": "waiting",
                    "reason": "waiting_for_reviewer",
                }
            )
            stopped = {
                "stage": "review",
                "reason": "waiting_for_reviewer",
                "note": "未指定 --reviewer；report 未执行。U4 会将其收紧为门禁失败",
            }
            for stage_name in STAGES[index + 1 :]:
                stage_results.append(
                    {
                        "stage": stage_name,
                        "status": "skipped",
                        "reason": "waiting_for_reviewer",
                    }
                )
            break

        started = time.time()
        result = _run_stage(stage, workdir_path, cfg)
        duration = time.time() - started
        if not result.get("ok"):
            error = dict(result.get("error") or {})
            error["stage"] = stage
            return {
                "ok": False,
                "error": error,
                "data": _summary(
                    workdir_path, pdf, from_stage, stage_results, state, None, stopped
                ),
            }
        data = result.get("data") or {}
        current = _current_input_hashes(workdir_path, pdf, markdown)
        artifacts = _stage_artifacts(stage, workdir_path, data, cfg)
        detail = {key: data.get(key) for key in ("verdict", "dry_run", "lang_out") if data.get(key) is not None}
        entry = _record(
            state,
            stage,
            inputs=_stage_inputs_snapshot(stage, current),
            artifacts=artifacts,
            duration=duration,
            detail=detail,
        )
        _save_state(workdir_path, state)
        stage_results.append(
            {
                "stage": stage,
                "status": "ok",
                "duration_s": entry["duration_s"],
                "artifacts": artifacts,
            }
        )

        if stage == "check":
            check_data = data
        if stage == "review":
            agent_review = data
        if stage == "translate" and (data.get("dry_run") or not data.get("translated_md")):
            # --prompt-only：只写 agent/prompt.md，不调用任何命令；后续阶段无从谈起。
            # 返回停止点而非错误：Agent 侧读走提示词后自己调翻译命令，再以 --markdown
            # 或 --translator 续跑。
            stopped = {
                "stage": "translate",
                "reason": "prompt_only",
                "note": "translate 为 prompt-only（dry-run），apply/build/check/review/report 未执行",
                "prompt": data.get("prompt"),
            }
            for stage_name in STAGES[index + 1 :]:
                stage_results.append(
                    {"stage": stage_name, "status": "skipped", "reason": "prompt_only"}
                )
            break

    state["inputs"] = current
    _save_state(workdir_path, state)

    summary = _summary(
        workdir_path,
        pdf,
        from_stage,
        stage_results,
        state,
        check_data,
        stopped,
        agent_review,
    )
    if stopped is not None:
        # 提前停止（prompt_only / waiting_for_reviewer）：不是失败，但也不是最终成功。
        return {"ok": True, "data": summary}
    verdict = (check_data or {}).get("verdict")
    if verdict == "needs_fix":
        # check 失败：仍然跑完 report（上面已跑），但整体 exit 1 并带上 verdict。
        return {
            "ok": False,
            "error": {
                "code": "check_needs_fix",
                "message": (
                    "check verdict=needs_fix：report 已生成，但整体视为失败；"
                    "按 blockers 决定重译哪些 id 后重跑"
                ),
                "stage": "check",
                "verdict": verdict,
                "blockers": (check_data or {}).get("blockers") or [],
            },
            "data": summary,
        }
    return {"ok": True, "data": summary}


def _summary(
    workdir: Path,
    pdf,
    from_stage: str,
    stage_results: list[dict],
    state: dict,
    check_data: dict | None,
    stopped: dict | None,
    agent_review: dict | None = None,
) -> dict:
    outputs: dict = {}
    for entry in stage_results:
        outputs.update(entry.get("artifacts") or {})
    payload = {
        "workdir": str(workdir),
        "pdf": pdf,
        "from": from_stage,
        "stages": stage_results,
        "verdict": (check_data or {}).get("verdict"),
        "outputs": outputs,
        "state": _rel(workdir, state_path(workdir)),
        "config": state.get("config") or {},
    }
    if check_data is not None:
        payload["check"] = {
            "verdict": check_data.get("verdict"),
            "blockers": len(check_data.get("blockers") or []),
            "blocker_codes": [
                item.get("code") for item in (check_data.get("blockers") or [])
            ],
            "warnings": len(check_data.get("warnings") or []),
            "metrics": check_data.get("metrics") or {},
        }
    if agent_review is not None:
        payload["agent_review"] = {
            "verdict": agent_review.get("verdict"),
            "findings": len(agent_review.get("findings") or []),
        }
    if stopped is not None:
        payload["stopped"] = stopped
        # 顶层停止点：方便脚本直接判断是停在哪一步（translate / review）。
        payload["stopped_at"] = stopped.get("stage")
        if "reason" in stopped:
            payload["status"] = stopped["reason"]
        if stopped.get("prompt"):
            payload["prompt"] = stopped["prompt"]
    return payload


def _fail(
    failure: dict,
    stage: str,
    stage_results: list[dict],
    workdir_path: Path,
    state: dict,
    from_stage: str,
) -> dict:
    """前置校验失败：结构化错误 + 已完成阶段摘要（产物保留供续跑）。"""
    error = dict(failure)
    error["stage"] = stage
    error["completed_stages"] = [
        entry["stage"] for entry in stage_results if entry.get("status") == "ok"
    ]
    return {
        "ok": False,
        "error": error,
        "data": _summary(
            workdir_path, state.get("pdf"), from_stage, stage_results, state, None, None
        ),
    }
