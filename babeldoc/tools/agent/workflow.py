"""agent 文档翻译工作流：(解析) → (subagent 翻译) → apply → reconstruct。

设计：
- apply：读取译文 sheet，用 tools/agent/protocol 校验（id 对齐 + 占位符
  多重集），通过后经 ILTranslator.post_translate_paragraph 写回段落并
  重建 composition。state.pkl（含解析后的 IR 与 TranslateInput，单次
  pickle 保持 Document 与 TranslateInput 共享公式对象的同一性）由
  ``markdown_view`` 解析阶段落盘。
- reconstruct：从 state.pkl 加载写回后的 IR，跑 Typesetting + PDFCreater
  生成 mono/dual PDF（复用解析阶段保存的 temp pdf 与 mediabox_data）。
- render：PDF 页 → PNG（视觉审查用）。

翻译本身完全由外部 agent subagent 完成，本模块不含任何 LLM 调用。
"""

from __future__ import annotations

import json
import logging
import pickle
import re
import shutil
from pathlib import Path

from babeldoc.format.pdf.document_il.backend.pdf_creater import PDFCreater
from babeldoc.format.pdf.document_il.midend.il_translator import ILTranslator
from babeldoc.format.pdf.document_il.midend.il_translator import PageTranslateTracker
from babeldoc.format.pdf.document_il.midend.typesetting import Typesetting
from babeldoc.format.pdf.document_il.xml_converter import XMLConverter
from babeldoc.format.pdf.high_level import fix_filter
from babeldoc.format.pdf.high_level import fix_media_box
from babeldoc.format.pdf.high_level import fix_null_page_content
from babeldoc.format.pdf.high_level import fix_null_xref
from babeldoc.format.pdf.high_level import get_translation_stage
from babeldoc.format.pdf.high_level import open_pdf_with_save_fallback
from babeldoc.format.pdf.high_level import save_pdf_with_same_path_fallback
from babeldoc.format.pdf.parse_shared import build_parse_only_config
from babeldoc.format.pdf.translation_config import WatermarkOutputMode
from babeldoc.tools.agent import protocol
from babeldoc.tools.agent.sheet_translator import SheetProtocolTranslator

AGENT_DIR = "agent"
STATE_FILE = "state.pkl"

logger = logging.getLogger(__name__)


def agent_dir(workdir: str | Path) -> Path:
    return Path(workdir) / AGENT_DIR


def state_path(workdir: str | Path) -> Path:
    return agent_dir(workdir) / STATE_FILE


def _base_config(pdf_path, workdir, lang_in, lang_out):
    """extract/apply/reconstruct 共用的基础配置（layout model 占位）。

    parse_shared 的单阶段 monitor 不含 LayoutParser/Typesetting 阶段名，
    这里换成 high_level 的完整阶段表（按 skip_* 标志裁剪）。
    """
    from babeldoc.progress_monitor import ProgressMonitor

    config = build_parse_only_config(pdf_path, working_dir=workdir)
    config.lang_in = lang_in
    config.lang_out = lang_out
    config.progress_monitor = ProgressMonitor(get_translation_stage(config))
    return config


def _prepare_pdf(pdf_path, config):
    """high_level 的预处理链（inline 以捕获 mediabox_data）。"""
    pdf_path = Path(pdf_path)
    temp_pdf_path = config.get_working_file_path("input.pdf")
    shutil.copy2(pdf_path, temp_pdf_path)

    doc_pdf = open_pdf_with_save_fallback(pdf_path, temp_pdf_path)
    fix_null_page_content(doc_pdf)
    fix_filter(doc_pdf)
    fix_null_xref(doc_pdf)
    mediabox_data = fix_media_box(doc_pdf)
    doc_pdf = save_pdf_with_same_path_fallback(doc_pdf, temp_pdf_path)
    return doc_pdf, temp_pdf_path, mediabox_data


