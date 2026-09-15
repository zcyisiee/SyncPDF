"""翻译工具：整篇翻译 / 按 id 重译 / 译文写回 IR。

翻译命令是**用户指定的子进程**（``--translator`` / ``BDT_TRANSLATOR``）：提示词
从 stdin 进，译文从 stdout 出。工具层不再拼 agy 参数，也不解析 JSON/usage。
"""

from __future__ import annotations

import re
from pathlib import Path

from babeldoc_tools import common

ID_MARK_RE = re.compile(
    r"<!--\s*id\s*=\s*([A-Za-z0-9._-]+)\s*(?:label\s*=\s*([^>]*?))?\s*-->"
)


def _resolve_translator(translator: str | None) -> str:
    """解析翻译命令：显式 ``--translator`` 优先，其次 ``BDT_TRANSLATOR`` 环境变量。

    两者都没有时报 ``translator_missing``，并提示两条免命令路径：导入已有译文
    （``--markdown``）或只取提示词（``--prompt-only``）。
    """
    resolved = translator or common.env_default("BDT_TRANSLATOR")
    if not resolved:
        raise common.ToolError(
            "translator_missing",
            "未指定翻译命令：请用 --translator <command> 或设置 BDT_TRANSLATOR "
            "环境变量（命令从 stdin 读提示词、把译文写到 stdout）；"
            "也可用 --markdown <file> 导入已有译文，或用 --prompt-only 只写出提示词",
        )
    return resolved


def translate_document(
    workdir: str,
    *,
    ids: list[str] | None = None,
    feedback: str | None = None,
    markdown: str | None = None,
    prompt_only: bool = False,
    translator: str | None = None,
    timeout: int | None = None,
    prompt: str | None = None,
    repair_prompt: str | None = None,
    retry_missing: bool = True,
) -> dict:
    """翻译整篇（默认）或按 ``ids`` 重译合并。

    - 无 ``ids``：``document.md`` 一次性交给翻译命令，自动补译缺失段落。
    - 有 ``ids``：走 ``retranslate_blocks`` 补译/重译并合并进 ``translated.md``。
    - ``markdown``：直接导入已有译文，不调用任何命令。
    - ``prompt_only``：只写 ``agent/prompt.md``，不调用任何命令。

    翻译命令（``--translator`` / ``BDT_TRANSLATOR``）只走 stdin/stdout 协议，
    模型与档位由该命令自己决定。
    """
    workdir_path = common.require_workdir(workdir)
    resolved_timeout = int(timeout or 1800)
    if ids:
        return retranslate_ids(
            str(workdir_path),
            ids,
            feedback=feedback,
            translator=translator,
            timeout=resolved_timeout,
            repair_prompt=repair_prompt,
        )
    return _translate_whole_document(
        workdir_path,
        markdown=markdown,
        prompt_only=prompt_only,
        translator=translator,
        timeout=resolved_timeout,
        prompt=prompt,
        retry_missing=retry_missing,
    )


def _translate_whole_document(
    workdir: Path,
    *,
    markdown: str | None,
    prompt_only: bool,
    translator: str | None,
    timeout: int,
    prompt: str | None,
    retry_missing: bool,
) -> dict:
    from babeldoc.tools.agent import markdown_view

    agent = common.agent_dir(workdir)
    document_md = agent / "document.md"
    if not document_md.exists():
        raise common.ToolError("document_missing", f"{document_md} 不存在：请先 bdt parse")

    prompt_name = prompt or "translator"
    document = document_md.read_text(encoding="utf-8")
    prompt_text = common.load_prompt(prompt_name, document=document)
    prompt_file = agent / "prompt.md"
    prompt_file.write_text(prompt_text, encoding="utf-8")

    translated_path = agent / "translated.md"
    called_agent = False
    if markdown:
        imported_path = Path(markdown)
        if not imported_path.exists():
            raise common.ToolError("translated_md_missing", f"{imported_path} 不存在")
        translated_path.write_text(
            imported_path.read_text(encoding="utf-8"), encoding="utf-8"
        )
    elif prompt_only:
        return {
            "dry_run": True,
            "prompt_only": True,
            "prompt": str(prompt_file),
            "prompt_chars": len(prompt_text),
            "translated_md": None,
        }
    else:
        command = _resolve_translator(translator)
        response = common.run_translator(prompt_text, command, timeout_s=timeout)
        translated_path.write_text(response, encoding="utf-8")
        called_agent = True

    missing = markdown_view.missing_ids(
        workdir, translated_path.read_text(encoding="utf-8")
    )
    retried = None
    # 只有真的调用过翻译命令才自动补译：``--markdown`` 承诺"不调任何命令"，
    # 导入译文若本身缺段，交由调用方用 --ids（显式给 --translator）处理。
    if missing and retry_missing and called_agent:
        retried = retranslate_blocks(
            workdir, missing, feedback="", translator=translator,
            timeout=timeout, repair_prompt=None,
        )
        missing = retried["still_missing"]
    return {
        "prompt": str(prompt_file),
        "translated_md": str(translated_path),
        "chars": len(translated_path.read_text(encoding="utf-8")),
        "missing_before_retry": retried["requested_ids"] if retried else missing,
        "missing_after_retry": missing,
        "retry": retried,
    }


