"""解析工具：PDF → 连续 Markdown（带锚点）+ IR 状态。"""

from __future__ import annotations

from pathlib import Path

from babeldoc_tools import common

LAYOUTS = ("mineru", "paddle")


def parse_document(
    pdf: str,
    workdir: str,
    *,
    layout: str = "mineru",
    pages: str | None = None,
    lang_in: str = "en",
    lang_out: str = "zh",
    mineru_token: str | None = None,
    mineru_json: str | None = None,
    mineru_cache_key: str | None = None,
    layout_coverage_threshold: float = 0.005,
    mineru_use_ocr_text: bool = False,
) -> dict:
    """解析 PDF：布局后端 → 段落 → 连续英文 Markdown（带行内锚点）。

    写 ``<workdir>/agent/{document.md,anchors.json,sheet.jsonl,state.pkl}``。
    ``layout=paddle`` 直接透传给 ``markdown_view.extract_markdown``。
    """
    from babeldoc.tools.agent import markdown_view

    pdf_path = Path(pdf)
    if not pdf_path.exists():
        raise common.ToolError("pdf_missing", f"PDF 不存在: {pdf_path}")
    workdir_path = Path(workdir)
    layout = layout or "mineru"
    if layout not in LAYOUTS:
        raise common.ToolError(
            "layout_unsupported", f"layout 必须是 {list(LAYOUTS)} 之一，得到 {layout!r}"
        )
    token = mineru_token or common.env_default("MINERU_API_TOKEN")
    if layout == "mineru" and not (token or mineru_json or mineru_cache_key):
        raise common.ToolError(
            "mineru_token_missing",
            "mineru 布局需要 mineru_token 或环境变量 MINERU_API_TOKEN"
            "（或用 mineru_json / mineru_cache_key 回放缓存布局）",
        )
    result = markdown_view.extract_markdown(
        pdf_path,
        workdir_path,
        lang_in=lang_in or "en",
        lang_out=lang_out or "zh",
        layout=layout,
        mineru_token=token,
        mineru_json=mineru_json,
        mineru_cache_key=mineru_cache_key,
        pages=pages,
        layout_coverage_threshold=layout_coverage_threshold,
        mineru_use_ocr_text=mineru_use_ocr_text,
    )
    result["workdir"] = str(workdir_path)
    result["layout"] = layout
    return result
