"""审查工具：结构性 verdict + 排版 lint + 链接审计 + 回译校验。

- ``review_document``：确定性检查（apply 报告 + 段内完整性 + 页数/目录/链接 +
  文本层占位符残留 + 标题字号）→ ``verdict: pass | needs_fix``。
- ``audit_links_tool``：调用 ``babeldoc.tools.agent.link_audit.audit_links``，
  把链接逐条审计结果落到 ``agent/link_audit.json``（只封装调用，不改算法）。
- ``check_document``：``bdt check`` 的三合一聚合——结构审查 + 排版 lint +
  链接审计，返回统一 verdict 与各子项状态。
- ``backtranslate_check``：只对高风险段落做回译（由 reviewer-fidelity agent
  产出英文），Python 侧用 Levenshtein 相似度判定。已从公开 CLI 移除，保留为
  内部 Python 函数。

聚合的 verdict 规则（``pass`` / ``needs_fix`` 二值）：

- 结构审查有 blocker，或排版 lint 有 P0/P1，或链接审计出现 missing /
  wrong_label / wrong_role / unresolved → ``needs_fix``；
- 子项 ``not_available`` / ``not_reconstructed``（产物缺失、源 PDF 不可用）
  说明"无法确认"，同样按 ``needs_fix`` 处理（只读 check 不崩，但质量门禁
  不能把"没检查"当通过）；
- ``ok`` 只在真正的执行异常时为 false：子项缺失只降级该子项。
"""

from __future__ import annotations

import re
from pathlib import Path

from babeldoc_tools import common

CJK_RE = re.compile(r"[\u4e00-\u9fff]")
LEFT_OVER_V_RE = re.compile(r"\{\s*v\s*\d+\s*\}")
LEFT_OVER_STYLE_RE = re.compile(r"<\s*/?\s*style")
ANCHOR_RE = re.compile(r"<style id='\d+'>|</style>|\{v\d+\}")


def _pdf_stats(pdf_path) -> dict:
    import pymupdf

    doc = pymupdf.open(pdf_path)
    pages = []
    text_all = []
    links = 0
    for page in doc:
        text = page.get_text()
        text_all.append(text)
        links += len(page.get_links())
    result = {
        "pages": len(doc),
        "toc_entries": len(doc.get_toc()),
        "links": links,
        "text": "\n".join(text_all),
        "page_texts": text_all,
    }
    doc.close()
    return result


