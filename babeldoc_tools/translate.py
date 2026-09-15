"""翻译工具：整篇翻译 / 按 id 重译 / 译文写回 IR。"""

from __future__ import annotations

import re
from pathlib import Path

from babeldoc_tools import common
from babeldoc_tools.registry import register

ID_MARK_RE = re.compile(
    r"<!--\s*id\s*=\s*([A-Za-z0-9._-]+)\s*(?:label\s*=\s*([^>]*?))?\s*-->"
)


def _model_defaults(args: dict) -> tuple[str, str | None, int]:
    """(model, effort, timeout)；effort 为 None 时不传 --effort 给 CLI。

    优先级：显式参数 → 环境变量 → 默认；``--arg effort="none"`` 可显式关闭。
    """
    model = (
        args.get("model")
        or common.env_default("BABELDOC_TRANSLATOR_MODEL")
        or "gemini-3.8-flash-low"
    )
    effort = args.get("effort")
    if effort is None:
        effort = common.env_default("BABELDOC_TRANSLATOR_EFFORT") or "low"
    if str(effort).lower() in ("none", "default", "auto"):
        effort = None
    timeout = int(args.get("timeout") or 1800)
    return model, effort, timeout


@register(
    "translate_document",
    group="translate",
    description=(
        "整篇翻译：document.md 一次性交给翻译模型（默认 agy CLI），"
        "自动补译缺失段落，产出 agent/translated.md + usage.json"
    ),
    output_hint="translated_md / chars / missing / retried / usage / dry_run",
    input_schema={
        "type": "object",
        "properties": {
            "workdir": {"type": "string", "minLength": 1},
            "model": {"type": "string"},
            "effort": {
                "type": "string",
                "description": "thinking 档位（low/medium/high）；\"none\" 表示不传 --effort"
                "（claude-* 等不支持该参数的模型用 \"none\"）",
            },
            "timeout": {"type": "integer", "minimum": 30},
            "command": {"type": "string", "description": "翻译 CLI，默认 agy"},
            "prompt": {"type": "string", "description": "提示词名，默认 translator"},
            "translated_md": {
                "type": "string",
                "description": "已有译文 Markdown 路径（给出则不调用模型，直接导入）",
            },
            "dry_run": {"type": "boolean", "description": "只写 prompt.md，不调用模型"},
            "retry_missing": {"type": "boolean", "description": "缺失段落自动补译一次"},
        },
        "required": ["workdir"],
    },
)
def translate_document(args: dict) -> dict:
    from babeldoc.tools.agent import markdown_view

    workdir = common.require_workdir(args["workdir"])
    agent = common.agent_dir(workdir)
    document_md = agent / "document.md"
    if not document_md.exists():
        raise common.ToolError("document_missing", f"{document_md} 不存在：请先 parse_document")

    prompt_name = args.get("prompt") or "translator"
    document = document_md.read_text(encoding="utf-8")
    prompt = common.load_prompt(prompt_name, document=document)
    prompt_file = agent / "prompt.md"
    prompt_file.write_text(prompt, encoding="utf-8")

    translated_path = agent / "translated.md"
    usage: dict = {}
    imported = args.get("translated_md")
    if imported:
        imported_path = Path(imported)
        if not imported_path.exists():
            raise common.ToolError("translated_md_missing", f"{imported_path} 不存在")
        translated_path.write_text(
            imported_path.read_text(encoding="utf-8"), encoding="utf-8"
        )
    elif args.get("dry_run"):
        return {
            "dry_run": True,
            "prompt": str(prompt_file),
            "prompt_chars": len(prompt),
            "translated_md": None,
        }
    else:
        model, effort, timeout = _model_defaults(args)
        response, usage = common.run_model(
            prompt,
            model,
            effort,
            timeout_s=timeout,
            command=args.get("command") or "agy",
        )
        translated_path.write_text(response, encoding="utf-8")
        common.append_usage(workdir, "translate", usage)

    missing = markdown_view.missing_ids(
        workdir, translated_path.read_text(encoding="utf-8")
    )
    retried = None
    if missing and args.get("retry_missing", True) and not args.get("dry_run"):
        retried = retranslate_blocks(workdir, missing, feedback="", args=args)
        missing = retried["still_missing"]
    return {
        "prompt": str(prompt_file),
        "translated_md": str(translated_path),
        "chars": len(translated_path.read_text(encoding="utf-8")),
        "missing_before_retry": retried["requested_ids"] if retried else missing,
        "missing_after_retry": missing,
        "retry": retried,
        "usage": usage,
    }


# --------------------------------------------------------------------------- #
# 按 id 重译
# --------------------------------------------------------------------------- #
def retranslate_blocks(workdir: Path, ids: list[str], feedback: str = "", args: dict | None = None) -> dict:
    """补译/重译指定段落，合并进 translated.md。返回统计（不写 IR）。"""
    from babeldoc.tools.agent import markdown_view

    args = args or {}
    agent = common.agent_dir(workdir)
    ids = [pid for pid in ids if pid]
    if not ids:
        return {
            "requested_ids": [],
            "returned_ids": [],
            "still_missing": [],
            "translated_md": str(agent / "translated.md"),
            "usage": {},
        }
    retry_doc = markdown_view.render_retry_markdown(workdir, ids)
    if not retry_doc.strip():
        raise common.ToolError("ids_unknown", f"这些 id 不在解析产物中: {ids}")
    prompt = common.load_prompt(
        args.get("repair_prompt") or "translator-repair",
        document=retry_doc,
        feedback=feedback or "（无额外反馈）",
    )
    prompt_file = agent / "prompt.retry.md"
    prompt_file.write_text(prompt, encoding="utf-8")

    model, effort, timeout = _model_defaults(args)
    response, usage = common.run_model(
        prompt, model, effort, timeout_s=timeout, command=args.get("command") or "agy"
    )
    (agent / "translated.retry.md").write_text(response, encoding="utf-8")
    common.append_usage(workdir, "retry", usage)

    blocks = markdown_view.parse_translated_markdown(response)
    merged_path = merge_translated_markdown(workdir, {pid: blocks[pid] for pid in ids if pid in blocks})
    all_missing = markdown_view.missing_ids(
        workdir, Path(merged_path).read_text(encoding="utf-8")
    )
    return {
        "requested_ids": ids,
        "returned_ids": [pid for pid in ids if pid in blocks],
        "still_missing": [pid for pid in ids if pid not in blocks],
        "document_missing_ids": all_missing,
        "translated_md": merged_path,
        "prompt": str(prompt_file),
        "usage": usage,
    }