def _trim_pages(pdf_path: Path, pages: list[int], out_path: Path) -> Path:
    """按页码裁剪 PDF（1-based），供 --pages 使用。"""
    import pymupdf

    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc = pymupdf.open(pdf_path)
    doc.select([p - 1 for p in pages])
    doc.save(out_path, garbage=3, deflate=True)
    return out_path


def _page_font_maps(page):
    page_font_map = {font.font_id: font for font in page.pdf_font}
    page_xobj_font_map = {}
    for xobj in page.pdf_xobject:
        merged = page_font_map.copy()
        for font in xobj.pdf_font:
            merged[font.font_id] = font
        page_xobj_font_map[xobj.xobj_id] = merged
    return page_font_map, page_xobj_font_map


def _bump_title_font_size(paragraph):
    """标题类段落字号修正。

    IEEE/ACM 标题用小型大写字母排版：首字母 ~10pt、其余 ~8pt 两种字号混排，
    解析器按多数字符取段级 pdf_style 时会选中偏小的小型大写字号，导致译文
    整段按小字号渲染（如 'I. 引言' 明显小于原文）。这里把标题段的段级字号
    提到段落内最大 run 字号（即首字母字号）。只影响 base_style（译文的
    默认样式），composition 各 run 自身样式不动，跳过段落不受影响。
    """
    import copy as _copy

    if (paragraph.layout_label or "").strip().lower() not in (
        "title",
        "doc_title",
        "paragraph_title",
    ):
        return
    if not paragraph.pdf_style or not paragraph.pdf_paragraph_composition:
        return
    sizes = []
    for c in paragraph.pdf_paragraph_composition:
        ssc = c.pdf_same_style_unicode_characters
        if ssc and ssc.pdf_style and ssc.pdf_style.font_size:
            sizes.append(ssc.pdf_style.font_size)
        chars = []
        if c.pdf_same_style_characters and c.pdf_same_style_characters.pdf_character:
            chars = c.pdf_same_style_characters.pdf_character
        elif c.pdf_character:
            chars = c.pdf_character
        for ch in chars:
            if ch.pdf_style and ch.pdf_style.font_size:
                sizes.append(ch.pdf_style.font_size)
    if sizes and paragraph.pdf_style.font_size < max(sizes):
        new_style = _copy.copy(paragraph.pdf_style)
        new_style.font_size = max(sizes)
        paragraph.pdf_style = new_style


def _formula_expansion(translator_input) -> dict[str, str]:
    """{vN} 占位符 -> 渲染时展开的原文文本（含原有尾随标点，如 '[17],'）。"""
    expansions = {}
    for placeholder in translator_input.placeholders:
        formula = getattr(placeholder, "formula", None)
        token = getattr(placeholder, "placeholder", None)
        if formula is None or not token:
            continue
        chars = getattr(formula, "pdf_character", None) or []
        expansions[token] = "".join(
            c.char_unicode or "" for c in chars
        )
    return expansions


# 占位符展开尾部已有标点时，译文中紧随的同类中文标点会造成双重标点
# （渲染为 "[17],，"），归一化时删除。
_STRIP_AFTER_COMMA = ("，", "、")
_STRIP_AFTER_PERIOD = ("。",)


def _normalize_placeholder_punctuation(
    target: str, expansions: dict[str, str]
) -> tuple[str, list[str]]:
    """删除与占位符展开尾随标点重复的中文标点。返回 (新文本, 修改记录)。"""
    changes = []
    for token, expansion in expansions.items():
        stripped = expansion.rstrip()
        if not stripped:
            continue
        if stripped.endswith((",", "，", "、")):
            pattern = re.compile(
                re.escape(token) + r"\s*([，、])"
            )
        elif stripped.endswith("."):
            pattern = re.compile(re.escape(token) + r"\s*(。)")
        else:
            continue
        new_target, n = pattern.subn(token, target)
        if n:
            changes.append(f"{token}: removed {n} duplicate punctuation")
            target = new_target
    return target, changes


