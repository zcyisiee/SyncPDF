"""报告工具：把一次完整编排收敛成 FINAL_REPORT.md。"""

from __future__ import annotations

import datetime
from pathlib import Path

from babeldoc_tools import common
from babeldoc_tools.registry import register


@register(
    "report",
    group="report",
    description="汇总 token 用量 / apply 指标 / 审查 verdict / lint 前后对比 → FINAL_REPORT.md",
    output_hint="report(path) / sections / leftovers",
    input_schema={
        "type": "object",
        "properties": {
            "workdir": {"type": "string", "minLength": 1},
            "output_dir": {"type": "string"},
            "title": {"type": "string"},
            "notes": {"type": "string", "description": "追加的备注（Markdown）"},
        },
        "required": ["workdir"],
    },
)
def report(args: dict) -> dict:
    workdir = common.require_workdir(args["workdir"])
    agent = common.agent_dir(workdir)
    usage = common.read_json(agent / "usage.json", default={}) or {}
    apply_report = common.read_json(agent / "apply_report.json", default={}) or {}
    verdict = common.read_json(agent / "review_verdict.json", default={}) or {}
    lint = common.read_json(agent / "layout_lint.json", default={}) or {}
    lint_history = common.read_json(agent / "lint_history.json", default=[]) or []
    overrides = common.read_json(agent / "layout_overrides.json", default={}) or {}
    backtranslation = common.read_json(
        agent / "backtranslation_check.json", default={}
    ) or {}
    geometry = common.read_json(agent / "layout_geometry.json", default={}) or {}
    recon = common.read_json(agent / "reconstruct_report.json", default={}) or {}

    leftovers = _leftovers(verdict, lint, apply_report, backtranslation)
    lines: list[str] = []
    title = args.get("title") or f"文档翻译报告（{workdir.name}）"
    lines.append(f"# {title}")
    lines.append("")
    lines.append(f"- 工作目录：`{workdir}`")
    lines.append(f"- 生成时间：{datetime.datetime.now().isoformat(timespec='seconds')}")
    if geometry.get("pages"):
        lines.append(f"- 页数：{geometry['pages']}；段落：{len(geometry.get('paragraphs') or [])}")
    lines.append("")

    # ---- token 用量 ------------------------------------------------------- #
    lines.append("## 一、Token 用量")
    lines.append("")
    usage_rows = _usage_rows(usage)
    if usage_rows:
        lines.append("| 调用 | 模型 | input | output | cache_read | total | 耗时(s) |")
        lines.append("|---|---|---:|---:|---:|---:|---:|")
        total = 0
        for label, item in usage_rows:
            tokens = item.get("total_tokens") or 0
            total += tokens if isinstance(tokens, (int, float)) else 0
            lines.append(
                f"| {label} | {item.get('model', '-')} "
                f"| {item.get('input_tokens', item.get('prompt_tokens', '-'))} "
                f"| {item.get('output_tokens', item.get('completion_tokens', '-'))} "
                f"| {item.get('cache_read_input_tokens', item.get('cache_read_tokens', item.get('cache_hit_prompt_tokens', 0)))} "
                f"| {tokens} | {_round(item.get('duration_seconds'))} |"
            )
        lines.append(f"| **合计** |  |  |  |  | **{total}** |  |")
    else:
        lines.append("（无 usage.json：本次未调用外部模型或因故未记录）")
    lines.append("")

    # ---- apply ------------------------------------------------------------ #
    lines.append("## 二、写回 IR（apply）")
    lines.append("")
    if apply_report:
        lines.append(
            f"- ok={apply_report.get('ok')} applied={apply_report.get('applied')} "
            f"锚点修复={len(apply_report.get('repaired') or [])} "
            f"回退原文={len(apply_report.get('fallback_ids') or [])} "
            f"violations={len(apply_report.get('violations') or [])}"
        )
        for item in (apply_report.get("repaired") or [])[:10]:
            lines.append(f"  - 修复 {item.get('id')}（{item.get('mode')}）")
    else:
        lines.append("（无 apply_report.json）")
    lines.append("")

    # ---- 审查 ------------------------------------------------------------- #
    lines.append("## 三、稳定性审查")
    lines.append("")
    if verdict:
        metrics = verdict.get("metrics") or {}
        lines.append(f"- **verdict: `{verdict.get('verdict')}`**")
        lines.append(
            f"- 段落：{metrics.get('rows')} 行 / {metrics.get('translated_rows')} 译文；"
            f"中位长度比 {metrics.get('median_len_ratio')}"
        )
        lines.append(
            f"- 页数/目录/链接：mono {metrics.get('pages_mono')}/{metrics.get('toc_mono')}/"
            f"{metrics.get('links_mono')}，dual {metrics.get('pages_dual')}/"
            f"{metrics.get('toc_dual')}/{metrics.get('links_dual')}"
        )
        lines.append(
            f"- 占位符残留：{metrics.get('placeholder_leftovers_mono', 0)} (mono) / "
            f"{metrics.get('placeholder_leftovers_dual', 0)} (dual)；"
            f"标题字号塌陷：{metrics.get('p1_title_shrink', 0)}"
        )
        for blocker in verdict.get("blockers") or []:
            lines.append(
                f"  - **P0 {blocker.get('code')}**：{blocker.get('ids') or blocker}"
            )
        for warning in (verdict.get("warnings") or [])[:15]:
            target = warning.get("id") or warning.get("ids")
            lines.append(
                f"  - {warning.get('sev', 'P2')} {warning.get('code')}"
                + (f"（{target}）" if target else "")
            )
    else:
        lines.append("（无 review_verdict.json）")
    lines.append("")

    # ---- 回译 ------------------------------------------------------------- #
    if backtranslation:
        lines.append("## 四、回译校验")
        lines.append("")
        lines.append(
            f"- 抽查 {len(backtranslation.get('ids') or [])} 段，阈值 "
            f"{backtranslation.get('threshold')}；需重译 "
            f"{len(backtranslation.get('needs_retranslate_ids') or [])} 段"
        )
        for item in (backtranslation.get("per_id") or [])[:10]:
            lines.append(
                f"  - {item['id']}：相似度 {item['similarity']} → {item['verdict']}"
            )
        lines.append("")

    # ---- 排版 ------------------------------------------------------------- #
    lines.append("## 五、排版微调")
    lines.append("")
    if lint_history:
        first, last = lint_history[0], lint_history[-1]
        lines.append("| 时间 | P0 | P1 | P2 | 生效覆盖 | 主要 code |")
        lines.append("|---|---:|---:|---:|---:|---|")
        for entry in lint_history[-8:]:
            summary = entry.get("summary") or {}
            severity = summary.get("by_severity") or {}
            counts = entry.get("counts") or {}
            overrides_digest = summary.get("overrides") or {}
            applied = len(overrides_digest.get("paragraphs") or {}) + len(
                overrides_digest.get("pages") or {}
            )
            lines.append(
                f"| {entry.get('ts')} | {severity.get('P0', 0)} | {severity.get('P1', 0)} "
                f"| {severity.get('P2', 0)} | {applied} | {', '.join(sorted(counts))} |"
            )
        lines.append("")
        lines.append(
            f"- 首次 lint：{first.get('counts')}；最后一次：{last.get('counts')}"
        )
    else:
        lines.append("（未跑 layout_lint）")
    paragraphs = overrides.get("paragraphs") or {}
    pages = overrides.get("pages") or {}
    if paragraphs or pages:
        lines.append(f"- 生效覆盖：{len(paragraphs)} 段 / {len(pages)} 页")
        for pid, patch in list(paragraphs.items())[:15]:
            lines.append(f"  - `{pid}`：{patch}")
        for page, patch in list(pages.items())[:5]:
            lines.append(f"  - 第 {page} 页：{patch}")
    else:
        lines.append("- 生效覆盖：无（默认布局）")
    if geometry.get("warnings"):
        lines.append(f"- 覆盖告警：{geometry['warnings'][:5]}")
    lines.append("")

    if recon:
        lines.append("```json")
        lines.append(str(recon)[:800])
        lines.append("```")
        lines.append("")

    # ---- 遗留项 ----------------------------------------------------------- #
    lines.append("## 六、遗留项")
    lines.append("")
    if leftovers:
        for item in leftovers:
            lines.append(f"- {item}")
    else:
        lines.append("- 无（所有确定性检查通过；排版 lint 仅剩 P2 信息项）")
    if args.get("notes"):
        lines.append("")
        lines.append(args["notes"])
    lines.append("")

    output_dir = Path(args.get("output_dir") or workdir)
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "FINAL_REPORT.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return {
        "report": str(path),
        "leftovers": leftovers,
        "metrics": {
            "verdict": verdict.get("verdict"),
            "lint_counts": (lint_history[-1] or {}).get("counts") if lint_history else None,
            "overrides": {"paragraphs": len(paragraphs), "pages": len(pages)},
        },
    }


