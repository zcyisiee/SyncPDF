"""排版工具：重建 PDF / 渲染页 / 覆盖微调 / lint / 定位。

公开 CLI 只暴露 ``build`` / ``layout-set``；``layout_lint`` 仍被 review 链路调用；
``layout_locate`` / ``dump_text_layer`` 已从公开 CLI 移除，但保留内部 Python 函数
（供 reviewer agent 与后续阶段使用）。
"""

from __future__ import annotations

from pathlib import Path

from babeldoc_tools import common
from babeldoc_tools import debug_runtime
from babeldoc_tools import target_layout as target_layout_tool

SEV_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def build_pdf(
    workdir: str,
    *,
    output_dir: str | None = None,
    dual: bool = False,
    watermark: bool = False,
    latex_bbox: bool = True,
    latex_bbox_mode: str | None = None,
    latex_refine: bool = True,
    target_layout: bool | None = None,
    render: str | None = None,
    stats: bool = True,
    debug_recorder=None,
    debug_recompile: bool = False,
) -> dict:
    """从 IR 重排生成 mono/dual PDF（应用 ``agent/layout_overrides.json``）。

    可选 ``latex_refine``：首遍产物里确实有段落被缩字时，用本地 PP-DocLayoutV3
    识别译文版面，把这些段的 LaTeX 贴片矩形向下扩到相邻墨迹之间，再重排一遍
    （只改贴片矩形，不写覆盖文件）。可选 ``target_layout``：build 成功后再对译文
    mono PDF 跑一次 MinerU，把**译文侧** provider IR 落到 ``agent/target/provider/``
    （前端「译文框」的数据源；默认在布局后端为 mineru 且 token 可用时执行，识别
    失败只记清单，不阻断 build）。可选 ``render``
    （``"1,2"`` / ``"1-3"``）在重建后把指定页渲染成 PNG。
    返回 JSON 含 ``mono_pdf`` / ``dual_pdf`` / ``layout_geometry`` / ``target_layout`` / ``images``。
    """
    workdir_path = common.require_workdir(workdir)
    resolved_output_dir = output_dir or str(workdir_path / "output")
    with debug_runtime.debug_stage(
        debug_recorder,
        "build",
        {
            "latex_bbox": bool(latex_bbox),
            "latex_bbox_mode": latex_bbox_mode,
            "latex_refine": bool(latex_refine),
            "target_layout": target_layout,
            "dual": bool(dual),
            "watermark": bool(watermark),
            "debug_recompile": bool(debug_recompile),
        },
    ):
        try:
            result = reconstruct_pdf(
                str(workdir_path),
                output_dir=resolved_output_dir,
                dual=dual,
                watermark=watermark,
                latex_bbox=latex_bbox,
                latex_bbox_mode=latex_bbox_mode,
                latex_refine=latex_refine,
                stats=stats,
                debug_recorder=debug_recorder,
                debug_recompile=debug_recompile,
            )
        except Exception as exc:
            if debug_recorder is not None:
                # 失败现场：只归档本次构建新落盘的 agent 文件与输出目录里新生成的
                # 部分 PDF。上一轮残留（``mtime`` 早于本次 run 起始）不归档，
                # 否则会被误报成本次输出。``reconstruct_report.json`` 只在成功
                # 路径写出，失败时残留的是旧报告 → 同样不归档。
                from babeldoc.tools.agent import debug_capture

                since = getattr(debug_recorder, "started_at_ts", None)
                debug_capture.capture_files(
                    debug_recorder,
                    "build",
                    workdir_path,
                    ["layout_geometry.json", "latex_bbox_report.json"],
                    phase="partial_outputs",
                    since=since,
                )
                partial_pdfs = {}
                for pdf_path in sorted(Path(resolved_output_dir).glob("*.pdf")):
                    if since is not None and pdf_path.stat().st_mtime < since:
                        continue
                    artifact = debug_recorder.archive_file(
                        "build", f"partial/{pdf_path.name}", pdf_path
                    )
                    if artifact:
                        partial_pdfs[pdf_path.name] = artifact
                if partial_pdfs:
                    debug_recorder.record_event(
                        "build",
                        "artifact_bundle",
                        {"phase": "partial_output_pdfs", "artifacts": partial_pdfs},
                    )
                debug_recorder.record_event(
                    "build",
                    "build_failed",
                    {"error": f"{type(exc).__name__}: {exc}"},
                )
            raise
        result.setdefault("images", [])
        result["target_layout"] = target_layout_tool.recognize_target_layout(
            workdir_path, result, enabled=target_layout
        )
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
            # 译文侧识别是 build 的附加产物：清单与 IR 一起归档，失败现场（只有
            # 清单里的 reason）在 debug run 里也看得到。
            recount = result.get("target_layout") or {}
            for name, artifact in (
                (
                    "target_recognition.json",
                    target_layout_tool.manifest_path(workdir_path),
                ),
                (
                    "target/provider/provider_ir.json",
                    target_layout_tool.provider_ir_path(workdir_path),
                ),
            ):
                if artifact.exists():
                    debug_recorder.archive_file("build", name, artifact)
            debug_recorder.record_event(
                "build",
                "target_layout",
                {
                    "status": recount.get("status"),
                    "reason": recount.get("reason"),
                    "provider": recount.get("provider"),
                    "page_count": recount.get("page_count"),
                    "pdf": recount.get("pdf"),
                },
            )
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
    latex_refine: bool = True,
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
    refined = _refine_tight_boxes(
        result, enabled=bool(latex_bbox) and bool(latex_refine)
    )
    if refined is not None and refined.overrides:
        result = workflow.reconstruct(
            str(workdir_path),
            output_dir=resolved_output_dir,
            no_dual=not dual,
            watermark=bool(watermark),
            latex_bbox=True,
            latex_bbox_mode=latex_bbox_mode,
            debug_recorder=debug_recorder,
            debug_recompile=debug_recompile,
            latex_box_overrides=refined.overrides,
        )
        result["latex_refine"] = {
            "targets": refined.targets,
            "expanded": len(refined.overrides),
            "skipped": dict(refined.skipped),
            "boxes": refined.overrides,
        }
    if refined is not None:
        result.setdefault("latex_refine", {
            "targets": refined.targets,
            "expanded": 0,
            "skipped": dict(refined.skipped),
        })
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