def apply(workdir, translated_sheet, *, debug_recorder=None):
    """校验译文 sheet 并写回 IR。返回报告 dict；失败时 ok=False。"""
    workdir = Path(workdir)
    with state_path(workdir).open("rb") as f:
        state = pickle.load(f)  # noqa: S301 - workdir 私有产物，非不可信输入
    doc = state["doc"]
    inputs = state["inputs"]

    index = {}
    for page in doc.page:
        for paragraph in page.pdf_paragraph:
            if paragraph.debug_id:
                index[paragraph.debug_id] = paragraph

    entries = list(_iter_sheet(Path(translated_sheet)))

    unknown = [e["id"] for e in entries if e["id"] not in inputs]
    violations = []
    for entry in entries:
        if entry["id"] in unknown:
            continue
        source = inputs[entry["id"]].unicode
        violations.extend(
            protocol.check_placeholders(entry["id"], source, entry["target"])
        )

    if debug_recorder:
        debug_recorder.record_event("apply", "placeholder_validation", {
            "unknown_ids": unknown, "violations": violations,
            "valid": not (unknown or violations), "entries": len(entries),
        })
    if unknown or violations:
        return {
            "applied": 0,
            "unknown_ids": unknown,
            "violations": violations,
            "ok": False,
        }

    config = _base_config(
        state["pdf_path"], workdir, state["lang_in"], state["lang_out"]
    )
    il_translator = ILTranslator(
        SheetProtocolTranslator(state["lang_in"], state["lang_out"], True), config
    )
    page_tracker = PageTranslateTracker()
    applied = 0
    punctuation_fixes = []
    for entry in entries:
        target = entry["target"]
        target, changes = _normalize_placeholder_punctuation(
            target, _formula_expansion(inputs[entry["id"]])
        )
        for change in changes:
            punctuation_fixes.append({"id": entry["id"], "change": change})
        il_translator.post_translate_paragraph(
            index[entry["id"]],
            page_tracker.new_paragraph(),
            inputs[entry["id"]],
            target,
        )
        applied += 1
        if debug_recorder:
            debug_recorder.record_event("apply", "canonical_writeback", {
                "id": entry["id"], "source": inputs[entry["id"]].unicode,
                "canonical_before": entry["target"], "canonical_after": target,
                "unicode": index[entry["id"]].unicode,
                "punctuation_fixes": changes, "persisted": False,
            })

    with state_path(workdir).open("wb") as f:
        pickle.dump(state, f)
    XMLConverter().write_json(
        doc, str(agent_dir(workdir) / "il_translated.applied.json")
    )
    if debug_recorder:
        debug_recorder.record_event("apply", "writeback_saved", {"applied": applied})
    return {
        "applied": applied,
        "unknown_ids": [],
        "violations": [],
        "punctuation_fixes": punctuation_fixes,
        "ok": True,
    }


