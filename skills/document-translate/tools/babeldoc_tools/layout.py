"""排版工具：重建 PDF / 渲染页 / 覆盖微调 / lint / 定位。"""

from __future__ import annotations

from pathlib import Path

from babeldoc_tools import common
from babeldoc_tools.registry import register

SEV_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


@register(
    "reconstruct_pdf",
    group="layout",
    description=(
        "从 IR 重排生成 mono/dual PDF，应用 agent/layout_overrides.json 并 dump "
        "agent/layout_geometry.json（排版微调的唯一入口）"
    ),
    output_hint="mono_pdf / dual_pdf / layout_geometry / layout_warnings / page 统计",
    input_schema={
        "type": "object",
        "properties": {
            "workdir": {"type": "string", "minLength": 1},
            "output_dir": {"type": "string"},
            "dual": {"type": "boolean", "description": "同时输出拼宽双语 PDF（默认 true）"},
            "watermark": {"type": "boolean"},
            "latex_bbox": {
                "type": "boolean",
                "description": "开启 LaTeX bbox 排版（实验特性；缺 XeLaTeX/字体时自动回退）",
            },
            "latex_bbox_mode": {
                "type": "string",
                "enum": ["full", "repair"],
                "description": "LaTeX bbox 资格模式：full（默认）/ repair（复现旧行为）",
            },
            "stats": {"type": "boolean", "description": "附带 PDF 页数/目录/链接统计"},
        },
        "required": ["workdir"],
    },
)
def reconstruct_pdf(args: dict) -> dict:
    from babeldoc.tools.agent import workflow

    workdir = common.require_workdir(args["workdir"])
    output_dir = args.get("output_dir") or str(workdir / "output")
    dual = args.get("dual")
    result = workflow.reconstruct(
        str(workdir),
        output_dir=output_dir,
        no_dual=not (True if dual is None else dual),
        watermark=bool(args.get("watermark")),
        latex_bbox=bool(args.get("latex_bbox")),
        latex_bbox_mode=args.get("latex_bbox_mode"),
    )
    if args.get("stats", True):
        result["stats"] = {
            kind: _pdf_stats(path)
            for kind, path in (("mono", result.get("mono_pdf")), ("dual", result.get("dual_pdf")))
            if path
        }
    common.write_json(common.agent_dir(workdir) / "reconstruct_report.json", result)
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


@register(
    "render_pages",
    group="layout",
    description="把 PDF 指定页渲染成 PNG（视觉审查用）",
    output_hint="images[]",
    input_schema={
        "type": "object",
        "properties": {
            "pdf": {"type": "string", "minLength": 1},
            "pages": {"type": "string", "description": "如 1,2 或 1-3"},
            "dpi": {"type": "integer", "minimum": 36, "maximum": 400},
            "out_dir": {"type": "string"},
        },
        "required": ["pdf", "pages"],
    },
)
def render_pages(args: dict) -> dict:
    from babeldoc.tools.agent import workflow

    pdf = Path(args["pdf"])
    if not pdf.exists():
        raise common.ToolError("pdf_missing", f"PDF 不存在: {pdf}")
    return workflow.render(
        str(pdf),
        args["pages"],
        dpi=int(args.get("dpi") or 110),
        out_dir=args.get("out_dir"),
    )


@register(
    "dump_text_layer",
    group="review",
    description=(
        "导出 PDF 文本层为逐页 txt（含 span 字号），供审查 agent grep 复核："
        "视觉发现的结论必须回到文本层确认"
    ),
    output_hint="pages[] / files[] / dir",
    input_schema={
        "type": "object",
        "properties": {
            "pdf": {"type": "string", "minLength": 1},
            "pages": {"type": "string", "description": "如 1,2 或 1-3；缺省 = 全部"},
            "out_dir": {"type": "string"},
            "with_spans": {"type": "boolean", "description": "附带字号/坐标 span 清单"},
        },
        "required": ["pdf"],
    },
)
def dump_text_layer(args: dict) -> dict:
    import unicodedata

    import pymupdf

    pdf = Path(args["pdf"])
    if not pdf.exists():
        raise common.ToolError("pdf_missing", f"PDF 不存在: {pdf}")
    out_dir = Path(args.get("out_dir") or (pdf.parent / "text_layer"))
    out_dir.mkdir(parents=True, exist_ok=True)
    selected = _parse_pages(args.get("pages"))
    doc = pymupdf.open(pdf)
    files = []
    for page_number in range(1, len(doc) + 1):
        if selected and page_number not in selected:
            continue
        page = doc[page_number - 1]
        body = page.get_text()
        compat = sorted({ch for ch in body if 0xF900 <= ord(ch) <= 0xFAFF})
        header = (
            f"# text layer: {pdf.name} page {page_number}\n"
            f"# 兼容表意文字 {len(compat)} 种: "
            + ", ".join(f"{ch}->{unicodedata.normalize('NFKC', ch)}" for ch in compat)
            + "\n"
        )
        text_path = out_dir / f"page-{page_number:02d}.txt"
        text_path.write_text(header + body, encoding="utf-8")
        files.append(str(text_path))
        if args.get("with_spans"):
            spans = []
            for block in page.get_text("dict")["blocks"]:
                for line in block.get("lines", []):
                    for span in line["spans"]:
                        if span["text"].strip():
                            spans.append(
                                f"[{span['size']:.1f}pt @({span['bbox'][0]:.0f},"
                                f"{span['bbox'][1]:.0f})] {span['text']}"
                            )
            (out_dir / f"page-{page_number:02d}.spans.txt").write_text(
                "\n".join(spans), encoding="utf-8"
            )
    doc.close()
    return {"dir": str(out_dir), "files": files, "pages": len(files)}


