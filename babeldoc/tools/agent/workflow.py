"""agent 文档翻译工作流：extract → (subagent 翻译) → apply → reconstruct。

设计：
- extract：进程内复现管道 parse→layout→paragraph→styles 链（与
  high_level.do_translate 前半段一致），对每个段落跑
  ILTranslator.pre_translate_paragraph 得到带占位符（{vN}、<style>）的源文，
  产出 translation sheet（JSONL，给翻译 subagent）+ state.pkl（给 apply/
  reconstruct；单次 pickle 保持 Document 与 TranslateInput 共享公式对象的
  同一性）。mediabox_data 必须在 extract 时捕获（fix_media_box 不幂等，
  二次调用返回归一化后的值）。
- apply：读取译文 sheet，用 tools/agent/protocol 校验（id 对齐 + 占位符
  多重集），通过后经 ILTranslator.post_translate_paragraph 写回段落并
  重建 composition。
- reconstruct：从 state.pkl 加载写回后的 IR，跑 Typesetting + PDFCreater
  生成 mono/dual PDF（复用 extract 保存的 temp pdf 与 mediabox_data）。
- render：PDF 页 → PNG（视觉审查用）。

翻译本身完全由外部 agent subagent 完成，本模块不含任何 LLM 调用。
"""

from __future__ import annotations

import json
import pickle
import shutil
from pathlib import Path

from babeldoc.format.pdf.document_il.backend.pdf_creater import PDFCreater
from babeldoc.format.pdf.document_il.midend.il_translator import ILTranslator
from babeldoc.format.pdf.document_il.midend.il_translator import PageTranslateTracker
from babeldoc.format.pdf.document_il.midend.layout_parser import LayoutParser
from babeldoc.format.pdf.document_il.midend.paragraph_finder import ParagraphFinder
from babeldoc.format.pdf.document_il.midend.styles_and_formulas import (
    StylesAndFormulas,
)
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
SHEET_FILE = "sheet.jsonl"


def agent_dir(workdir: str | Path) -> Path:
    return Path(workdir) / AGENT_DIR


def state_path(workdir: str | Path) -> Path:
    return agent_dir(workdir) / STATE_FILE


def sheet_path(workdir: str | Path) -> Path:
    return agent_dir(workdir) / SHEET_FILE


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


def extract(
    pdf_path,
    workdir,
    lang_in="en",
    lang_out="zh",
    pages: str | None = None,
):
    """解析 PDF 并导出翻译 sheet + 状态文件。返回统计 dict。"""
    from babeldoc.const import close_process_pool
    from babeldoc.format.pdf.new_parser.native_parse import (
        parse_prepared_pdf_with_new_parser_to_legacy_ir,
    )

    workdir = Path(workdir)
    if pages:
        pdf_path = _trim_pages(
            Path(pdf_path), _parse_pages(pages), workdir / "trimmed.pdf"
        )

    config = _base_config(pdf_path, workdir, lang_in, lang_out)
    # 换成真实 layout model（build_parse_only_config 塞的是占位对象；
    # 自动加载只在 TranslationConfig.__init__ 内触发，事后赋 None 不会加载）
    from babeldoc.docvision.doclayout import DocLayoutModel

    config.doc_layout_model = DocLayoutModel.load_available()
    config.skip_scanned_detection = True

    doc_pdf, temp_pdf_path, mediabox_data = _prepare_pdf(pdf_path, config)
    docs = parse_prepared_pdf_with_new_parser_to_legacy_ir(
        temp_pdf_path, config=config, doc_pdf=doc_pdf
    )
    docs = LayoutParser(config).process(docs, doc_pdf)
    close_process_pool()
    ParagraphFinder(config).process(docs)
    StylesAndFormulas(config).process(docs)

    il_translator = ILTranslator(
        SheetProtocolTranslator(lang_in, lang_out, True), config
    )
    inputs = {}
    rows = []
    label_counts = {}
    for page in docs.page:
        page_font_map, page_xobj_font_map = _page_font_maps(page)
        page_tracker = PageTranslateTracker()
        for paragraph in page.pdf_paragraph:
            if not paragraph.debug_id:
                continue
            text, translate_input = il_translator.pre_translate_paragraph(
                paragraph,
                page_tracker.new_paragraph(),
                page_font_map,
                page_xobj_font_map,
            )
            if text is None:
                continue
            inputs[paragraph.debug_id] = translate_input
            label = paragraph.layout_label or "text"
            rows.append(
                {
                    "id": paragraph.debug_id,
                    "page": page.page_number,
                    "layout_label": label,
                    "source": text,
                }
            )
            label_counts[label] = label_counts.get(label, 0) + 1

    out_dir = agent_dir(workdir)
    out_dir.mkdir(parents=True, exist_ok=True)
    with open(sheet_path(workdir), "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    with open(state_path(workdir), "wb") as f:
        pickle.dump(
            {
                "doc": docs,
                "inputs": inputs,
                "temp_pdf_path": str(temp_pdf_path),
                "pdf_path": str(pdf_path),
                "lang_in": lang_in,
                "lang_out": lang_out,
                "mediabox_data": mediabox_data,
            },
            f,
        )

    return {
        "sheet": str(sheet_path(workdir)),
        "paragraphs": len(rows),
        "layout_label_counts": label_counts,
        "pages": len(docs.page),
    }


def apply(workdir, translated_sheet):
    """校验译文 sheet 并写回 IR。返回报告 dict；失败时 ok=False。"""
    workdir = Path(workdir)
    with open(state_path(workdir), "rb") as f:
        state = pickle.load(f)
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
    for entry in entries:
        il_translator.post_translate_paragraph(
            index[entry["id"]],
            page_tracker.new_paragraph(),
            inputs[entry["id"]],
            entry["target"],
        )
        applied += 1

    with open(state_path(workdir), "wb") as f:
        pickle.dump(state, f)
    XMLConverter().write_json(
        doc, str(agent_dir(workdir) / "il_translated.applied.json")
    )
    return {"applied": applied, "unknown_ids": [], "violations": [], "ok": True}


def reconstruct(workdir, output_dir=None, no_dual=True, watermark=False):
    """从写回后的 IR 重排并生成 PDF。返回输出路径 dict。"""
    workdir = Path(workdir)
    with open(state_path(workdir), "rb") as f:
        state = pickle.load(f)
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
    config.watermark_output_mode = (
        WatermarkOutputMode.Watermarked if watermark else WatermarkOutputMode.NoWatermark
    )

    Typesetting(config).typesetting_document(doc)
    pdf_creater = PDFCreater(
        str(temp_pdf_path), doc, config, state["mediabox_data"]
    )
    result = pdf_creater.write(config)
    return {
        "mono_pdf": str(result.mono_pdf_path) if result.mono_pdf_path else None,
        "dual_pdf": str(result.dual_pdf_path) if result.dual_pdf_path else None,
    }


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
