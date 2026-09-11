"""解析工具：PDF → 连续 Markdown（带锚点）+ IR 状态。"""

from __future__ import annotations

from pathlib import Path

from babeldoc_tools import common
from babeldoc_tools.registry import register

PDF_SCHEMA = {"type": "string", "minLength": 1}


@register(
    "parse_document",
    group="parse",
    description=(
        "解析 PDF：MinerU/native 布局 → 段落 → 连续英文 Markdown（带行内锚点），"
        "写 <workdir>/agent/{document.md,anchors.json,sheet.jsonl,state.pkl}"
    ),
    output_hint="paragraphs / label_counts / skipped_label_counts / document_md / sheet",
    input_schema={
        "type": "object",
        "properties": {
            "pdf": PDF_SCHEMA,
            "workdir": {"type": "string", "minLength": 1},
            "layout": {"type": "string", "enum": ["mineru", "native"]},
            "mineru_token": {"type": "string"},
            "mineru_json": {"type": "string", "description": "回放缓存的 MinerU layout.json"},
            "pages": {"type": "string", "description": "如 1,2 或 1-3"},
            "lang_in": {"type": "string"},
            "lang_out": {"type": "string"},
        },
        "required": ["pdf", "workdir"],
    },
)
def parse_document(args: dict) -> dict:
    from babeldoc.tools.agent import markdown_view

    pdf = Path(args["pdf"])
    if not pdf.exists():
        raise common.ToolError("pdf_missing", f"PDF 不存在: {pdf}")
    workdir = Path(args["workdir"])
    layout = args.get("layout") or "mineru"
    token = args.get("mineru_token") or common.env_default("MINERU_API_TOKEN")
    if layout == "mineru" and not token and not args.get("mineru_json"):
        raise common.ToolError(
            "mineru_token_missing",
            "mineru 布局需要 mineru_token 或环境变量 MINERU_API_TOKEN"
            "（或用 layout=native / mineru_json 回放）",
        )
    result = markdown_view.extract_markdown(
        pdf,
        workdir,
        lang_in=args.get("lang_in") or "en",
        lang_out=args.get("lang_out") or "zh",
        layout=layout,
        mineru_token=token,
        mineru_json=args.get("mineru_json"),
        pages=args.get("pages"),
    )
    result["workdir"] = str(workdir)
    result["layout"] = layout
    return result