def review_document(
    workdir: str,
    *,
    mono: str | None = None,
    dual: str | None = None,
    source_pdf: str | None = None,
    skip_pdf_checks: bool = False,
) -> dict:
    """结构性审查并给出 verdict：apply 报告 + 段内完整性 + 页数/目录/链接 +
    占位符残留 + 标题字号；blockers → needs_fix。"""
    import pickle

    from babeldoc.tools.agent import quality_checks

    workdir_path = common.require_workdir(workdir)
    agent = common.agent_dir(workdir_path)
    rows = common.read_jsonl(agent / "sheet.jsonl")
    targets = {
        row["id"]: row.get("target", "")
        for row in common.read_jsonl(agent / "translated.jsonl")
        if row.get("id")
    }
    apply_report = common.read_json(agent / "apply_report.json", default={}) or {}
    # 本地工作目录产物，非不可信输入
    with (agent / "state.pkl").open("rb") as handle:
        state = pickle.load(handle)  # noqa: S301

    blockers: list[dict] = []
    warnings: list[dict] = []
    metrics: dict = {
        "rows": len(rows),
        "translated_rows": len(targets),
        "applied": apply_report.get("applied"),
        "repaired": len(apply_report.get("repaired") or []),
        "fallback": len(apply_report.get("fallback_ids") or []),
        "apply_ok": bool(apply_report.get("ok")),
    }

    # ---- 1) apply 报告（真源） ------------------------------------------- #
    if apply_report.get("ok") is False:
        blockers.append(
            {
                "code": "apply_failed",
                "violations": apply_report.get("violations") or [],
                "extra_ids": apply_report.get("extra_ids") or [],
                "hint": "锚点协议/ID 对齐不通过：看 violations 决定重译哪些 id",
            }
        )
    if apply_report.get("unknown_ids"):
        blockers.append({"code": "unknown_ids", "ids": apply_report["unknown_ids"]})

    # ---- 2) 段内完整性（确定性） ---------------------------------------- #
    checks = quality_checks.check_document(
        rows,
        targets,
        missing_ids=apply_report.get("fallback_ids") or [],
        fallback_ids=apply_report.get("fallback_ids") or [],
    )
    warnings.extend(checks["warnings"])
    blockers.extend(checks["blockers"])
    metrics.update(checks["metrics"])

    # ---- 3) PDF 层核对 --------------------------------------------------- #
    skip_pdf = bool(skip_pdf_checks)
    mono = mono or _find_output(workdir_path, "mono")
    dual = dual or _find_output(workdir_path, "dual")
    if not skip_pdf and (mono or dual):
        source_pdf = source_pdf or state.get("pdf_path")
        source_stats = _pdf_stats(source_pdf) if source_pdf and Path(source_pdf).exists() else None
        for kind, path in (("mono", mono), ("dual", dual)):
            if not path or not Path(path).exists():
                continue
            stats = _pdf_stats(path)
            metrics[f"pages_{kind}"] = stats["pages"]
            metrics[f"toc_{kind}"] = stats["toc_entries"]
            metrics[f"links_{kind}"] = stats["links"]
            if source_stats:
                if stats["pages"] != source_stats["pages"]:
                    blockers.append(
                        {
                            "code": "page_count_mismatch",
                            "pdf": kind,
                            "expected": source_stats["pages"],
                            "actual": stats["pages"],
                        }
                    )
                if source_stats["toc_entries"] and stats["toc_entries"] < source_stats["toc_entries"]:
                    blockers.append(
                        {
                            "code": "toc_missing",
                            "pdf": kind,
                            "expected": source_stats["toc_entries"],
                            "actual": stats["toc_entries"],
                        }
                    )
                if source_stats["links"] and stats["links"] < source_stats["links"]:
                    blockers.append(
                        {
                            "code": "links_missing",
                            "pdf": kind,
                            "expected": source_stats["links"],
                            "actual": stats["links"],
                        }
                    )
            leftovers_v = len(LEFT_OVER_V_RE.findall(stats["text"]))
            leftovers_style = len(LEFT_OVER_STYLE_RE.findall(stats["text"]))
            metrics[f"placeholder_leftovers_{kind}"] = leftovers_v + leftovers_style
            if leftovers_v or leftovers_style:
                blockers.append(
                    {
                        "code": "placeholder_leftover",
                        "pdf": kind,
                        "v_placeholders": leftovers_v,
                        "style_tags": leftovers_style,
                    }
                )

    if mono:
        geometry = common.read_json(agent / "layout_geometry.json", default={}) or {}
        title_shrink = _title_shrink(geometry)
        metrics["p1_title_shrink"] = len(title_shrink)
        if title_shrink:
            warnings.append(
                {
                    "code": "title_font_shrink",
                    "sev": "P1",
                    "items": title_shrink[:10],
                    "hint": "标题字号明显小于源文：检查 _bump_title_font_size / 布局覆盖",
                }
            )

    verdict = "needs_fix" if blockers else "pass"
    result = {
        "verdict": verdict,
        "blockers": blockers,
        "warnings": warnings,
        "metrics": metrics,
        "apply_report": apply_report,
    }
    report_path = common.write_json(agent / "review_verdict.json", result)
    result["report"] = str(report_path)
    return result


def _title_shrink(geometry: dict) -> list[dict]:
    out = []
    for para in geometry.get("paragraphs") or []:
        if (para.get("layout_label") or "").lower() not in (
            "title",
            "doc_title",
            "paragraph_title",
        ):
            continue
        src = para.get("src_font_size")
        rendered = para.get("mode_font_size")
        if src and rendered and rendered / src < 0.85:
            out.append(
                {
                    "id": para["id"],
                    "page": para.get("page"),
                    "src_font_size": src,
                    "mode_font_size": rendered,
                    "ratio": round(rendered / src, 3),
                }
            )
    return out


def _find_output(workdir: Path, suffix: str) -> str | None:
    for base in (workdir / "output", workdir):
        if not base.is_dir():
            continue
        for path in sorted(base.glob(f"*.{suffix}.pdf")):
            return str(path)
    return None