# --------------------------------------------------------------------------- #
# 按 id 重译
# --------------------------------------------------------------------------- #
def retranslate_blocks(
    workdir: Path,
    ids: list[str],
    feedback: str = "",
    *,
    translator: str | None = None,
    timeout: int | None = None,
    repair_prompt: str | None = None,
) -> dict:
    """补译/重译指定段落，合并进 translated.md。返回统计（不写 IR）。"""
    from babeldoc.tools.agent import markdown_view

    agent = common.agent_dir(workdir)
    ids = [pid for pid in ids if pid]
    if not ids:
        return {
            "requested_ids": [],
            "returned_ids": [],
            "still_missing": [],
            "translated_md": str(agent / "translated.md"),
        }
    retry_doc = markdown_view.render_retry_markdown(workdir, ids)
    if not retry_doc.strip():
        raise common.ToolError("ids_unknown", f"这些 id 不在解析产物中: {ids}")
    prompt_text = common.load_prompt(
        repair_prompt or "translator-repair",
        document=retry_doc,
        feedback=feedback or "（无额外反馈）",
    )
    prompt_file = agent / "prompt.retry.md"
    prompt_file.write_text(prompt_text, encoding="utf-8")

    command = _resolve_translator(translator)
    response = common.run_translator(
        prompt_text, command, timeout_s=int(timeout or 1800)
    )
    (agent / "translated.retry.md").write_text(response, encoding="utf-8")

    blocks = markdown_view.parse_translated_markdown(response)
    merged_path = merge_translated_markdown(
        workdir, {pid: blocks[pid] for pid in ids if pid in blocks}
    )
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
    }


def retranslate_ids(
    workdir: str,
    ids: list[str],
    *,
    feedback: str | None = None,
    translator: str | None = None,
    timeout: int | None = None,
    repair_prompt: str | None = None,
) -> dict:
    """按 id 补译/重译（可带 feedback），合并回 translated.md。

    ``feedback`` 只进入重译提示词，不改变 Agent 调用协议。
    """
    workdir_path = common.require_workdir(workdir)
    result = retranslate_blocks(
        workdir_path,
        list(ids),
        feedback or "",
        translator=translator,
        timeout=timeout,
        repair_prompt=repair_prompt,
    )
    return result


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


# --------------------------------------------------------------------------- #
# 写回 IR
# --------------------------------------------------------------------------- #
def apply_translation(workdir: str, *, markdown: str | None = None) -> dict:
    """校验译文 Markdown（id 对齐 + 锚点协议）并写回 IR。

    ``markdown`` 缺省为 ``agent/translated.md``；也接受 ``agent/document.md``
    （自译自校验仅用于验证链路）。
    """
    from babeldoc.tools.agent import markdown_view

    workdir_path = common.require_workdir(workdir)
    md_path = Path(markdown) if markdown else common.agent_dir(workdir_path) / "translated.md"
    if not md_path.exists():
        raise common.ToolError(
            "translated_md_missing", f"{md_path} 不存在：请先 bdt translate"
        )
    report = markdown_view.apply_markdown(workdir_path, md_path)
    report["translated_md"] = str(md_path)
    common.write_json(common.agent_dir(workdir_path) / "apply_report.json", report)
    return report
