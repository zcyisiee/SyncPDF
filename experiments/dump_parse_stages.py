"""解析管线分阶段快照导出器（诊断/优化用）。

把解析管线（`workflow` 与 `markdown_view` 共用的阶段）拆成可观测的 6 步，
每步把中间产物落盘到 <out-dir>/，便于对照优化：

    00_input.json          PDF 元信息（页数、mediabox、sha256）
    01_prepare.json        _prepare_pdf 之后（修复后的临时 PDF + mediabox_data）
    02_raw_ir.json         new parser -> legacy IR 后的原始文本层（字符/行/段）
    03_layout.json         LayoutParser 之后的版面框（class_name/box/conf）
    04_paragraphs.json     ParagraphFinder 之后的段落（debug_id/layout_label/box/unicode）
    05_styles_formulas.json StylesAndFormulas 之后的 composition runs（样式/公式）
    06_sheet.jsonl         与解析阶段一致的待译清单（source 字符串）
    06_translate_inputs.json 每个 id 的占位符明细（{vN} / <style id='N'> 各自是什么）

用法：
    python experiments/dump_parse_stages.py <pdf> --out-dir <dir> \
        [--workdir <dir>] [--layout mineru] \
        [--mineru-json <cached layout.json>] [--pages 1,2] [--detail-page 1]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

#: 回放缓存布局时使用的占位 token（不发起 MinerU API 调用）。
_MINERU_REPLAY_ARG = "replay"


def box_dict(b):
    if b is None:
        return None
    return {
        "x": round(float(b.x), 2),
        "y": round(float(b.y), 2),
        "x2": round(float(b.x2), 2),
        "y2": round(float(b.y2), 2),
        "w": round(float(b.x2 - b.x), 2),
        "h": round(float(b.y2 - b.y), 2),
    }


def style_dict(s):
    if s is None:
        return None
    return {
        "font_id": s.font_id,
        "font_size": s.font_size,
        "graphic_state": (
            None
            if s.graphic_state is None
            else {
                "fill_color": getattr(s.graphic_state, "fill_color", None),
                "stroke_color": getattr(s.graphic_state, "stroke_color", None),
            }
        ),
    }


def char_dict(c):
    return {
        "text": c.char_unicode,
        "box": box_dict(c.box),
        "visual_bbox": None
        if c.visual_bbox is None
        else box_dict(c.visual_bbox.box),
        "font_id": None if c.pdf_style is None else c.pdf_style.font_id,
        "font_size": None if c.pdf_style is None else c.pdf_style.font_size,
        "xobj_id": c.xobj_id,
        "vertical": c.vertical,
        "formula_layout_id": c.formula_layout_id,
    }


def run_styles(docs):
    """StylesAndFormulas 之后的 composition run 摘要。"""
    from babeldoc.format.pdf.document_il.utils.layout_helper import (
        get_char_unicode_string,
    )

    out = []
    for paragraph in docs.pdf_paragraph:
        runs = []
        for comp in paragraph.pdf_paragraph_composition:
            if comp.pdf_line is not None:
                runs.append(
                    {
                        "type": "line",
                        "box": box_dict(comp.pdf_line.box),
                        "text": get_char_unicode_string(
                            comp.pdf_line.pdf_character
                        ),
                        "n_chars": len(comp.pdf_line.pdf_character),
                    }
                )
            elif comp.pdf_formula is not None:
                runs.append(
                    {
                        "type": "formula",
                        "box": box_dict(comp.pdf_formula.box),
                        "text": get_char_unicode_string(
                            comp.pdf_formula.pdf_character
                        ),
                        "n_chars": len(comp.pdf_formula.pdf_character),
                        "n_curves": len(comp.pdf_formula.pdf_curve),
                        "n_forms": len(comp.pdf_formula.pdf_form),
                    }
                )
            elif comp.pdf_same_style_characters is not None:
                ssc = comp.pdf_same_style_characters
                runs.append(
                    {
                        "type": "same_style_characters",
                        "box": box_dict(ssc.box),
                        "style": style_dict(ssc.pdf_style),
                        "text": get_char_unicode_string(ssc.pdf_character),
                        "n_chars": len(ssc.pdf_character),
                    }
                )
            elif comp.pdf_same_style_unicode_characters is not None:
                ssc = comp.pdf_same_style_unicode_characters
                runs.append(
                    {
                        "type": "same_style_unicode_characters",
                        "style": style_dict(ssc.pdf_style),
                        "text": ssc.unicode,
                    }
                )
            elif comp.pdf_character is not None:
                runs.append({"type": "character", "text": comp.pdf_character.char_unicode})
        out.append(
            {
                "debug_id": paragraph.debug_id,
                "layout_label": paragraph.layout_label,
                "box": box_dict(paragraph.box),
                "runs": runs,
            }
        )
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("pdf")
    ap.add_argument("--out-dir", required=True)
    ap.add_argument("--workdir", default=None)
    ap.add_argument("--layout", choices=["mineru"], default="mineru")
    ap.add_argument("--mineru-json", default=None)
    ap.add_argument("--pages", default=None, help="1-based，如 1,2 或 1-3")
    ap.add_argument("--detail-page", type=int, default=1, help="详细 dump 的页（1-based）")
    ap.add_argument("--dump-page", type=int, default=None, help="要 dump 详情的页（1-based）")
    ap.add_argument("--lang-in", default="en")
    ap.add_argument("--lang-out", default="zh")
    args = ap.parse_args()

    from babeldoc.tools.agent import workflow

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    workdir = Path(args.workdir or (out_dir / "workdir"))
    workdir.mkdir(parents=True, exist_ok=True)

    pdf_path = Path(args.pdf)
    detail_page = (args.dump_page or args.detail_page) - 1  # 0-based

    def dump(name, payload):
        path = out_dir / name
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        print(f"  -> {path}")
        return path

    # ---- Step 0: 输入 PDF ----
    import pymupdf

    doc = pymupdf.open(pdf_path)
    dump(
        "00_input.json",
        {
            "pdf": str(pdf_path),
            "sha256": hashlib.sha256(pdf_path.read_bytes()).hexdigest(),
            "page_count": doc.page_count,
            "pages": [
                {
                    "page": i + 1,
                    "mediabox": [round(v, 2) for v in p.mediabox],
                    "rotation": p.rotation,
                }
                for i, p in enumerate(doc)
            ],
        },
    )
    doc.close()

    # ---- 配置 ----
    if args.pages:
        pdf_path = workflow._trim_pages(
            pdf_path, workflow._parse_pages(args.pages), workdir / "trimmed.pdf"
        )
    config = workflow._base_config(pdf_path, workdir, args.lang_in, args.lang_out)

    if args.layout != "mineru":
        raise SystemExit("native 布局后端已移除，请使用 --layout mineru")

    from babeldoc.docvision.mineru_doclayout import MinerUDocLayoutModel
    from babeldoc.format.pdf.translation_config import TranslationConfig

    mineru_json = args.mineru_json or os.environ.get("BABELDOC_MINERU_LAYOUT_JSON")
    if not mineru_json:
        raise SystemExit(
            "mineru 模式需要 --mineru-json 或环境变量 BABELDOC_MINERU_LAYOUT_JSON"
        )
    os.environ["BABELDOC_MINERU_LAYOUT_JSON"] = str(mineru_json)
    config.doc_layout_model = MinerUDocLayoutModel(api_token=_MINERU_REPLAY_ARG)
    config.mineru_doclayout_enabled = True
    config.mineru_skip_translate_effective_labels = (
        TranslationConfig.expand_mineru_skip_translate_layout_labels(
            TranslationConfig.get_mineru_default_skip_translate_layout_labels()
        )
    )
    config.skip_scanned_detection = True

    # ---- Step 1: _prepare_pdf ----
    print("[1/6] _prepare_pdf ...")
    doc_pdf, temp_pdf_path, mediabox_data = workflow._prepare_pdf(pdf_path, config)
    dump(
        "01_prepare.json",
        {
            "temp_pdf_path": str(temp_pdf_path),
            "temp_pdf_size": Path(temp_pdf_path).stat().st_size,
            "page_count": doc_pdf.page_count,
            "mediabox_data": mediabox_data,
        },
    )

    # ---- Step 2: new parser -> legacy IR ----
    print("[2/6] parse_prepared_pdf_with_new_parser_to_legacy_ir ...")
    from babeldoc.format.pdf.new_parser.native_parse import (
        parse_prepared_pdf_with_new_parser_to_legacy_ir,
    )

    docs = parse_prepared_pdf_with_new_parser_to_legacy_ir(
        temp_pdf_path, config=config, doc_pdf=doc_pdf
    )
    raw_pages = []
    for page in docs.page:
        n_lines = 0
        for para in page.pdf_paragraph:
            for comp in para.pdf_paragraph_composition:
                if comp.pdf_line is not None:
                    n_lines += 1
                elif comp.pdf_same_style_characters is not None:
                    n_lines += 0
        raw_pages.append(
            {
                "page": page.page_number + 1,
                "mediabox": None
                if page.mediabox is None
                else [page.mediabox.box.x, page.mediabox.box.y,
                      page.mediabox.box.x2, page.mediabox.box.y2],
                "n_chars": len(page.pdf_character),
                "n_lines": n_lines,
                "n_paragraphs": len(page.pdf_paragraph),
                "n_fonts": len(page.pdf_font),
                "n_xobjects": len(page.pdf_xobject),
            }
        )
    dump(
        "02_raw_ir.json",
        {
            "per_page": raw_pages,
            "detail_page": detail_page + 1,
            "first_chars": [
                char_dict(c) for c in docs.page[detail_page].pdf_character[:40]
            ],
            "paragraphs": [
                {
                    "index": i,
                    "box": box_dict(p.box),
                    "unicode": p.unicode,
                    "n_chars": len(p.unicode or ""),
                    "composition_types": [
                        "line"
                        if c.pdf_line
                        else "formula"
                        if c.pdf_formula
                        else "same_style_characters"
                        if c.pdf_same_style_characters
                        else "same_style_unicode_characters"
                        if c.pdf_same_style_unicode_characters
                        else "character"
                        for c in p.pdf_paragraph_composition
                    ],
                    "xobj_id": p.xobj_id,
                }
                for i, p in enumerate(docs.page[detail_page].pdf_paragraph)
            ],
        },
    )

    # ---- Step 3: LayoutParser ----
    print("[3/6] LayoutParser ...")
    from babeldoc.const import close_process_pool
    from babeldoc.format.pdf.document_il.midend.layout_parser import LayoutParser

    docs = LayoutParser(config).process(docs, doc_pdf)
    close_process_pool()
    layout_pages = []
    for page in docs.page:
        counts = {}
        for layout in page.page_layout:
            counts[layout.class_name] = counts.get(layout.class_name, 0) + 1
        layout_pages.append({"page": page.page_number + 1, "counts": counts})
    dump(
        "03_layout.json",
        {
            "per_page": layout_pages,
            "detail_page": detail_page + 1,
            "layouts": [
                {
                    "id": layout.id,
                    "class_name": layout.class_name,
                    "box": box_dict(layout.box),
                    "conf": layout.conf,
                }
                for layout in docs.page[detail_page].page_layout
            ],
        },
    )

    # ---- Step 4: ParagraphFinder ----
    print("[4/6] ParagraphFinder ...")
    from babeldoc.format.pdf.document_il.midend.paragraph_finder import ParagraphFinder

    docs = ParagraphFinder(config).process(docs) or docs
    dump(
        "04_paragraphs.json",
        {
            "detail_page": detail_page + 1,
            "paragraphs": [
                {
                    "debug_id": p.debug_id,
                    "layout_id": p.layout_id,
                    "layout_label": p.layout_label,
                    "box": box_dict(p.box),
                    "unicode": p.unicode,
                    "n_chars": len(p.unicode or ""),
                    "n_compositions": len(p.pdf_paragraph_composition),
                    "composition_types": [
                        "line"
                        if c.pdf_line
                        else "formula"
                        if c.pdf_formula
                        else "same_style_characters"
                        if c.pdf_same_style_characters
                        else "same_style_unicode_characters"
                        if c.pdf_same_style_unicode_characters
                        else "character"
                        for c in p.pdf_paragraph_composition
                    ],
                    "vertical": p.vertical,
                    "first_line_indent": p.first_line_indent,
                }
                for p in docs.page[detail_page].pdf_paragraph
            ],
        },
    )

    # ---- Step 5: StylesAndFormulas ----
    print("[5/6] StylesAndFormulas ...")
    from babeldoc.format.pdf.document_il.midend.styles_and_formulas import (
        StylesAndFormulas,
    )

    docs = StylesAndFormulas(config).process(docs) or docs
    dump(
        "05_styles_formulas.json",
        {
            "detail_page": detail_page + 1,
            "paragraphs": list(run_styles(docs.page[detail_page])),
        },
    )

    # ---- Step 6: ILTranslator.pre_translate_paragraph -> sheet ----
    print("[6/6] ILTranslator.pre_translate_paragraph -> sheet.jsonl ...")
    from babeldoc.format.pdf.document_il.midend.il_translator import ILTranslator
    from babeldoc.format.pdf.document_il.midend.il_translator import (
        PageTranslateTracker,
    )
    from babeldoc.tools.agent.sheet_translator import SheetProtocolTranslator

    il_translator = ILTranslator(
        SheetProtocolTranslator(args.lang_in, args.lang_out, True), config
    )
    rows = []
    inputs = {}
    label_counts = {}
    skipped = {}
    for page in docs.page:
        page_font_map, page_xobj_font_map = workflow._page_font_maps(page)
        tracker = PageTranslateTracker()
        for paragraph in page.pdf_paragraph:
            if not paragraph.debug_id:
                continue
            label = paragraph.layout_label or "text"
            workflow._bump_title_font_size(paragraph)
            text, translate_input = il_translator.pre_translate_paragraph(
                paragraph, tracker.new_paragraph(), page_font_map, page_xobj_font_map
            )
            if text is None:
                skipped[label] = skipped.get(label, 0) + 1
                continue
            rows.append(
                {
                    "id": paragraph.debug_id,
                    "page": page.page_number,
                    "layout_label": label,
                    "source": text,
                }
            )
            inputs[paragraph.debug_id] = translate_input
            label_counts[label] = label_counts.get(label, 0) + 1

    with (out_dir / "06_sheet.jsonl").open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"  -> {out_dir / '06_sheet.jsonl'}")

    dump(
        "06_translate_inputs.json",
        {
            "label_counts": label_counts,
            "skipped_label_counts": skipped,
            "inputs": {
                pid: {
                    "unicode": ti.unicode,
                    "n_placeholders": len(ti.placeholders),
                    "placeholders": [p.to_dict() for p in ti.placeholders],
                }
                for pid, ti in inputs.items()
            },
        },
    )

    print("\n完成。阶段产物目录：", out_dir)
    print("label_counts:", json.dumps(label_counts, ensure_ascii=False))
    print("skipped_label_counts:", json.dumps(skipped, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