# --------------------------------------------------------------------------- #
# 链接审计（封装 babeldoc.tools.agent.link_audit.audit_links）
# --------------------------------------------------------------------------- #
#: 链接子项里视为"需要修复"的计数键；``external_unchecked`` / ``no_source_text``
#: 是"未确认"类信息，不单独触发 needs_fix（外部地址本就只校验是否保留）。
LINK_PROBLEM_KEYS = ("missing", "wrong_label", "wrong_role", "unresolved")

#: 链接审计结果里前 N 条问题进 JSON（完整清单在 agent/link_audit.json）。
LINK_PROBLEM_LIMIT = 20


def _existing_path(raw, workdir_path: Path) -> str | None:
    """把记录里的路径解析成真实存在的路径（相对路径先按 CWD、再按 workdir 试）。"""
    if not raw:
        return None
    candidate = Path(raw)
    if candidate.exists():
        return str(candidate)
    fallback = workdir_path / candidate
    if fallback.exists():
        return str(fallback)
    return None


def state_pdf_path(workdir) -> str | None:
    """从 ``agent/state.pkl`` 取源 PDF 路径（parse 阶段落的 ``pdf_path``）。"""
    import pickle

    workdir_path = Path(workdir)
    state_file = common.agent_dir(workdir_path) / "state.pkl"
    if not state_file.exists():
        return None
    try:
        with state_file.open("rb") as handle:
            state = pickle.load(handle)  # noqa: S301 - 本地 workdir 私有产物
    except Exception:  # noqa: BLE001 - 取不到源 PDF 不应阻断其余子项
        return None
    raw = state.get("pdf_path") if isinstance(state, dict) else None
    if not raw:
        return None
    return _existing_path(raw, workdir_path) or str(raw)


def reconstruct_mono_pdf(workdir) -> str | None:
    """从 ``agent/reconstruct_report.json`` 取 mono PDF 路径。"""
    workdir_path = Path(workdir)
    recon = common.read_json(
        common.agent_dir(workdir_path) / "reconstruct_report.json", default={}
    ) or {}
    return recon.get("mono_pdf") or recon.get("dual_pdf")


def _link_summary(report: dict, report_path: Path) -> dict:
    summary = report.get("summary") or {}
    findings = report.get("findings") or []
    problems = [
        {
            "page": item.get("page"),
            "logical_id": item.get("logical_id"),
            "anchor_status": item.get("anchor_status"),
            "source_text": item.get("source_text"),
            "output_text": item.get("output_text"),
        }
        for item in findings
        if item.get("anchor_status") in ("missing", "wrong_label", "wrong_role", "unverified")
    ]
    return {
        "status": "ok",
        "source_pdf": report.get("source_pdf"),
        "pdf": report.get("pdf"),
        "missing": summary.get("missing", 0),
        "wrong_label": summary.get("wrong_label", 0),
        "wrong_role": summary.get("wrong_role", 0),
        "unverified": summary.get("unverified", 0),
        "unresolved": summary.get("unverified", 0),
        "no_source_text": summary.get("no_source_text", 0),
        "verified": summary.get("verified", 0),
        "external_unchecked": summary.get("external_unchecked", 0),
        "source_invalid": summary.get("source_invalid", 0),
        "output_invalid": summary.get("output_invalid", 0),
        "source_links": summary.get("source_links", 0),
        "output_annotations": summary.get("output_annotations", 0),
        "preserved": summary.get("preserved", 0),
        "targets_preserved": bool(report.get("targets_preserved")),
        "anchors_verified": bool(report.get("anchors_verified")),
        "problem_count": len(problems),
        "problems": problems[:LINK_PROBLEM_LIMIT],
        "report": str(report_path),
    }


