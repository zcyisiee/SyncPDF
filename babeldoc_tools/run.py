"""``bdt run``：把各阶段串成一条可续跑的编排。

阶段顺序::

    parse → translate → apply → build → check → [review 占位] → report

设计要点（U2 Part 2；reviewer 阶段由 U3 注入）：

- **不做状态机**：一个显式阶段元组 + 一个 ``agent/run_state.json`` 私有状态文件。
- **阶段产物校验**：每个要执行的阶段先检查所需 artifact 是否存在，缺失则以
  ``missing_artifact`` 结构化错误退出（exit 1），并指明该跑哪一步；绝不靠"跳过"
  制造成功。
- **哈希失效**：``run_state.json`` 记录每个阶段运行时的输入 sha256（
  ``document.md`` / ``translated.md`` / ``layout_overrides.json`` / PDF / 外部
  ``--markdown``）。``--from`` 续跑时，若被跳过阶段的输入哈希与记录不符，说明上游
  产物被改过，**报错并给出明确指引**（不静默沿用旧产物，也不静默回退长跑）。
- 任一步失败：以该步错误 JSON 退出，已完成步骤的产物留在 workdir 供续跑。

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

#: 显式阶段列表；``review`` 是 U3 才实现的占位阶段（本任务直通）。
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
            model=cfg["model"],
            effort=cfg["effort"],
            timeout=cfg["timeout"],
            command=cfg["translator"],
            prompt=cfg["prompt"],
            repair_prompt=cfg["repair_prompt"],
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
    elif stage == "report":
        if data.get("report"):
            artifacts["final_report"] = _rel(workdir, data["report"])
    return artifacts


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
    model: str | None = None,
    effort: str | None = None,
    timeout: int = 1800,
    translator: str | None = None,
    prompt: str | None = None,
    repair_prompt: str | None = None,
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
        "model": model,
        "effort": effort,
        "timeout": timeout,
        "translator": translator,
        "prompt": prompt,
        "repair_prompt": repair_prompt,
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
        "markdown": markdown,
        "prompt_only": prompt_only,
        "model": model,
        "effort": effort,
        "timeout": timeout,
        "dual": dual,
        "output_dir": output_dir,
    }

    stopped: dict | None = None
    check_data: dict | None = None
    for index in range(start_index, len(STAGES)):
        stage = STAGES[index]

        if stage == "review":
            # ---- reviewer 阶段：U3 注入（本任务只留位置并直通） ------------ #
            failure = _preflight(stage, workdir_path, pdf)
            if failure is not None:
                return _fail(failure, stage, stage_results, workdir_path, state, from_stage)
            current = _current_input_hashes(workdir_path, pdf, markdown)
            entry = _record(
                state,
                "review",
                inputs=_stage_inputs_snapshot("review", current),
                artifacts={},
                duration=0.0,
                detail={"placeholder": True, "note": "reviewer 阶段由 U3 注入"},
                status="placeholder",
            )
            _save_state(workdir_path, state)
            stage_results.append(
                {"stage": "review", "status": "placeholder", "note": "U3 注入"}
            )
            continue

        failure = _preflight(stage, workdir_path, pdf)
        if failure is not None:
            return _fail(failure, stage, stage_results, workdir_path, state, from_stage)

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
        if stage == "translate" and (data.get("dry_run") or not data.get("translated_md")):
            # --prompt-only：只写 prompt，不调模型；后续阶段无从谈起（U3 定义完整语义）
            stopped = {
                "reason": "prompt_only",
                "note": "translate 为 dry-run（--prompt-only），apply/build/check/report 未执行",
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
        workdir_path, pdf, from_stage, stage_results, state, check_data, stopped
    )
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
    if stopped is not None:
        payload["stopped"] = stopped
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