def merge_translated_markdown(workdir: Path, replacements: dict[str, tuple[str, str]]) -> str:
    """把 {id: (body, label)} 合并进 translated.md（按 sheet 顺序重排）。"""
    from babeldoc.tools.agent import markdown_view

    agent = common.agent_dir(workdir)
    translated_path = agent / "translated.md"
    existing: dict[str, tuple[str, str]] = {}
    if translated_path.exists():
        existing = markdown_view.parse_translated_markdown(
            translated_path.read_text(encoding="utf-8")
        )
    anchors = common.read_json(agent / "anchors.json", default={"rows": []}) or {}
    order = [row["id"] for row in anchors.get("rows", [])]
    existing.update(replacements)

    ordered: list[str] = [markdown_view.MD_HEADER.rstrip("\n"), ""]
    written: set[str] = set()
    for pid in order + [pid for pid in existing if pid not in order]:
        if pid in written or pid not in existing:
            continue
        body, label = existing[pid]
        label = label or _label_of(anchors, pid) or "text"
        ordered.append(f"<!-- id={pid} label={label} -->")
        ordered.append(body.strip())
        ordered.append("")
        written.add(pid)
    translated_path.write_text("\n".join(ordered), encoding="utf-8")
    return str(translated_path)


def _label_of(anchors: dict, pid: str) -> str | None:
    for row in anchors.get("rows", []):
        if row.get("id") == pid:
            return row.get("layout_label")
    return None


@register(
    "retranslate_ids",
    group="translate",
    description=(
        "按 id 补译/重译（可带 feedback），合并回 translated.md；默认同时 apply 写回 IR"
    ),
    output_hint="requested_ids / returned_ids / still_missing / apply_report / usage",
    input_schema={
        "type": "object",
        "properties": {
            "workdir": {"type": "string", "minLength": 1},
            "ids": {"type": "array", "items": {"type": "string"}, "minItems": 1},
            "feedback": {"type": "string", "description": "给翻译模型的反馈（缺陷清单/术语要求）"},
            "model": {"type": "string"},
            "effort": {
                "type": "string",
                "description": "thinking 档位（low/medium/high）；\"none\" 表示不传 --effort"
                "（claude-* 等不支持该参数的模型用 \"none\"）",
            },
            "timeout": {"type": "integer", "minimum": 30},
            "command": {"type": "string"},
            "repair_prompt": {"type": "string"},
            "apply": {"type": "boolean", "description": "重译后自动 apply（默认 true）"},
            "dry_run": {"type": "boolean"},
        },
        "required": ["workdir", "ids"],
    },
)
def retranslate_ids(args: dict) -> dict:
    workdir = common.require_workdir(args["workdir"])
    if args.get("dry_run"):
        from babeldoc.tools.agent import markdown_view

        retry_doc = markdown_view.render_retry_markdown(workdir, args["ids"])
        prompt = common.load_prompt(
            args.get("repair_prompt") or "translator-repair",
            document=retry_doc,
            feedback=args.get("feedback") or "（无额外反馈）",
        )
        path = common.agent_dir(workdir) / "prompt.retry.md"
        path.write_text(prompt, encoding="utf-8")
        return {"dry_run": True, "prompt": str(path), "ids": args["ids"]}
    result = retranslate_blocks(workdir, list(args["ids"]), args.get("feedback") or "", args)
    result["apply_report"] = None
    if args.get("apply", True):
        result["apply_report"] = apply_translation({"workdir": str(workdir)})
    return result


# --------------------------------------------------------------------------- #
# 写回 IR
# --------------------------------------------------------------------------- #
@register(
    "apply_translation",
    group="translate",
    description=(
        "校验译文 Markdown（id 对齐 + 锚点协议）并写回 IR；确定性修复锚点/双标点/注释残留"
    ),
    output_hint="ok / applied / violations / repaired / warnings / fallback_ids",
    input_schema={
        "type": "object",
        "properties": {
            "workdir": {"type": "string", "minLength": 1},
            "translated_md": {"type": "string", "description": "默认 agent/translated.md"},
        },
        "required": ["workdir"],
    },
)
def apply_translation(args: dict) -> dict:
    from babeldoc.tools.agent import markdown_view

    workdir = common.require_workdir(args["workdir"])
    md_path = Path(args.get("translated_md") or common.agent_dir(workdir) / "translated.md")
    if not md_path.exists():
        raise common.ToolError(
            "translated_md_missing", f"{md_path} 不存在：请先 translate_document"
        )
    report = markdown_view.apply_markdown(workdir, md_path)
    report["translated_md"] = str(md_path)
    common.write_json(common.agent_dir(workdir) / "apply_report.json", report)
    return report
