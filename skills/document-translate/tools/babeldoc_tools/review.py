"""审查工具：结构性 verdict + 回译校验。

- ``review_document``：确定性检查（apply 报告 + 段内完整性 + 页数/目录/链接 +
  文本层占位符残留 + 标题字号）→ ``verdict: pass | needs_fix``。
- ``backtranslate_check``：只对高风险段落做回译（由 reviewer-fidelity agent
  产出英文），Python 侧用 Levenshtein 相似度判定。
"""

from __future__ import annotations

import re
from pathlib import Path

from babeldoc_tools import common
from babeldoc_tools.registry import register

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


@register(
    "review_document",
    group="review",
    description=(
        "结构性审查并给出 verdict：apply 报告 + 段内完整性 + 页数/目录/链接 + "
        "占位符残留 + 标题字号；blockers → needs_fix"
    ),
    output_hint="verdict / blockers / warnings / metrics / report(path)",
    input_schema={
        "type": "object",
        "properties": {
            "workdir": {"type": "string", "minLength": 1},
            "mono": {"type": "string", "description": "mono PDF 路径（可选，用于 PDF 层核对）"},
            "dual": {"type": "string"},
            "source_pdf": {"type": "string", "description": "原文 PDF（默认取 state.pkl 内路径）"},
            "skip_pdf_checks": {"type": "boolean"},
        },
        "required": ["workdir"],
    },
)
def review_document(args: dict) -> dict:
    import pickle

    from babeldoc.tools.agent import quality_checks

    workdir = common.require_workdir(args["workdir"])
    agent = common.agent_dir(workdir)
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
    skip_pdf = bool(args.get("skip_pdf_checks"))
    mono = args.get("mono") or _find_output(workdir, "mono")
    dual = args.get("dual") or _find_output(workdir, "dual")
    if not skip_pdf and (mono or dual):
        source_pdf = args.get("source_pdf") or state.get("pdf_path")
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


@register(
    "backtranslate_check",
    group="review",
    description=(
        "回译校验：对指定 id（默认高风险段）由 reviewer agent 回译英文，"
        "Python 侧算 Levenshtein 相似度并判定是否需要重译"
    ),
    output_hint="per_id / needs_retranslate_ids / usage / prompt(path)",
    input_schema={
        "type": "object",
        "properties": {
            "workdir": {"type": "string", "minLength": 1},
            "ids": {"type": "array", "items": {"type": "string"}},
            "max_ids": {"type": "integer", "minimum": 1, "maximum": 200},
            "threshold": {"type": "number", "minimum": 0, "maximum": 1},
            "model": {"type": "string"},
            "effort": {
                "type": "string",
                "description": "thinking 档位；\"none\" = 不传 --effort（claude-* 等）",
            },
            "timeout": {"type": "integer", "minimum": 30},
            "command": {"type": "string"},
            "dry_run": {"type": "boolean"},
            "backtranslation": {
                "type": "object",
                "description": "{id: 回译英文}：给出则不调用模型（供外部 reviewer 产出）",
            },
        },
        "required": ["workdir"],
    },
)
def backtranslate_check(args: dict) -> dict:
    from babeldoc.tools.agent import markdown_view

    workdir = common.require_workdir(args["workdir"])
    agent = common.agent_dir(workdir)
    threshold = float(args.get("threshold") if args.get("threshold") is not None else 0.55)
    ids = args.get("ids") or high_risk_ids(
        workdir, limit=int(args.get("max_ids") or 40)
    )
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
    prompt = common.load_prompt("reviewer-fidelity", document=document, ids=", ".join(ids))
    prompt_file = agent / "prompt.backtranslate.md"
    prompt_file.write_text(prompt, encoding="utf-8")

    provided = args.get("backtranslation")
    usage: dict = {}
    if provided:
        back = {
            pid: (provided.get(pid) or {}).get("text", provided.get(pid, ""))
            if isinstance(provided.get(pid), dict)
            else provided.get(pid, "")
            for pid in ids
        }
    elif args.get("dry_run"):
        return {"dry_run": True, "prompt": str(prompt_file), "ids": ids}
    else:
        model = (
            args.get("model")
            or common.env_default("BABELDOC_REVIEWER_MODEL")
            or common.env_default("BABELDOC_TRANSLATOR_MODEL")
            or "gemini-3.8-flash-low"
        )
        effort = args.get("effort") or common.env_default("BABELDOC_REVIEWER_EFFORT") or "low"
        response, usage = common.run_model(
            prompt,
            model,
            effort,
            timeout_s=int(args.get("timeout") or 900),
            command=args.get("command") or "agy",
        )
        (agent / "backtranslation.raw.md").write_text(response, encoding="utf-8")
        common.append_usage(workdir, "backtranslate", usage)
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
        "usage": usage,
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
