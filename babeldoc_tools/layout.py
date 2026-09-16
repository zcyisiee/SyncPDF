"""排版工具：重建 PDF / 渲染页 / 覆盖微调 / lint / 定位。

公开 CLI 只暴露 ``build`` / ``layout-set``；``layout_lint`` 仍被 review 链路调用；
``layout_locate`` / ``dump_text_layer`` 已从公开 CLI 移除，但保留内部 Python 函数
（供 reviewer agent 与后续阶段使用）。
"""

from __future__ import annotations

from pathlib import Path

from babeldoc_tools import common
from babeldoc_tools import debug_runtime

SEV_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def build_pdf(
    workdir: str,
    *,
    output_dir: str | None = None,
    dual: bool = False,
    watermark: bool = False,
    latex_bbox: bool = True,
    latex_bbox_mode: str | None = None,
    render: str | None = None,
    stats: bool = True,
    debug_recorder=None,
    debug_recompile: bool = False,
) -> dict:
    """从 IR 重排生成 mono/dual PDF（应用 ``agent/layout_overrides.json``）。

    可选 ``render``（``"1,2"`` / ``"1-3"``）在重建后把指定页渲染成 PNG。
    返回 JSON 含 ``mono_pdf`` / ``dual_pdf`` / ``layout_geometry`` / ``images``。
    """
    workdir_path = common.require_workdir(workdir)
    resolved_output_dir = output_dir or str(workdir_path / "output")
    with debug_runtime.debug_stage(
        debug_recorder,
        "build",
        {
            "latex_bbox": bool(latex_bbox),
            "latex_bbox_mode": latex_bbox_mode,
            "dual": bool(dual),
            "watermark": bool(watermark),
            "debug_recompile": bool(debug_recompile),
        },
    ):
        result = reconstruct_pdf(
            str(workdir_path),
            output_dir=resolved_output_dir,
            dual=dual,
            watermark=watermark,
            latex_bbox=latex_bbox,
            latex_bbox_mode=latex_bbox_mode,
            stats=stats,
            debug_recorder=debug_recorder,
            debug_recompile=debug_recompile,
        )
        result.setdefault("images", [])
        if render:
            rendered = render_pages(
                result.get("mono_pdf") or result.get("dual_pdf"),
                render,
                out_dir=str(Path(resolved_output_dir) / "render"),
            )
            result["images"] = rendered.get("images") or []
        if debug_recorder is not None:
            agent = common.agent_dir(workdir_path)
            for key, name in (
                ("mono_pdf", "mono.pdf"),
                ("dual_pdf", "dual.pdf"),
            ):
                if result.get(key):
                    debug_recorder.archive_file("build", name, result[key])
            for name in (
                "layout_geometry.json",
                "latex_bbox_report.json",
                "reconstruct_report.json",
            ):
                artifact = agent / name
                if artifact.exists():
                    debug_recorder.archive_file("build", name, artifact)
            stats_map = result.get("stats") or {}
            debug_recorder.record_event(
                "build",
                "stage_finished",
                {
                    "mono_pdf": result.get("mono_pdf"),
                    "dual_pdf": result.get("dual_pdf"),
                    "pages": (stats_map.get("mono") or {}).get("pages"),
                },
            )
        return result


def reconstruct_pdf(
    workdir: str,
    *,
    output_dir: str | None = None,
    dual: bool = False,
    watermark: bool = False,
    latex_bbox: bool = True,
    latex_bbox_mode: str | None = None,
    stats: bool = True,
    debug_recorder=None,
    debug_recompile: bool = False,
) -> dict:
    from babeldoc.tools.agent import workflow

    workdir_path = common.require_workdir(workdir)
    resolved_output_dir = output_dir or str(workdir_path / "output")
    result = workflow.reconstruct(
        str(workdir_path),
        output_dir=resolved_output_dir,
        no_dual=not dual,
        watermark=bool(watermark),
        latex_bbox=bool(latex_bbox),
        latex_bbox_mode=latex_bbox_mode,
        debug_recorder=debug_recorder,
        debug_recompile=debug_recompile,
    )
    if stats:
        result["stats"] = {
            kind: _pdf_stats(path)
            for kind, path in (
                ("mono", result.get("mono_pdf")),
                ("dual", result.get("dual_pdf")),
            )
            if path
        }
    common.write_json(common.agent_dir(workdir_path) / "reconstruct_report.json", result)
    return result