def _refine_tight_boxes(result: dict, *, enabled: bool):
    """首遍产物里有缩字段时，按译文版面算一遍更大贴片框（P6）。

    只读首遍的 ``latex_bbox_report.json`` / ``layout_geometry.json`` 与 mono
    PDF；检测器不可用或无可扩段时返回 None，调用方保持单遍行为。
    """
    if not enabled:
        return None
    from babeldoc.docvision.paddle_layout_regions import PaddleLayoutRegions
    from babeldoc.tools.agent import layout_refine

    if not layout_refine.refine_enabled():
        return None
    pdf_path = result.get("mono_pdf") or result.get("dual_pdf")
    report_path = result.get("latex_bbox_report")
    if not pdf_path or not report_path:
        return None
    report = common.read_json(report_path, default=None)
    if not isinstance(report, dict) or report.get("mode") == "repair":
        return None
    geometry = common.read_json(result.get("layout_geometry") or "", default=None)
    detector = PaddleLayoutRegions()
    if not detector.available:
        return None
    try:
        return layout_refine.plan_build_refinement(
            report=report,
            geometry=geometry,
            pdf_path=pdf_path,
            detector=detector,
        )
    except Exception:  # noqa: BLE001 - 扩框失败不阻断构建
        import logging

        logging.getLogger(__name__).warning("编译后扩框失败，保留首遍版式", exc_info=True)
        return None


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
