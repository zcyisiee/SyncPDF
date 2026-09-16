"""解析阶段证据采集：IL → 调试快照/事件（仅 recorder 开启时执行）。

坐标约定：IL 内所有 box 是 PDF point、**左下原点（y 向上）**；快照统一转成
``debug_recorder.model.PDF_TOPLEFT``（左上原点，y 向下）。页高取该页 cropbox
（``_prepare_pdf`` 的 ``fix_media_box`` 已把 cropbox 归一到 mediabox 并清零
原点）。裁页（``--pages``）时 ``original_pages`` 提供显示页 → 源 PDF 物理页号
映射。

本模块只做「读 IL / 读已落盘审计文件 → 写快照与事件」，不改变任何解析行为。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from babeldoc.debug_recorder import model as dm

logger = logging.getLogger(__name__)

STAGE = "parse"


# --------------------------------------------------------------------------- #
# 坐标与 IL 访问
# --------------------------------------------------------------------------- #
def _page_height(page) -> float:
    """IL 页高（点）：cropbox 优先，mediabox 兜底；取不到返回 0。"""
    for attr in ("cropbox", "mediabox"):
        box = getattr(getattr(page, attr, None), "box", None)
        if box is None:
            continue
        y = getattr(box, "y", None)
        y2 = getattr(box, "y2", None)
        if y is None or y2 is None:
            continue
        try:
            height = float(y2) - float(y)
        except (TypeError, ValueError):
            continue
        if height > 0:
            return height
    return 0.0


def _il_box_to_topleft(box, page_height: float) -> dm.Box | None:
    """IL Box（左下原点）→ ``dm.Box``（左上原点）；无法转换返回 ``None``。"""
    if box is None or page_height <= 0:
        return None
    try:
        x0 = float(box.x)
        y0 = float(box.y)
        x1 = float(box.x2)
        y1 = float(box.y2)
    except (TypeError, ValueError, AttributeError):
        return None
    return dm.Box(
        x0=round(x0, 3),
        y0=round(page_height - y1, 3),
        x1=round(x1, 3),
        y1=round(page_height - y0, 3),
    )


def _read_json(path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _archive_if_exists(recorder, name: str, path) -> str | None:
    """证据文件存在才归档；可选产物缺失不算采集错误。"""
    if path is None:
        return None
    candidate = Path(path)
    if not candidate.is_file():
        return None
    return recorder.archive_file(STAGE, name, candidate)


# --------------------------------------------------------------------------- #
# 输入与页面几何
# --------------------------------------------------------------------------- #
def capture_input_pdf(recorder, pdf_path) -> None:
    """归档调用方给的原始 PDF（裁页前）。"""
    _archive_if_exists(recorder, "input.pdf", pdf_path)


def capture_pdf_prepared(recorder, temp_pdf_path, *, page_count: int, trimmed) -> None:
    """归档实际被解析的 PDF（裁页/预处理后的 ``input.pdf`` 副本）。"""
    ref = _archive_if_exists(recorder, "prepared.pdf", temp_pdf_path)
    recorder.record_event(
        STAGE,
        "pdf_prepared",
        {
            "pages": page_count,
            "trimmed": trimmed,
            "prepared_pdf": ref,
        },
    )


def capture_page_frames(
    doc_pdf,
    recorder,
    *,
    original_pages: list[int] | None = None,
    mediabox_data: dict | None = None,
) -> None:
    """逐页 ``PageFrame``：MediaBox/CropBox/旋转/显示矩阵 + 原始页号映射。

    ``mediabox_data`` 是 ``fix_media_box`` 收集的归一化前页面盒子
    （``{xref: {"MediaBox": ..., "CropBox": ...}}``），存在则原样保留。
    """
    frames = []
    for index in range(len(doc_pdf)):
        page = doc_pdf[index]
        frame = dm.frame_from_pymupdf_page(page, index + 1)
        entry = frame.to_dict()
        if original_pages and index < len(original_pages):
            entry["page_number_original"] = int(original_pages[index])
        xref = getattr(page, "xref", None)
        if mediabox_data and xref in mediabox_data:
            entry["original_boxes"] = mediabox_data[xref]
        frames.append(entry)
    recorder.write_snapshot(
        STAGE,
        "page-frames",
        {"version": 1, "coord_system": dm.PDF_TOPLEFT, "frames": frames},
    )
    recorder.record_event(STAGE, "page_frames", {"pages": len(frames)})


# --------------------------------------------------------------------------- #
# 原生字符 / 布局
# --------------------------------------------------------------------------- #
def capture_native_chars(docs, recorder) -> None:
    """逐页原生字符 + 字体表（``native-chars.json``，字符层按需供前端启用）。"""
    pages = []
    total = 0
    for page in docs.page:
        height = _page_height(page)
        fonts = {}
        for font in page.pdf_font or []:
            if font.font_id is None:
                continue
            fonts[str(font.font_id)] = {
                "name": font.name,
                "bold": font.bold,
                "italic": font.italic,
                "monospace": font.monospace,
                "serif": font.serif,
                "encoding_length": font.encoding_length,
            }
        chars = []
        for index, char in enumerate(page.pdf_character or []):
            style = getattr(char, "pdf_style", None)
            box = _il_box_to_topleft(getattr(char, "box", None), height)
            chars.append(
                {
                    "id": f"C{page.page_number + 1:03d}-{index:05d}",
                    "u": char.char_unicode or "",
                    "box": box.to_dict() if box is not None else None,
                    "font": getattr(style, "font_id", None),
                    "size": getattr(style, "font_size", None),
                }
            )
        total += len(chars)
        pages.append(
            {"page_index": page.page_number, "fonts": fonts, "chars": chars}
        )
    recorder.write_snapshot(
        STAGE,
        "native-chars",
        {"version": 1, "coord_system": dm.PDF_TOPLEFT, "pages": pages},
    )
    recorder.record_event(
        STAGE, "native_chars", {"pages": len(pages), "chars": total}
    )


def capture_layout(docs, recorder, *, backend=None, error=None) -> None:
    """适配后的 layout 区域（``layout.json``）；``error`` 标注采集时已过门禁失败。"""
    pages = []
    total = 0
    labels: dict[str, int] = {}
    for page in docs.page:
        height = _page_height(page)
        entities = []
        for layout_region in page.page_layout or []:
            box = _il_box_to_topleft(getattr(layout_region, "box", None), height)
            region_id = getattr(layout_region, "id", None) or len(entities) + 1
            entities.append(
                dm.Entity(
                    id=f"L{page.page_number + 1:02d}-{int(region_id):03d}",
                    kind="layout_region",
                    label=layout_region.class_name,
                    page=page.page_number + 1,
                    box=box,
                    attrs={"conf": getattr(layout_region, "conf", None)},
                ).to_dict()
            )
            labels[layout_region.class_name or "unknown"] = (
                labels.get(layout_region.class_name or "unknown", 0) + 1
            )
        total += len(entities)
        pages.append(
            {
                "page_index": page.page_number,
                "height": height,
                "entities": entities,
            }
        )
    recorder.write_snapshot(
        STAGE,
        "layout",
        {
            "version": 1,
            "coord_system": dm.PDF_TOPLEFT,
            "backend": backend,
            "pages": pages,
        },
    )
    data = {"backend": backend, "regions": total, "labels": labels}
    if error is not None:
        data["error"] = str(error)[:300]
    recorder.record_event(STAGE, "layout_parsed", data)


def capture_coverage(config, recorder) -> None:
    """布局覆盖率审计：门禁前已写 ``layout_coverage.json``，读回做事件+归档。"""
    try:
        report_path = Path(
            config.get_working_file_path("layout_coverage.json")
        )
    except Exception:  # noqa: BLE001
        report_path = None
    report = _read_json(report_path) if report_path else None
    ref = _archive_if_exists(recorder, "layout-coverage.json", report_path)
    data = {"report": ref}
    if report:
        data.update(
            {
                "passed": report.get("passed"),
                "threshold": report.get("threshold"),
                "uncovered_ratio": report.get("global", {}).get("uncovered_ratio"),
                "uncovered_chars": report.get("global", {}).get("uncovered_chars"),
                "total_chars": report.get("global", {}).get("total_chars"),
            }
        )
    else:
        data["report_missing"] = True
    recorder.record_event(STAGE, "layout_coverage", data)


def archive_provider_artifacts(recorder, workdir, *, backend) -> None:
    """归档 provider 原始响应与 IR（MinerU ``layout_raw.json`` / Paddle ``layout.json``）。"""
    from babeldoc.docvision.provider_paths import provider_artifact_path
    from babeldoc.format.pdf.document_il.midend.inline_math_protector import (
        provider_ir_path,
    )

    agent = Path(workdir) / "agent"
    ir_path = provider_ir_path(agent, workdir)
    _archive_if_exists(recorder, "provider-ir.json", ir_path)
    raw_path = None
    if backend == "paddle":
        raw_path = provider_artifact_path(agent, workdir, "layout.json", existing=True)
    elif ir_path is not None:
        raw_path = ir_path.with_name("layout_raw.json")
    ref = _archive_if_exists(recorder, "provider-layout.json", raw_path)
    recorder.record_event(
        STAGE,
        "provider_artifacts",
        {
            "backend": backend,
            "provider_ir": ir_path is not None,
            "raw_layout": ref,
        },
    )


# --------------------------------------------------------------------------- #
# midend 增强 pass
# --------------------------------------------------------------------------- #
def capture_inline_math(recorder, workdir) -> None:
    """行内公式保护 + 原生↔provider 对齐审计（``alignment.json``）。"""
    from babeldoc.format.pdf.document_il.midend.inline_math_protector import (
        provider_ir_path,
    )

    agent = Path(workdir) / "agent"
    ir_path = provider_ir_path(agent, workdir)
    alignment_path = (
        ir_path.with_name("alignment.json") if ir_path is not None else None
    )
    report = _read_json(alignment_path) if alignment_path else None
    ref = _archive_if_exists(recorder, "alignment.json", alignment_path)
    data = {"report": ref}
    if report and isinstance(report.get("summary"), dict):
        data["summary"] = report["summary"]
        data["protected"] = report["summary"].get("protected_inline_math", 0)
    elif ir_path is None:
        data["status"] = "skipped"
        data["reason"] = "provider_ir_missing"
    else:
        data["status"] = "skipped"
        data["reason"] = "alignment_missing"
    recorder.record_event(STAGE, "inline_math", data)


def capture_ocr_fusion(recorder, workdir, *, enabled) -> None:
    """provider OCR 回填审计（``ocr_fusion.json``）；未启用时如实标注。"""
    agent = Path(workdir) / "agent"
    report_path = agent / "source" / "mineru" / "ocr_fusion.json"
    report = _read_json(report_path)
    ref = _archive_if_exists(recorder, "ocr-fusion.json", report_path)
    data = {"enabled": enabled, "report": ref}
    if report and isinstance(report.get("summary"), dict):
        data["summary"] = report["summary"]
    elif enabled:
        data["report_missing"] = True
    recorder.record_event(STAGE, "ocr_backfill", data)


def capture_enclosed_marker(fixer, recorder) -> None:
    """圈号修复统计（``EnclosedMarkerFixer.last_stats``，由 process 写入）。"""
    stats = getattr(fixer, "last_stats", None)
    recorder.record_event(
        STAGE,
        "enclosed_marker",
        stats if isinstance(stats, dict) else {"enabled": False},
    )


def capture_toc(recorder, workdir) -> None:
    """目录条目化审计（``source/toc.json``）。"""
    report_path = Path(workdir) / "agent" / "source" / "toc.json"
    report = _read_json(report_path)
    ref = _archive_if_exists(recorder, "toc.json", report_path)
    data = {"report": ref}
    if report and isinstance(report.get("summary"), dict):
        data["summary"] = report["summary"]
    else:
        data["entries"] = 0
    recorder.record_event(STAGE, "toc", data)


def capture_styles_formulas(docs, recorder) -> None:
    """公式与样式处理后的计数事件。"""
    formulas = 0
    paragraphs = 0
    for page in docs.page:
        for paragraph in page.pdf_paragraph or []:
            paragraphs += 1
            for composition in paragraph.pdf_paragraph_composition or []:
                if composition.pdf_formula is not None:
                    formulas += 1
    recorder.record_event(
        STAGE,
        "styles_formulas",
        {"paragraphs": paragraphs, "formulas": formulas},
    )


# --------------------------------------------------------------------------- #
# 段落 / 选择 / 链接
# --------------------------------------------------------------------------- #
def capture_paragraphs(docs, recorder) -> None:
    """段落快照（``paragraphs.json``）：确定性 id + 与 layout 的显式关联。

    必须在 ``_deterministic_ids`` 之后调用——``debug_id`` 此时才是
    ``P<页>-<序>`` 稳定 id。
    """
    entities = []
    relations = []
    for page in docs.page:
        height = _page_height(page)
        fallback = 0
        for paragraph in page.pdf_paragraph or []:
            fallback += 1
            entity_id = paragraph.debug_id or f"px{page.page_number + 1:02d}-{fallback:03d}"
            box = _il_box_to_topleft(paragraph.box, height)
            entities.append(
                dm.Entity(
                    id=entity_id,
                    kind="paragraph",
                    label=paragraph.layout_label,
                    page=page.page_number + 1,
                    box=box,
                    attrs={
                        "layout_id": paragraph.layout_id,
                        "unicode": paragraph.unicode or "",
                        "compositions": len(
                            paragraph.pdf_paragraph_composition or []
                        ),
                    },
                ).to_dict()
            )
            layout_id = paragraph.layout_id
            if layout_id is not None:
                relations.append(
                    dm.Relation(
                        from_id=entity_id,
                        to_id=f"L{page.page_number + 1:02d}-{int(layout_id):03d}",
                        kind="in_layout",
                        method="explicit",
                    ).to_dict()
                )
    recorder.write_snapshot(
        STAGE,
        "paragraphs",
        {
            "version": 1,
            "coord_system": dm.PDF_TOPLEFT,
            "entities": entities,
            "relations": relations,
        },
    )
    recorder.record_event(
        STAGE,
        "paragraphs_found",
        {"paragraphs": len(entities), "relations": len(relations)},
    )


def capture_source_geometry(source_line_geometry, recorder) -> None:
    """LaTeX bbox 源行几何采集结果计数。"""
    recorder.record_event(
        STAGE,
        "source_geometry",
        {"entries": len(source_line_geometry or {})},
    )


def capture_links(link_state, bookmark_result, recorder, workdir) -> None:
    """超链接/书签快照事件 + 归档 ``links.json`` / ``bookmarks.json``。"""
    link_snapshot = (link_state or {}).get("link_snapshot") or {}
    link_count = sum(len(entries) for entries in link_snapshot.values())
    agent = Path(workdir) / "agent"
    links_ref = _archive_if_exists(recorder, "links.json", agent / "source" / "links.json")
    bookmarks_ref = _archive_if_exists(
        recorder, "bookmarks.json", agent / "source" / "bookmarks.json"
    )
    recorder.record_event(
        STAGE,
        "links_snapshot",
        {
            "links": link_count,
            "links_artifact": links_ref,
            "bookmarks": int((bookmark_result or {}).get("count") or 0),
            "bookmarks_artifact": bookmarks_ref,
        },
    )


def capture_selection(
    rows, skipped_rows, label_counts, skipped_label_counts, recorder
) -> None:
    """翻译选择快照（``selection.json``）：选中行 + 跳过行及理由。"""
    recorder.write_snapshot(
        STAGE,
        "selection",
        {
            "version": 1,
            "selected": rows,
            "skipped": skipped_rows,
            "label_counts": label_counts,
            "skipped_label_counts": skipped_label_counts,
        },
    )
    recorder.record_event(
        STAGE,
        "selection",
        {
            "selected": len(rows),
            "skipped": len(skipped_rows),
            "label_counts": label_counts,
            "skipped_label_counts": skipped_label_counts,
        },
    )