def _round(value):
    if isinstance(value, (int, float)):
        return round(value, 1)
    return value if value is not None else "-"


def _usage_rows(usage: dict) -> list[tuple[str, dict]]:
    """把 usage.json 拆成 [(阶段名, 用量)]。

    兼容两种历史格式：
    - 新格式：``{"translate": {...}, "retry": {...}}``
    - 旧格式（`markdown_translate.py` 写的）：
      ``{"model": "…", "input_tokens": …, "retry": {...}}``（顶层就是一次调用）
    """
    if not usage:
        return []
    nested = {k: v for k, v in usage.items() if isinstance(v, dict)}
    flat = {
        k: v
        for k, v in usage.items()
        if not isinstance(v, dict) and k not in ("model", "effort")
    }
    rows: list[tuple[str, dict]] = []
    if flat.get("total_tokens") or flat.get("input_tokens"):
        rows.append(("translate", {**flat, "model": usage.get("model", "-")}))
    for key, item in nested.items():
        rows.append((key, item))
    if not rows:
        rows.append(("translate", usage))
    return rows


def _leftovers(verdict: dict, lint: dict, apply_report: dict, backtranslation: dict) -> list[str]:
    items: list[str] = []
    for blocker in verdict.get("blockers") or []:
        items.append(f"blocker `{blocker.get('code')}`：{blocker.get('ids') or blocker}")
    for warning in verdict.get("warnings") or []:
        if warning.get("code") in (
            "intra_paragraph_truncated",
            "low_cjk",
            "sentence_end_mismatch",
            "suspect_merge",
        ):
            items.append(
                f"warning `{warning['code']}`：{warning.get('id') or warning.get('ids')}"
            )
    for pid in backtranslation.get("needs_retranslate_ids") or []:
        items.append(f"回译相似度不足，建议重译：{pid}")
    for pid in apply_report.get("fallback_ids") or []:
        items.append(f"未翻译回退原文：{pid}")
    summary = (lint or {}).get("summary") or {}
    if summary.get("defects"):
        codes = (lint or {}).get("counts") or {}
        detail = ", ".join(f"{code}×{count}" for code, count in sorted(codes.items()))
        lines_ = f"排版缺陷 {summary['defects']} 项（{detail}，详见 layout_lint.json）"
        items.append(lines_)
    return items