def reconstruct(
    workdir,
    output_dir=None,
    no_dual=True,
    watermark=False,
    latex_bbox=True,
    latex_bbox_mode=None,
    debug_recorder=None,
    debug_recompile=False,
):
    """从写回后的 IR 重排并生成 PDF。返回输出路径 dict。

    支持 agent 排版微调：读 ``<workdir>/agent/layout_overrides.json`` →
    注入 Typesetting（scale_cap / line_skip / 强制换行）并在 IR 上应用
    box / font_scale；Typesetting 之后 dump ``agent/layout_geometry.json``。

    ``latex_bbox`` 默认开启：Typesetting 之前捕获源几何与公式融合 body，
    PDFCreater 在内容流生成时跳过已贴片段落的字符并贴片；能力缺失或任何
    失败自动回退现有渲染。传 ``latex_bbox=False``（CLI ``--no-latex-bbox``）
    关闭，关闭时仅使用原生排版（包括源主标题居中）。``latex_bbox_mode`` 可选
    ``"full"``（默认）/ ``"repair"``。
    """
    from babeldoc.format.pdf.document_il.backend.latex_bbox.source_geometry import (
        geometry_from_char_objects,
    )
    from babeldoc.tools.agent import layout_geometry
    from babeldoc.tools.agent import layout_overrides

    workdir = Path(workdir)
    with state_path(workdir).open("rb") as f:
        state = pickle.load(f)  # noqa: S301 - workdir 私有产物，非不可信输入
    doc = state["doc"]

    temp_pdf_path = Path(state["temp_pdf_path"])
    if not temp_pdf_path.exists():
        raise FileNotFoundError(f"解析产物 PDF 不存在: {temp_pdf_path}，请重新 extract")

    config = _base_config(
        state["pdf_path"], workdir, state["lang_in"], state["lang_out"]
    )
    if output_dir:
        # output_dir 经 post-init 赋值，不会触发 __init__ 里的 mkdir
        Path(output_dir).mkdir(parents=True, exist_ok=True)
    config.output_dir = output_dir
    config.no_dual = no_dual
    config.no_mono = False
    if debug_recorder is None:
        from babeldoc import debug_recorder as _dr

        debug_recorder = _dr.get_current()
    config.debug_recorder = debug_recorder
    config.latex_debug_recompile = bool(debug_recompile)
    config.watermark_output_mode = (
        WatermarkOutputMode.Watermarked if watermark else WatermarkOutputMode.NoWatermark
    )

    overrides = layout_overrides.load_overrides(workdir)
    config.paragraph_layout_overrides = layout_overrides.to_config_hook(overrides)
    config.page_font_scale = {
        int(page): float(patch["font_scale"])
        for page, patch in (overrides.get("pages") or {}).items()
        if isinstance(patch, dict) and patch.get("font_scale")
    }
    config.source_line_geometry = state.get("source_line_geometry") or {}
    if not config.source_line_geometry:
        config.source_line_geometry = geometry_from_char_objects(
            doc, state.get("page_char_objects") or {}
        )
    source_state = layout_geometry.capture_source_state(doc, overrides)
    ir_stats = layout_overrides.apply_to_ir(doc, overrides)
    layout_geometry.capture_source_state(doc, overrides, state=source_state)

    if latex_bbox:
        config.enable_latex_bbox_layout = True
        if latex_bbox_mode:
            config.latex_bbox_mode = str(latex_bbox_mode)
        # 公式融合需要 provider IR（extract 时落在 <workdir>/agent；
        # _base_config 不设 provider_ir_dir，这里补齐，与 extract 保持一致）。
        config.provider_ir_dir = agent_dir(workdir)
        # 片段级公式嵌图需要源 PDF（不是 mono 产物）：用 extract 落的输入副本。
        config.latex_source_pdf_path = str(temp_pdf_path)
        from babeldoc.format.pdf.document_il.backend.latex_bbox import (
            capture_layout_sources,
        )

        try:
            # 未翻译段判定：用 extract 时记录的源文（translated.jsonl 的输入侧）。
            source_texts = {
                debug_id: getattr(item, "unicode", "") or ""
                for debug_id, item in (state.get("inputs") or {}).items()
            }
            # 源行几何（P3-0）：extract 落的为主；旧 workdir 用 extract 时的
            # page_char_objects（box 仍是源坐标）按段 box 聚类兜底。
            config.latex_source_geometry = config.source_line_geometry
            capture_layout_sources(
                doc,
                config,
                source_texts=source_texts,
                # 超链接快照：角标引文的颜色/字号/抬升 + 字符归属（fusion 的
                # cornermark 级用它把上标引文渲染成 bdoclink 标记链接）。
                link_state={
                    "link_snapshot": state.get("link_snapshot") or {},
                    "page_char_objects": state.get("page_char_objects") or {},
                },
            )
        except Exception:
            logger.warning(
                "LaTeX bbox 版面源捕获失败，回退现有渲染路径", exc_info=True
            )
            config.enable_latex_bbox_layout = False
    else:
        # 显式关闭 overlay，不受 TranslationConfig 默认值影响。
        config.enable_latex_bbox_layout = False

    Typesetting(config).typesetting_document(doc)

    geometry = layout_geometry.build_geometry(doc, source_state, overrides)
    geometry["warnings"] = list(getattr(config, "layout_warnings", []) or [])
    geometry["ir_overrides"] = ir_stats
    geometry_path = layout_geometry.write_geometry(workdir, geometry)

    pdf_creater = PDFCreater(
        str(temp_pdf_path),
        doc,
        config,
        state["mediabox_data"],
        link_remap_state={
            "link_snapshot": state.get("link_snapshot") or {},
            "page_char_objects": state.get("page_char_objects") or {},
        },
    )
    result = pdf_creater.write(config)
    # decisions[] 已完整落盘 latex_bbox_report.json；stdout 只给摘要，
    # 避免把数十 KB 的逐段决策重复展开到终端。
    latex_stats_summary = _latex_stats_summary(pdf_creater.latex_bbox_stats)
    return {
        "mono_pdf": str(result.mono_pdf_path) if result.mono_pdf_path else None,
        "dual_pdf": str(result.dual_pdf_path) if result.dual_pdf_path else None,
        "layout_geometry": str(geometry_path),
        "layout_override_stats": ir_stats,
        "layout_warnings": list(getattr(config, "layout_warnings", []) or []),
        "link_total": pdf_creater.link_stats.get("total", 0),
        "link_remapped": pdf_creater.link_stats.get("remapped", 0),
        "link_fallback_paragraph": pdf_creater.link_stats.get(
            "fallback_paragraph", 0
        ),
        "link_unresolved": pdf_creater.link_stats.get("unresolved", []),
        "link_uri_set_match": pdf_creater.link_stats.get("uri_set_match"),
        "latex_bbox": latex_stats_summary,
        "latex_bbox_report": (
            str(Path(config.working_dir) / "latex_bbox_report.json")
            if pdf_creater.latex_bbox_stats
            else None
        ),
    }