def audit_links_tool(
    workdir: str,
    *,
    mono: str | None = None,
    source_pdf: str | None = None,
    report_path: str | None = None,
) -> dict:
    """把源 PDF 与 mono PDF 的逐条链接审计落到 ``agent/link_audit.json``。

    - 源 PDF 取自 ``state.pkl`` 的 ``pdf_path``（可显式 ``source_pdf`` 覆盖）；
    - mono PDF 取自 ``agent/reconstruct_report.json``（可显式 ``mono`` 覆盖）；
    - mono PDF 不存在 → ``{"status": "not_reconstructed"}``（不报错）；
    - 源 PDF 不可用 / 页数序列不一致 → ``{"status": "not_available"}``；
    - 成功 → 计数 + ``targets_preserved`` / ``anchors_verified`` 语义原样透出。

    不改 ``audit_links`` 的算法与判定口径，只做调用封装。
    """
    from babeldoc.tools.agent import link_audit

    workdir_path = common.require_workdir(workdir)
    agent = common.agent_dir(workdir_path)

    mono_raw = mono or reconstruct_mono_pdf(workdir_path) or _find_output(workdir_path, "mono")
    mono_path = _existing_path(mono_raw, workdir_path)
    if not mono_path:
        return {
            "status": "not_reconstructed",
            "reason": "reconstruct_report.json 里没有可用的 mono PDF：先跑 bdt build",
            "mono_pdf": str(mono_raw) if mono_raw else None,
        }

    source_raw = source_pdf or state_pdf_path(workdir_path)
    source_path = _existing_path(source_raw, workdir_path)
    if not source_path:
        return {
            "status": "not_available",
            "reason": f"源 PDF 不可用（来自 state.pkl 的 pdf_path）：{source_raw}",
            "pdf": mono_path,
        }

    target = Path(report_path) if report_path else (agent / "link_audit.json")
    try:
        report = link_audit.audit_links(source_path, mono_path, report_path=target)
    except ValueError as exc:
        # audit_links 要求 mono 与源页数序列一致；不满足时该子项不可确认，
        # 但不影响结构审查与排版 lint 的结论。
        return {
            "status": "not_available",
            "reason": str(exc),
            "source_pdf": source_path,
            "pdf": mono_path,
        }
    return _link_summary(report, target)


# --------------------------------------------------------------------------- #
# 排版 lint 子项
# --------------------------------------------------------------------------- #
def layout_subitem(workdir) -> dict:
    """排版 lint 子项：缺 ``layout_geometry.json`` → ``not_available``。"""
    from babeldoc_tools import layout as layout_tool

    workdir_path = common.require_workdir(workdir)
    agent = common.agent_dir(workdir_path)
    geometry = common.read_json(agent / "layout_geometry.json", default=None)
    if not isinstance(geometry, dict) or not geometry.get("paragraphs"):
        return {
            "status": "not_available",
            "reason": "layout_geometry.json 不存在或为空：先跑 bdt build",
        }
    result = layout_tool.layout_lint(str(workdir_path))
    return {
        "status": "ok",
        "summary": result.get("summary") or {},
        "counts": result.get("counts") or {},
        "findings": result.get("findings") or [],
        "report": str(agent / "layout_lint.json"),
    }


# --------------------------------------------------------------------------- #
# 三合一聚合：``bdt check``
# --------------------------------------------------------------------------- #
def _aggregate_verdict(review: dict, layout: dict, links: dict) -> tuple[str, list[str], list[str]]:
    """返回 ``(verdict, reasons, unconfirmed)``；needs_fix 由三者最严重的一档决定。"""
    reasons: list[str] = []
    unconfirmed: list[str] = []

    blockers = review.get("blockers") or []
    for blocker in blockers:
        reasons.append(f"结构审查 blocker `{blocker.get('code')}`")
    if review.get("verdict") == "needs_fix" and not blockers:
        reasons.append("结构审查 verdict=needs_fix")

    if layout.get("status") != "ok":
        unconfirmed.append("layout")
        reasons.append(f"排版 lint 不可用（{layout.get('status')}）：{layout.get('reason')}")
    else:
        severity = (layout.get("summary") or {}).get("by_severity") or {}
        if severity.get("P0") or severity.get("P1"):
            reasons.append(
                f"排版 lint 缺陷 P0={severity.get('P0', 0)} P1={severity.get('P1', 0)}"
            )

    if links.get("status") != "ok":
        unconfirmed.append("links")
        reasons.append(f"链接审计不可用（{links.get('status')}）：{links.get('reason')}")
    else:
        for key in LINK_PROBLEM_KEYS:
            if links.get(key):
                reasons.append(f"链接审计 {key}={links[key]}")

    return ("needs_fix" if reasons else "pass"), reasons, unconfirmed


