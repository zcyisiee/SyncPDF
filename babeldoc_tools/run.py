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

**停止语义（U4）**：``--prompt-only`` 让编排停在 translate 步；``review`` 阶段没有
``--reviewer`` 时**质量门禁失败**——停在 review、退出码 1、错误码
``waiting_for_reviewer``（U3 曾把它当"等待"返回 exit 0；U4 收紧为失败，因为
"没人审查"不能算交付成功）。``reviewer`` 返回 ``needs_fix`` 时把 findings 映射成
``actions`` JSON 并以 ``reviewer_needs_fix`` 失败；单个任务最多 2 个翻译修复轮 +
2 个排版修复轮，超限停在 ``needs_human_review`` 且不再调用模型。

``agent/run_state.json`` 是 run 的私有状态，别的工具不读它；除各阶段哈希外还记录
``quality``：check 的 verdict 与子项状态、reviewer 结论、修复轮计数（供 ``--from``
与人工判断）。
"""

from __future__ import annotations

import datetime
import hashlib
import json
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

#: 单任务修复轮上限：kind → 最多可记录的修复轮数（超限停 ``needs_human_review``）。
#: run 不自动执行修复，一轮 = reviewer 给出一次 needs_fix（上层 Agent 执行
#: ``translate --ids`` / ``layout-set`` 后再 ``--from apply`` 续跑）。
MAX_FIX_ROUNDS = {"retranslate": 2, "layout": 2}

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
    "check": {
        "review_verdict": ("agent", "review_verdict.json"),
        "layout_lint": ("agent", "layout_lint.json"),
        "link_audit": ("agent", "link_audit.json"),
    },
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
    recorder = cfg.get("debug_recorder")
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
            mineru_language=cfg.get("mineru_language"),
            mineru_json=cfg["mineru_json"],
            mineru_cache_key=cfg["mineru_cache_key"],
            layout_coverage_threshold=cfg["layout_coverage_threshold"],
            mineru_use_ocr_text=cfg["mineru_use_ocr_text"],
            debug_recorder=recorder,
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
            glossaries=cfg["glossaries"],
            debug_recorder=recorder,
        )
    if stage == "apply":
        return registry.invoke(
            translate.apply_translation,
            workdir=str(workdir),
            debug_recorder=recorder,
        )
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
            debug_recorder=recorder,
            debug_recompile=cfg.get("debug_recompile", False),
        )
    if stage == "check":
        mono = _find_pdf(workdir, "mono")
        dual = _find_pdf(workdir, "dual")
        # run 的 check 使用 strict 语义：verdict != pass（含子项 not_available 导致
        # 无法确认）时整体失败；但继续跑 reviewer 与 report（由 run_pipeline 决定）。
        return registry.invoke(
            review.check_document,
            workdir=str(workdir),
            mono=str(mono) if mono else None,
            dual=str(dual) if dual else None,
            source_pdf=cfg["source_pdf"],
            skip_pdf_checks=cfg["skip_pdf_checks"],
            strict=True,
            debug_recorder=recorder,
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
            debug_recorder=recorder,
        )
    raise common.ToolError("unknown_stage", f"未知阶段: {stage}")  # pragma: no cover


def _stage_artifacts(stage: str, workdir: Path, data: dict, cfg: dict) -> dict:
    artifacts = {
        label: _rel(workdir, workdir.joinpath(*parts))
        for label, parts in STAGE_ARTIFACTS.get(stage, {}).items()
        # check 的子项产物按需生成（not_available 时不存在）：只记真实落盘的。
        if workdir.joinpath(*parts).exists()
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
# reviewer 契约（U3 调用方 / U4 质量门禁）
# --------------------------------------------------------------------------- #
PROBLEM_ANCHORS = ("missing", "wrong_label", "wrong_role", "unverified")


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
    # check 三块结论的落盘位置固定出现在提示词里；缺失时写"未产出"而不是省略，
    # 让 reviewer 明确知道缺了什么，不需要去猜内部目录。
    required = [
        ("结构审查结果", agent / "review_verdict.json"),
        ("排版 lint", agent / "layout_lint.json"),
        ("链接审计", agent / "link_audit.json"),
    ]
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
    ]
    for label, path in required:
        lines.append(f"- {label}：`{path}`" if path.exists() else f"- {label}：（未产出 {path.name}）")
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
        '{"id": "<段落 id 或 <页号>:<链接逻辑 id>>", "kind": "retranslate" | "layout", '
        '"sev": "P0|P1|P2", "page": <页号或 null>, '
        '"evidence": "<工具输出片段/坐标/文本>", "action": "translate --ids <id> | '
        'layout-set <patch> | accept:<理由>"}]}',
        'verdict 只能是 "pass" 或 "needs_fix"；findings 必须存在（可为空数组）。',
        "findings 每条必须带 id / kind（retranslate 或 layout）/ evidence / action；",
        "缺字段或不合法会让 run 以 reviewer_invalid_json 失败（未知字段会保留）。",
        "run 不会自动修复：它把 findings 映射成 actions 交给上层 Agent 执行后",
        "再以 `bdt run --from apply` 续跑。",
        "",
    ]
    if " model-call " in (cfg.get("reviewer") or ""):
        lines += ["Text-only review: no PDF/image access; do not claim visual inspection."]
        total = 0
        for name in ("document.md", "translated.md", "review_verdict.json", "layout_lint.json", "link_audit.json"):
            path = agent / name
            if path.is_file():
                total += path.stat().st_size
                if total > 1_000_000:
                    raise common.ToolError("model_context_too_large", "Text review context exceeds 1 MB; use a script reviewer or disable AI review")
                text = path.read_text(encoding="utf-8")
                if name.endswith(".json"):
                    text = json.dumps(_review_safe_json(json.loads(text)), ensure_ascii=False)
                lines += ["## " + name, text]
    guidance = _reviewer_guidance()
    return (guidance + "\n\n" + "\n".join(lines)) if guidance else "\n".join(lines)


def _review_safe_json(value):
    """Exclude configuration/credential fields from text-only model review inputs."""
    if isinstance(value, dict):
        return {k: _review_safe_json(v) for k, v in value.items()
                if not any(word in k.lower() for word in
                           ("config", "token", "secret", "key", "command", "translator", "reviewer", "url"))}
    if isinstance(value, list):
        return [_review_safe_json(v) for v in value]
    return value


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
    for index, finding in enumerate(findings):
        if not isinstance(finding, dict):
            raise common.ToolError(
                "reviewer_invalid_json",
                f"reviewer 的 findings[{index}] 必须是对象，得到 {type(finding).__name__}",
            )
        for field in ("id", "kind", "evidence", "action"):
            value = finding.get(field)
            if value in (None, "", []):
                raise common.ToolError(
                    "reviewer_invalid_json",
                    f"reviewer 的 findings[{index}] 缺 {field}"
                    "（每条必须带 id / kind / evidence / action）",
                )
        kind = finding["kind"]
        if kind not in ("retranslate", "layout"):
            raise common.ToolError(
                "reviewer_invalid_json",
                f'reviewer 的 findings[{index}].kind 必须是 "retranslate" 或 "layout"，'
                f"得到 {kind!r}",
            )
    return payload


def _findings_to_actions(findings: list[dict]) -> dict:
    """把 reviewer findings 映射成上层 Agent 可直接执行的 actions JSON。

    run 不自动执行任何修复（无隐藏循环）：只产出 actions，Agent 执行
    ``bdt translate --ids`` / ``bdt layout-set`` 后再 ``--from apply`` 续跑。
    """
    retranslate_ids: list[str] = []
    layout_items: list[dict] = []
    accepted: list[dict] = []
    for finding in findings:
        kind = finding.get("kind")
        action = finding.get("action")
        entry = {
            "id": finding.get("id"),
            "kind": kind,
            "action": action,
            "evidence": finding.get("evidence"),
            "sev": finding.get("sev"),
        }
        if isinstance(action, str) and action.lower().startswith("accept"):
            accepted.append(entry)
            continue
        if kind == "retranslate":
            retranslate_ids.append(str(finding.get("id")))
        else:
            layout_items.append(entry)
    actions: dict = {"round_kind": None}
    if retranslate_ids:
        actions["round_kind"] = "retranslate"
        actions["retranslate"] = {
            "command": "bdt translate --ids " + ",".join(retranslate_ids),
            "ids": retranslate_ids,
        }
    if layout_items:
        if actions["round_kind"] is None:
            actions["round_kind"] = "layout"
        actions["layout"] = {
            "command": "bdt layout-set --patch <patch> 然后 bdt build",
            "items": layout_items,
        }
    if accepted:
        actions["accepted"] = accepted
    actions["resume"] = "执行上面的命令后：bdt run --workdir <WD> --from apply"
    return actions


def _fix_rounds(state: dict) -> dict:
    """修复轮计数（record：kind → 已记录的修复轮数）。"""
    quality = state.setdefault("quality", {})
    rounds = quality.get("fix_rounds")
    if not isinstance(rounds, dict):
        rounds = {}
        quality["fix_rounds"] = rounds
    for kind in MAX_FIX_ROUNDS:
        rounds.setdefault(kind, 0)
    return rounds


def _record_fix_round(state: dict, actions: dict) -> dict:
    """按 findings 的 kind 记一轮修复；返回每个 kind 的最新计数。"""
    rounds = _fix_rounds(state)
    kinds = set()
    if (actions.get("retranslate") or {}).get("ids"):
        kinds.add("retranslate")
    if (actions.get("layout") or {}).get("items"):
        kinds.add("layout")
    for kind in kinds:
        rounds[kind] = int(rounds.get(kind, 0)) + 1
    return rounds


def _rounds_exhausted(rounds: dict) -> list[str]:
    """已达上限的 kind 列表（计入下一轮前先判断，避免超限调用模型）。"""
    return [
        kind
        for kind, limit in MAX_FIX_ROUNDS.items()
        if int(rounds.get(kind, 0)) >= limit
    ]


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
    mineru_language: str | None = "en",
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
    skip_ai_review: bool = False,
    retry_missing: bool = True,
    glossaries: str | None = None,
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
    debug_recorder=None,
    debug_recompile: bool = False,
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
        "mineru_language": mineru_language,
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
        #: 术语表 CSV 路径（W13）：只被 translate 阶段消费（apply/build/check/review 忽略），
        #: 且只在整篇翻译的提示词里生效（重译不注入，见 translate.translate_document）。
        "glossaries": glossaries,
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
        "debug_recorder": debug_recorder,
        "debug_recompile": bool(debug_recompile),
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

        if stage == "review" and skip_ai_review and not reviewer:
            state.setdefault("quality", {})["reviewer"] = {"status": "skipped"}
            _record(state, "review", inputs={}, artifacts={}, duration=0.0,
                    detail={"ai_review": "disabled"}, status="skipped")
            stage_results.append({"stage": "review", "status": "skipped"})
            _save_state(workdir_path, state)
            continue

        # 各阶段的前置产物校验（含 review 需要 check 的 review_verdict.json）
        failure = _preflight(stage, workdir_path, pdf)
        if failure is not None:
            return _fail(failure, stage, stage_results, workdir_path, state, from_stage)

        if stage == "review" and not reviewer:
            # ---- 没有 --reviewer：质量门禁失败（U4 收紧，U3 曾是 exit 0 的等待） -- #
            # "没人审查"不能算交付成功：停在 review、exit 1、error.code 为
            # waiting_for_reviewer，并写清 --reviewer 用法；report 不执行。
            # 审查提示词落盘（agent/review_prompt.md），供上层 Agent 接手执行。
            (common.agent_dir(workdir_path) / "review_prompt.md").write_text(
                _review_prompt(workdir_path, cfg), encoding="utf-8"
            )
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
            state.setdefault("quality", {})["reviewer"] = {
                "status": "waiting_for_reviewer",
                "verdict": None,
            }
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
                "note": "未指定 --reviewer，review/report 未执行",
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

        if stage == "review":
            # ---- 迭代上限：超限就停，且**不再调用模型** ---------------------- #
            # 单任务最多 2 个翻译修复轮 + 2 个排版修复轮（记在 run_state.json）。
            # 任一 kind 用尽即停：继续让模型审查也无法再推进修复（findings 会重复），
            # 属"需要人判断"而非"再试一次"。
            exhausted = _rounds_exhausted(_fix_rounds(state))
            if exhausted:
                state.setdefault("quality", {})["reviewer"] = {
                    "status": "needs_human_review",
                    "verdict": None,
                    "fix_rounds": dict(_fix_rounds(state)),
                    "rounds_exhausted": exhausted,
                }
                _save_state(workdir_path, state)
                stage_results.append(
                    {
                        "stage": "review",
                        "status": "blocked",
                        "reason": "needs_human_review",
                    }
                )
                stopped = {
                    "stage": "review",
                    "reason": "needs_human_review",
                    "note": (
                        "修复轮已达上限"
                        f"（{MAX_FIX_ROUNDS}）；不再调用 reviewer，请人工判断"
                    ),
                }
                for stage_name in STAGES[index + 1 :]:
                    stage_results.append(
                        {
                            "stage": stage_name,
                            "status": "skipped",
                            "reason": "needs_human_review",
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
            state.setdefault("quality", {})["check"] = {
                "verdict": data.get("verdict"),
                "blockers": len(data.get("blockers") or []),
                "layout": (data.get("layout") or {}).get("status"),
                "links": (data.get("links") or {}).get("status"),
                "reasons": data.get("reasons") or [],
            }
            _save_state(workdir_path, state)
        if stage == "review":
            actions = _findings_to_actions(data.get("findings") or [])
            rounds = _fix_rounds(state)
            state.setdefault("quality", {})["reviewer"] = {
                "verdict": data.get("verdict"),
                "findings": len(data.get("findings") or []),
                "actions": actions,
                "fix_rounds_before": dict(rounds),
                "rounds_exhausted_before": _rounds_exhausted(rounds),
            }
            agent_review = data
            agent_review["actions"] = actions
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
        if stopped.get("reason") == "waiting_for_reviewer":
            # U4 收紧：没有 --reviewer 即质量门禁失败（exit 1），不再是 U3 的等待。
            return {
                "ok": False,
                "error": {
                    "code": "waiting_for_reviewer",
                    "message": (
                        "review 阶段未执行：需要显式审查命令才能确认交付质量。"
                        "请用 `bdt run --workdir <WD> --from check "
                        "--reviewer '<命令>'` 续跑（命令 stdin 收审查提示词、"
                        "stdout 必须是 {verdict, findings} JSON）。"
                        f"审查提示词已写好在 {common.agent_dir(workdir_path) / 'review_prompt.md'}"
                    ),
                    "stage": "review",
                    "stopped_at": "review",
                    "prompt": str(common.agent_dir(workdir_path) / "review_prompt.md"),
                    "reviewer_usage": (
                        "bdt run --workdir <WD> --from check --reviewer '<cmd>'"
                    ),
                },
                "data": summary,
            }
        if stopped.get("reason") == "needs_human_review":
            rounds = dict(_fix_rounds(state))
            return {
                "ok": False,
                "error": {
                    "code": "needs_human_review",
                    "message": (
                        "修复轮已达上限（单任务最多 2 个翻译修复轮 + 2 个排版修复轮："
                        f"{MAX_FIX_ROUNDS}）。stop 自动迭代，不再调用模型；请人工判断。"
                    ),
                    "stage": "review",
                    "fix_rounds": rounds,
                    "limits": dict(MAX_FIX_ROUNDS),
                    "exhausted": _rounds_exhausted(rounds),
                    "stopped_at": "review",
                },
                "data": summary,
            }
        # prompt_only：不是失败，但也不是最终成功。
        return {"ok": True, "data": summary}

    if check_data is None:
        # --from review/report 续跑：本次会话没跑 check，但前置校验已确认
        # review_verdict.json 存在且上游哈希未变，check 的结论以 run_state
        # 记录为准——否则 reviewer 的 pass 会绕过 check 的确定性 blocker。
        recorded = (state.get("quality") or {}).get("check") or {}
        if recorded.get("verdict"):
            check_data = {
                "verdict": recorded.get("verdict"),
                "reasons": recorded.get("reasons") or [],
                "blockers": [],
                "layout": {"status": recorded.get("layout")},
                "links": {"status": recorded.get("links")},
            }
    verdict = (check_data or {}).get("verdict")
    review_verdict = (agent_review or {}).get("verdict")
    if verdict == "needs_fix":
        # check 失败：report/review 已跑完，但整体 exit 1。reviewer 的 pass 不能
        # 覆盖 check 的确定性 blocker（check needs_fix 优先）。
        error = {
            "code": "check_needs_fix",
            "message": (
                "check verdict=needs_fix：report 已生成，但整体视为失败；"
                "按 reasons / blockers 决定重译哪些 id 或调整排版后重跑"
            ),
            "stage": "check",
            "verdict": verdict,
            "reasons": (check_data or {}).get("reasons") or [],
            "blockers": (check_data or {}).get("blockers") or [],
            "reviewer_verdict": review_verdict,
            "layout": (check_data or {}).get("layout") or {},
            "links": (check_data or {}).get("links") or {},
        }
        # reviewer 也判 needs_fix 时，把它的可执行 actions 一并给出（修复入口），
        # 但错误码仍是 check_needs_fix——确定性 blocker 优先。
        if review_verdict == "needs_fix":
            actions = (agent_review or {}).get("actions") or {}
            after = _record_fix_round(state, actions)
            state["quality"]["reviewer"]["fix_rounds"] = dict(after)
            state["quality"]["reviewer"]["status"] = "needs_fix"
            _save_state(workdir_path, state)
            summary["fix_rounds"] = dict(after)
            error["findings"] = (agent_review or {}).get("findings") or []
            error["actions"] = actions
            error["fix_rounds"] = dict(after)
            error["limits"] = dict(MAX_FIX_ROUNDS)
        return {"ok": False, "error": error, "data": summary}
    if review_verdict == "needs_fix":
        actions = (agent_review or {}).get("actions") or {}
        # 记一轮修复（run 不自动执行修复，只产出 actions 供上层 Agent 执行）。
        # 上限判断在调用 reviewer 之前完成（超限时根本不会走到这里）。
        after = _record_fix_round(state, actions)
        state["quality"]["reviewer"]["fix_rounds"] = dict(after)
        state["quality"]["reviewer"]["status"] = "needs_fix"
        _save_state(workdir_path, state)
        summary["fix_rounds"] = dict(after)
        return {
            "ok": False,
            "error": {
                "code": "reviewer_needs_fix",
                "message": (
                    "reviewer verdict=needs_fix：findings 已映射为 actions；"
                    "执行 `bdt translate --ids ...` / `bdt layout-set` 后"
                    "以 `bdt run --from apply` 续跑"
                ),
                "stage": "review",
                "verdict": "needs_fix",
                "findings": (agent_review or {}).get("findings") or [],
                "actions": actions,
                "fix_rounds": dict(after),
                "limits": dict(MAX_FIX_ROUNDS),
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
        "fix_rounds": dict((state.get("quality") or {}).get("fix_rounds") or {}),
        "fix_round_limits": dict(MAX_FIX_ROUNDS),
    }
    if check_data is not None:
        layout = check_data.get("layout") or {}
        links = check_data.get("links") or {}
        payload["check"] = {
            "verdict": check_data.get("verdict"),
            "blockers": len(check_data.get("blockers") or []),
            "blocker_codes": [
                item.get("code") for item in (check_data.get("blockers") or [])
            ],
            "warnings": len(check_data.get("warnings") or []),
            "metrics": check_data.get("metrics") or {},
            "reasons": check_data.get("reasons") or [],
            "unconfirmed": check_data.get("unconfirmed") or [],
            # 各子项状态（ok / not_available / not_reconstructed）
            "layout": {
                "status": layout.get("status"),
                "summary": layout.get("summary") or {},
                "reason": layout.get("reason"),
            },
            "links": {
                "status": links.get("status"),
                "missing": links.get("missing"),
                "wrong_label": links.get("wrong_label"),
                "wrong_role": links.get("wrong_role"),
                "external_unchecked": links.get("external_unchecked"),
                "targets_preserved": links.get("targets_preserved"),
                "anchors_verified": links.get("anchors_verified"),
                "reason": links.get("reason"),
            },
        }
    if agent_review is not None:
        payload["agent_review"] = {
            "verdict": agent_review.get("verdict"),
            "findings": len(agent_review.get("findings") or []),
        }
        if agent_review.get("actions"):
            payload["agent_review"]["actions"] = agent_review["actions"]
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