def _latex_stats_summary(stats: dict | None) -> dict | None:
    """去掉逐段 decisions，只留摘要（完整报告在 latex_bbox_report.json）。"""
    if not stats:
        return stats
    summary = dict(stats)
    decisions = summary.pop("decisions", None)
    if decisions is not None:
        summary["decision_count"] = len(decisions)
    return summary


def render(pdf_path, pages, dpi=110, out_dir=None):
    """渲染 PDF 页为 PNG，返回路径列表。pages 形如 "1,2" 或 "1-3"。"""
    import pymupdf

    pdf_path = Path(pdf_path)
    out_dir = Path(out_dir) if out_dir else pdf_path.parent / "render"
    out_dir.mkdir(parents=True, exist_ok=True)

    selected = _parse_pages(pages)
    doc = pymupdf.open(pdf_path)
    paths = []
    for page_number in selected:
        if page_number < 1 or page_number > len(doc):
            continue
        page = doc[page_number - 1]
        out = out_dir / f"page-{page_number:02d}.png"
        page.get_pixmap(dpi=dpi).save(out)
        paths.append(str(out))
    return {"images": paths}


def _iter_sheet(path: Path):
    """读取译文 sheet：支持 JSONL 或 JSON 数组两种格式。"""
    text = path.read_text(encoding="utf-8").strip()
    if text.startswith("["):
        entries = json.loads(text)
    else:
        entries = [json.loads(line) for line in text.splitlines() if line.strip()]
    for entry in entries:
        if not isinstance(entry, dict) or "id" not in entry or "target" not in entry:
            raise ValueError(f"sheet 行缺少 id/target 字段: {str(entry)[:120]}")
        yield entry


def _parse_pages(pages: str | None) -> list[int]:
    if not pages:
        return []
    result = []
    for part in pages.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            start, _, end = part.partition("-")
            result.extend(range(int(start), int(end) + 1))
        else:
            result.append(int(part))
    return result