def check_document(
    workdir: str,
    *,
    mono: str | None = None,
    dual: str | None = None,
    source_pdf: str | None = None,
    skip_pdf_checks: bool = False,
    strict: bool = False,
) -> dict:
    """三合一质量门禁：结构审查 → 排版 lint → 链接审计，返回合并 JSON。

    子项产物缺失只让该子项 ``{"status": "not_available"}``（或
    ``not_reconstructed``），不影响其余子项，``ok`` 仍为 true；真正的执行异常
    会让整个调用以错误信封返回。``strict`` 只回填到结果里（供 CLI 决定退出码）。
    """
    workdir_path = common.require_workdir(workdir)
    review = review_document(
        workdir,
        mono=mono,
        dual=dual,
        source_pdf=source_pdf,
        skip_pdf_checks=skip_pdf_checks,
    )
    layout = layout_subitem(workdir_path)
    links = audit_links_tool(str(workdir_path), mono=mono, source_pdf=source_pdf)
    verdict, reasons, unconfirmed = _aggregate_verdict(review, layout, links)
    return {
        "verdict": verdict,
        "blockers": review.get("blockers") or [],
        "warnings": review.get("warnings") or [],
        "metrics": review.get("metrics") or {},
        "layout": layout,
        "links": links,
        "reasons": reasons,
        "unconfirmed": unconfirmed,
        "strict": bool(strict),
        "apply_report": review.get("apply_report") or {},
        "report": review.get("report"),
    }


# --------------------------------------------------------------------------- #
# 回译校验
# --------------------------------------------------------------------------- #
HIGH_RISK_CODES = {
    "intra_paragraph_truncated",
    "suspect_merge",
    "low_cjk",
    "sentence_end_mismatch",
    "empty_target",
    "fallback_to_source",
    "missing_ids",
}


def high_risk_ids(workdir: Path, limit: int = 40) -> list[str]:
    """高风险段落：确定性 warning 命中的 id + 空译/回退段落。"""
    agent = common.agent_dir(workdir)
    verdict = common.read_json(agent / "review_verdict.json", default={}) or {}
    ids: list[str] = []

    def add(candidate):
        if candidate and candidate not in ids:
            ids.append(candidate)

    for warning in verdict.get("warnings") or []:
        if warning.get("code") not in HIGH_RISK_CODES:
            continue
        if warning.get("id"):
            add(warning["id"])
        for pid in warning.get("ids") or []:
            add(pid)
    for blocker in verdict.get("blockers") or []:
        if blocker.get("code") in HIGH_RISK_CODES:
            for pid in blocker.get("ids") or []:
                add(pid)
    if not ids:
        # 没有 verdict 时退回"空译/极短"启发式
        rows = {row["id"]: row for row in common.read_jsonl(agent / "sheet.jsonl")}
        targets = {row["id"]: row.get("target", "") for row in common.read_jsonl(agent / "translated.jsonl")}
        for pid, target in targets.items():
            if not ANCHOR_RE.sub("", target).strip() and ANCHOR_RE.sub(
                "", rows.get(pid, {}).get("source", "")
            ).strip():
                add(pid)
    return ids[:limit]