def _pdf_stats(pdf_path) -> dict:
    import pymupdf

    doc = pymupdf.open(pdf_path)
    stats = {
        "pages": len(doc),
        "toc_entries": len(doc.get_toc()),
        "links": sum(len(page.get_links()) for page in doc),
    }
    doc.close()
    return stats


def render_pages(pdf: str, pages: str, *, dpi: int = 110, out_dir: str | None = None) -> dict:
    """把 PDF 指定页渲染成 PNG（视觉审查用）。"""
    from babeldoc.tools.agent import workflow

    pdf_path = Path(pdf)
    if not pdf_path.exists():
        raise common.ToolError("pdf_missing", f"PDF 不存在: {pdf_path}")
    return workflow.render(
        str(pdf_path),
        pages,
        dpi=int(dpi or 110),
        out_dir=out_dir,
    )


def dump_text_layer(
    pdf: str,
    *,
    pages: str | None = None,
    out_dir: str | None = None,
    with_spans: bool = False,
) -> dict:
    """导出 PDF 文本层为逐页 txt（含 span 字号），供审查 agent grep 复核。

    已从公开 CLI 移除；保留为内部 Python 函数。
    """
    import unicodedata

    import pymupdf

    pdf_path = Path(pdf)
    if not pdf_path.exists():
        raise common.ToolError("pdf_missing", f"PDF 不存在: {pdf_path}")
    resolved_out_dir = Path(out_dir) if out_dir else (pdf_path.parent / "text_layer")
    resolved_out_dir.mkdir(parents=True, exist_ok=True)
    selected = _parse_pages(pages)
    doc = pymupdf.open(pdf_path)
    files = []
    for page_number in range(1, len(doc) + 1):
        if selected and page_number not in selected:
            continue
        page = doc[page_number - 1]
        body = page.get_text()
        compat = sorted({ch for ch in body if 0xF900 <= ord(ch) <= 0xFAFF})
        header = (
            f"# text layer: {pdf_path.name} page {page_number}\n"
            f"# 兼容表意文字 {len(compat)} 种: "
            + ", ".join(f"{ch}->{unicodedata.normalize('NFKC', ch)}" for ch in compat)
            + "\n"
        )
        text_path = resolved_out_dir / f"page-{page_number:02d}.txt"
        text_path.write_text(header + body, encoding="utf-8")
        files.append(str(text_path))
        if with_spans:
            spans = []
            for block in page.get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    for span in line["spans"]:
                        if span["text"].strip():
                            spans.append(
                                f"[{span['size']:.1f}pt @({span['bbox'][0]:.0f},"
                                f"{span['bbox'][1]:.0f})] {span['text']}"
                            )
            (resolved_out_dir / f"page-{page_number:02d}.spans.txt").write_text(
                "\n".join(spans), encoding="utf-8"
            )
    doc.close()
    return {"dir": str(resolved_out_dir), "files": files, "pages": len(files)}


def _parse_pages(pages: str | None) -> list[int] | None:
    if not pages:
        return None
    from babeldoc.tools.agent.workflow import _parse_pages as parse

    return parse(pages)


def layout_set(
    workdir: str,
    *,
    patch: dict | None = None,
    reason: str | None = None,
    clear: bool = False,
) -> dict:
    """写入/合并段落级排版覆盖；不重建 PDF，需随后调 ``bdt build``。"""
    from babeldoc.tools.agent import layout_overrides

    workdir_path = common.require_workdir(workdir)
    if clear:
        return layout_overrides.clear_overrides(workdir_path, reason=reason)
    if not patch:
        raise common.ToolError("patch_missing", "需要 patch（或 clear=true）")
    result = layout_overrides.apply_patch(workdir_path, patch, reason=reason)
    if not result.get("ok"):
        raise common.ToolError(
            "invalid_patch", "; ".join(result.get("errors") or []), errors=result.get("errors")
        )
    return result