def _parse_pages(pages: str | None) -> list[int] | None:
    if not pages:
        return None
    from babeldoc.tools.agent.workflow import _parse_pages as parse

    return parse(pages)


@register(
    "layout_set",
    group="layout",
    description=(
        "写入/合并段落级排版覆盖（scale_cap / font_scale / line_skip / box_scale / box / "
        "force_break_after_text / force_break_after_offset，页级 font_scale）；"
        "不重建 PDF，需随后调 reconstruct_pdf"
    ),
    output_hint="overrides / diff / path",
    input_schema={
        "type": "object",
        "properties": {
            "workdir": {"type": "string", "minLength": 1},
            "patch": {
                "type": "object",
                "description": "形如 {paragraphs: {P05-012: {...}}, pages: {'5': {...}}}",
                "properties": {
                    "paragraphs": {"type": "object"},
                    "pages": {"type": "object"},
                },
            },
            "reason": {"type": "string", "description": "改动理由（写入 history）"},
            "clear": {"type": "boolean", "description": "清空全部覆盖（回滚到无覆盖）"},
        },
        "required": ["workdir"],
    },
)
def layout_set(args: dict) -> dict:
    from babeldoc.tools.agent import layout_overrides

    workdir = common.require_workdir(args["workdir"])
    if args.get("clear"):
        return layout_overrides.clear_overrides(workdir, reason=args.get("reason"))
    patch = args.get("patch")
    if not patch:
        raise common.ToolError("patch_missing", "需要 patch（或 clear=true）")
    result = layout_overrides.apply_patch(workdir, patch, reason=args.get("reason"))
    if not result.get("ok"):
        raise common.ToolError(
            "invalid_patch", "; ".join(result.get("errors") or []), errors=result.get("errors")
        )
    return result


@register(
    "layout_lint",
    group="layout",
    description=(
        "排版缺陷 lint：越界/段落重叠/字号塌缩/压图 + 文本层兼容字/链接错位（P2）；"
        "需要先 reconstruct_pdf 生成 layout_geometry.json"
    ),
    output_hint="findings / counts / summary / metrics",
    input_schema={
        "type": "object",
        "properties": {
            "workdir": {"type": "string", "minLength": 1},
            "pdf": {"type": "string", "description": "输出 PDF（默认自动找 mono）"},
            "page": {"type": "integer", "minimum": 1, "description": "只看某一页"},
            "code": {"type": "string", "description": "只看某类 finding"},
            "min_sev": {"type": "string", "enum": ["P0", "P1", "P2"], "description": "最低严重度"},
        },
        "required": ["workdir"],
    },
)
def layout_lint(args: dict) -> dict:
    from babeldoc.tools.agent import layout_geometry

    workdir = common.require_workdir(args["workdir"])
    geometry = layout_geometry.load_geometry(workdir)
    pdf = args.get("pdf") or _find_output(workdir, "mono")
    result = layout_geometry.lint_geometry(geometry, pdf)
    findings = result["findings"]
    # Typesetting 阶段记下的覆盖告警（如强制换行锚点没命中）
    for warning in geometry.get("warnings") or []:
        findings.append(
            {"code": "force_break_unresolved", "sev": "P2", "page": None, "evidence": {"warning": warning}}
        )
    if args.get("page"):
        findings = [f for f in findings if f.get("page") == args["page"]]
    if args.get("code"):
        findings = [f for f in findings if f["code"] == args["code"]]
    if args.get("min_sev"):
        limit = SEV_ORDER[args["min_sev"]]
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
        "pdf": pdf,
        "overrides": geometry.get("overrides") or {},
        "ir_overrides": geometry.get("ir_overrides") or {},
    }
    _append_lint_history(workdir, result)
    common.write_json(common.agent_dir(workdir) / "layout_lint.json", result)
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


@register(
    "layout_locate",
    group="layout",
    description="按页 + box（IoU）或 text（子串）定位段落 id，用于把视觉问题映射回可覆盖的段落",
    output_hint="candidates[]（id / score / rendered_box / overridable / text）",
    input_schema={
        "type": "object",
        "properties": {
            "workdir": {"type": "string", "minLength": 1},
            "page": {"type": "integer", "minimum": 1},
            "box": {
                "type": "array",
                "items": {"type": "number"},
                "description": "[x, y, x2, y2]（PDF 坐标，y 向上）",
                "minItems": 4,
            },
            "text": {"type": "string", "description": "段落文本片段（前 60 字内）"},
            "limit": {"type": "integer", "minimum": 1, "maximum": 50},
        },
        "required": ["workdir"],
    },
)
def layout_locate(args: dict) -> dict:
    from babeldoc.tools.agent import layout_geometry

    workdir = common.require_workdir(args["workdir"])
    geometry = layout_geometry.load_geometry(workdir)
    if not geometry:
        raise common.ToolError(
            "geometry_missing", "layout_geometry.json 不存在：请先 reconstruct_pdf"
        )
    box = args.get("box")
    if box is not None and len(box) != 4:
        raise common.ToolError("invalid_box", "box 必须是 [x, y, x2, y2]")
    candidates = layout_geometry.locate(
        geometry,
        page=args.get("page"),
        box=box,
        text=args.get("text"),
        limit=int(args.get("limit") or 8),
    )
    return {
        "candidates": candidates,
        "geometry": str(layout_geometry.geometry_path(workdir)),
        "overrides": geometry.get("overrides") or {},
    }


def _find_output(workdir: Path, suffix: str) -> str | None:
    for base in (workdir / "output", workdir):
        if not base.is_dir():
            continue
        for path in sorted(base.glob(f"*.{suffix}.pdf")):
            return str(path)
    return None