def backtranslate_check(
    workdir: str,
    *,
    ids: list[str] | None = None,
    max_ids: int = 40,
    threshold: float = 0.55,
    translator: str | None = None,
    timeout: int | None = None,
    dry_run: bool = False,
    backtranslation: dict | None = None,
) -> dict:
    """回译校验：对指定 id（默认高风险段）由 reviewer agent 回译英文，
    Python 侧算 Levenshtein 相似度并判定是否需要重译。

    已从公开 CLI 移除；保留为内部 Python 函数。调用方必须显式传入回译命令
    （``translator``）——本函数不再有默认 agy 行为，命令走 stdin/stdout 协议。
    """
    from babeldoc.tools.agent import markdown_view

    workdir_path = common.require_workdir(workdir)
    agent = common.agent_dir(workdir_path)
    threshold = float(threshold if threshold is not None else 0.55)
    ids = ids or high_risk_ids(workdir_path, limit=int(max_ids or 40))
    sources = {
        row["id"]: row.get("source", "") for row in common.read_jsonl(agent / "sheet.jsonl")
    }
    targets = {
        row["id"]: row.get("target", "")
        for row in common.read_jsonl(agent / "translated.jsonl")
    }
    ids = [pid for pid in ids if pid in sources and pid in targets]
    if not ids:
        return {"ids": [], "per_id": [], "needs_retranslate_ids": [], "summary": "无高风险段落"}

    items = [
        {
            "id": pid,
            "translated": targets[pid],
        }
        for pid in ids
    ]
    document = "\n".join(
        f"<!-- id={item['id']} -->\n{markdown_view.canonical_to_markdown(item['translated'])}"
        for item in items
    )
    prompt_text = common.load_prompt("reviewer-fidelity", document=document, ids=", ".join(ids))
    prompt_file = agent / "prompt.backtranslate.md"
    prompt_file.write_text(prompt_text, encoding="utf-8")

    if backtranslation:
        back = {
            pid: (backtranslation.get(pid) or {}).get("text", backtranslation.get(pid, ""))
            if isinstance(backtranslation.get(pid), dict)
            else backtranslation.get(pid, "")
            for pid in ids
        }
    elif dry_run:
        return {"dry_run": True, "prompt": str(prompt_file), "ids": ids}
    else:
        if not translator:
            raise common.ToolError(
                "translator_missing",
                "backtranslate_check 需要显式 translator 命令"
                "（stdin 收提示词、stdout 出回译文本，经 BDT_TRANSLATOR 或调用方传入）",
            )
        response = common.run_translator(
            prompt_text, translator, timeout_s=int(timeout or 900)
        )
        (agent / "backtranslation.raw.md").write_text(response, encoding="utf-8")
        back = _parse_backtranslation(response, ids)

    per_id = []
    for pid in ids:
        source = sources[pid]
        text = back.get(pid) or ""
        similarity = similarity_ratio(source, text)
        per_id.append(
            {
                "id": pid,
                "similarity": round(similarity, 3),
                "verdict": "needs_retranslate" if similarity < threshold else "ok",
                "source": ANCHOR_RE.sub("", source)[:120],
                "backtranslation": ANCHOR_RE.sub("", text)[:120],
            }
        )
    per_id.sort(key=lambda item: item["similarity"])
    result = {
        "ids": ids,
        "threshold": threshold,
        "per_id": per_id,
        "needs_retranslate_ids": [
            item["id"] for item in per_id if item["verdict"] == "needs_retranslate"
        ],
        "prompt": str(prompt_file),
    }
    common.write_json(agent / "backtranslation_check.json", result)
    return result


def _parse_backtranslation(response: str, ids: list[str]) -> dict[str, str]:
    """从模型输出中提取 {id: 回译英文}（容忍 JSON 对象/JSONL/行内 id 标记）。"""
    import json

    text = response.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n|```$", "", text).strip()
    try:
        payload = json.loads(text)
        if isinstance(payload, dict):
            if "items" in payload and isinstance(payload["items"], list):
                return {
                    item.get("id"): item.get("backtranslation") or item.get("text") or ""
                    for item in payload["items"]
                    if item.get("id")
                }
            if "id" in payload:
                return {payload["id"]: payload.get("backtranslation", "")}
            return {str(k): str(v) for k, v in payload.items()}
        if isinstance(payload, list):
            return {
                item.get("id"): item.get("backtranslation") or item.get("text") or ""
                for item in payload
                if isinstance(item, dict) and item.get("id")
            }
    except json.JSONDecodeError:
        pass
    # 退化：按 <!-- id=... --> 分块
    out: dict[str, str] = {}
    blocks = re.split(r"<!--\s*id\s*=\s*([A-Za-z0-9._-]+)\s*-->", text)
    for index in range(1, len(blocks) - 1, 2):
        out[blocks[index]] = blocks[index + 1].strip()
    for pid in ids:
        if pid in out:
            continue
        match = re.search(rf"{re.escape(pid)}\s*[:：]\s*(.+)", text)
        if match:
            out[pid] = match.group(1).strip()
    return out


def similarity_ratio(source: str, backtranslation: str) -> float:
    """归一化后的 Levenshtein 相似度（0~1）。"""
    try:
        import Levenshtein
    except ImportError:  # pragma: no cover - 依赖缺失时退回 difflib
        import difflib

        return difflib.SequenceMatcher(
            None, _normalize(source), _normalize(backtranslation)
        ).ratio()
    return Levenshtein.ratio(_normalize(source), _normalize(backtranslation))


def _normalize(text: str) -> str:
    text = ANCHOR_RE.sub(" ", text or "")
    text = text.lower()
    text = re.sub(r"[\u4e00-\u9fff]", " ", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()