def layout_lint(
    workdir: str,
    *,
    pdf: str | None = None,
    page: int | None = None,
    code: str | None = None,
    min_sev: str | None = None,
) -> dict:
    """排版缺陷 lint：越界/段落重叠/字号塌缩/压图 + 文本层兼容字/链接错位（P2）。

    需要先 ``bdt build`` 生成 ``layout_geometry.json``。
    """
    from babeldoc.tools.agent import layout_geometry

    workdir_path = common.require_workdir(workdir)
    geometry = layout_geometry.load_geometry(workdir_path)
    resolved_pdf = pdf or _find_output(workdir_path, "mono")
    result = layout_geometry.lint_geometry(geometry, resolved_pdf)
    findings = result["findings"]
    # Typesetting 阶段记下的覆盖告警（如强制换行锚点没命中）
    for warning in geometry.get("warnings") or []:
        findings.append(
            {"code": "force_break_unresolved", "sev": "P2", "page": None, "evidence": {"warning": warning}}
        )
    if page:
        findings = [f for f in findings if f.get("page") == page]
    if code:
        findings = [f for f in findings if f["code"] == code]
    if min_sev:
        limit = SEV_ORDER[min_sev]
        findings = [f for f in findings if SEV_ORDER.get(f.get("sev", "P2"), 9) <= limit]
    result["findings"] = sorted(
        findings, key=lambda f: (SEV_ORDER.get(f.get("sev", "P2"), 9), f.get("page") or 0)
    )
    counts: dict[str, int] = {}
    severity: dict[str, int] = {}
    for finding in result["findings"]:
        counts[finding["code"]] = counts.get(finding["code"], 0) + 1
        severity[finding.get("sev", "P2")] = severity.get(finding.get("sev", "P2"), 0) + 1
    result["counts"] = counts
    result["summary"] = {
        "total": len(result["findings"]),
        "by_severity": severity,
        "blocking": severity.get("P0", 0),
        "defects": severity.get("P0", 0) + severity.get("P1", 0),
        "info": severity.get("P2", 0) + severity.get("P3", 0),
        "pdf": resolved_pdf,
        "overrides": geometry.get("overrides") or {},
        "ir_overrides": geometry.get("ir_overrides") or {},
    }
    _append_lint_history(workdir_path, result)
    common.write_json(common.agent_dir(workdir_path) / "layout_lint.json", result)
    return result


def _append_lint_history(workdir: Path, result: dict) -> None:
    path = common.agent_dir(workdir) / "lint_history.json"
    history = common.read_json(path, default=[]) or []
    import datetime

    history.append(
        {
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "summary": result.get("summary"),
            "counts": result.get("counts"),
        }
    )
    common.write_json(path, history[-50:])


def layout_locate(
    workdir: str,
    *,
    page: int | None = None,
    box: list[float] | None = None,
    text: str | None = None,
    limit: int = 8,
) -> dict:
    """按页 + box（IoU）或 text（子串）定位段落 id。

    已从公开 CLI 移除；保留为内部 Python 函数，供 reviewer agent 把视觉问题
    映射回可覆盖的段落。
    """
    from babeldoc.tools.agent import layout_geometry

    workdir_path = common.require_workdir(workdir)
    geometry = layout_geometry.load_geometry(workdir_path)
    if not geometry:
        raise common.ToolError(
            "geometry_missing", "layout_geometry.json 不存在：请先 bdt build"
        )
    if box is not None and len(box) != 4:
        raise common.ToolError("invalid_box", "box 必须是 [x, y, x2, y2]")
    candidates = layout_geometry.locate(
        geometry,
        page=page,
        box=box,
        text=text,
        limit=int(limit or 8),
    )
    return {
        "candidates": candidates,
        "geometry": str(layout_geometry.geometry_path(workdir_path)),
        "overrides": geometry.get("overrides") or {},
    }


def _find_output(workdir: Path, suffix: str) -> str | None:
    for base in (workdir / "output", workdir):
        if not base.is_dir():
            continue
        for path in sorted(base.glob(f"*.{suffix}.pdf")):
            return str(path)
    return None
