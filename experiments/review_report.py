"""严格 reviewer：对翻译产物做结构化审查，输出问题清单供迭代。

.. note::
   本脚本已被工具层的 ``review_document`` 取代（数据源改用 ``apply_report``，
   长度比改为相对全文中位比，并新增排版 lint / 回译校验），保留仅供历史比对：
   ``PYTHONPATH=skills/document-translate/tools python -m babeldoc_tools call review_document``

用法：
    python experiments/review_report.py <workdir> <output_pdf> [--render-dir <dir>]

检查项（按严重度）：
  P0 占位符残留        输出 PDF 文本中存在 {vN} / <style> 标签
  P0 未翻译回退        translated.jsonl 中 target == source（协议重试耗尽的段落）
  P1 疑似漏译          target 中文占比 < 20%（en→zh）
  P1 标题字号塌陷      title 段译文渲染字号 < 段级字号 × 0.85
  P2 引用后双标点      target 中 ] 后跟 ，/、 或连续两个中文逗号
  P2 占位符双标点      {vN} 展开尾部已带标点而译文中紧随 ，/、/。
"""

from __future__ import annotations

import argparse
import json
import pickle
import re
import sys
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

CJK = re.compile(r"[\u4e00-\u9fff]")
LEFTOVER_V = re.compile(r"\{\s*v\s*\d+\s*\}")
LEFTOVER_STYLE = re.compile(r"<\s*/?\s*style")
DOUBLE_PUNCT = re.compile(r"\]\s*[，、]|\]\s*,\s*[，、]|[，、]\s*[，、]")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("workdir")
    parser.add_argument("output_pdf")
    args = parser.parse_args()

    agent_dir = Path(args.workdir) / "agent"
    sheet = {
        json.loads(line)["id"]: json.loads(line)
        for line in (agent_dir / "sheet.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    translated = {
        json.loads(line)["id"]: json.loads(line)["target"]
        for line in (agent_dir / "translated.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    }
    state = pickle.load(open(agent_dir / "state.pkl", "rb"))
    para_by_id = {}
    for page in state["doc"].page:
        for para in page.pdf_paragraph:
            if para.debug_id:
                para_by_id[para.debug_id] = para

    doc = pymupdf.open(args.output_pdf)
    page_texts = [p.get_text() for p in doc]
    full_text = "\n".join(page_texts)

    issues = []

    # P0 占位符残留
    for m in LEFTOVER_V.finditer(full_text):
        issues.append({"sev": "P0", "type": "leftover_placeholder", "detail": m.group(0)})
    for m in LEFTOVER_STYLE.finditer(full_text):
        issues.append({"sev": "P0", "type": "leftover_style_tag", "detail": m.group(0)})
    issues_count_p0 = len(issues)

    # P0/P1 逐段文本检查
    fallback, low_cjk = [], []
    for pid, target in translated.items():
        row = sheet.get(pid)
        if row is None:
            continue
        if target == row["source"]:
            fallback.append(pid)
            continue
        ratio = len(CJK.findall(target)) / max(len(target), 1)
        if ratio < 0.2:
            low_cjk.append({"id": pid, "cjk_ratio": round(ratio, 2), "text": target[:40]})
    issues += [{"sev": "P0", "type": "fallback_to_source", "detail": p} for p in fallback]
    issues += [{"sev": "P1", "type": "low_cjk_ratio", **x} for x in low_cjk]

    # P2 引用后双标点
    double_punct = []
    for pid, target in translated.items():
        if DOUBLE_PUNCT.search(target):
            double_punct.append({"id": pid, "text": target[:60]})
    issues += [{"sev": "P2", "type": "double_punct_after_citation", **x} for x in double_punct]

    # P2 占位符双标点：{vN} 展开（state.pkl 中 formula 字符）尾部已带标点，
    # 而译文中占位符后紧跟中文标点 → 渲染成 "[17],，"。
    placeholder_punct = []
    for pid, target in translated.items():
        ti = state["inputs"].get(pid)
        if ti is None:
            continue
        for ph in getattr(ti, "placeholders", []):
            token = getattr(ph, "placeholder", None)
            formula = getattr(ph, "formula", None)
            if not token or formula is None:
                continue
            expansion = "".join(
                c.char_unicode or "" for c in (formula.pdf_character or [])
            ).rstrip()
            if not expansion:
                continue
            if expansion.endswith((",", "，", "、")):
                trailing = "[，、]"
            elif expansion.endswith("."):
                trailing = "[。]"
            else:
                continue
            m = re.search(re.escape(token) + r"\s*" + trailing, target)
            if m:
                placeholder_punct.append(
                    {"id": pid, "token": token, "expansion": expansion[:20]}
                )
    issues += [
        {"sev": "P2", "type": "placeholder_double_punct", **x} for x in placeholder_punct
    ]

    # P2 渲染层双标点：输出 PDF 文本里 ] 后 ASCII+CJK 标点连写、连续中文标点等
    RENDERED_DOUBLE = re.compile(r"\]\s*,\s*[，、。]|[，、]\s*[，、]|,,|、、")
    rendered_double = []
    for pno, text in enumerate(page_texts):
        for m in RENDERED_DOUBLE.finditer(text):
            rendered_double.append(
                {"page": pno + 1, "text": text[max(0, m.start() - 15) : m.end() + 15]}
            )
    issues += [
        {"sev": "P2", "type": "rendered_double_punct", **x} for x in rendered_double
    ]

    # P1 标题字号塌陷：在输出页里定位 title 段译文首个 span 的渲染字号
    span_index = []  # (page, text, size)
    for pno, page in enumerate(doc):
        for block in page.get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    t = span["text"].strip()
                    if t:
                        span_index.append((pno, t, span["size"]))
    title_shrink = []
    for pid, target in translated.items():
        para = para_by_id.get(pid)
        row = sheet.get(pid)
        if para is None or row is None or row["layout_label"] != "title":
            continue
        expected = para.pdf_style.font_size if para.pdf_style else None
        if not expected:
            continue
        probe = re.sub(r"\{v\d+\}|<[^>]*>", "", target).strip()
        probe = probe[:6] if len(probe) >= 6 else probe
        if not probe:
            continue
        rendered = None
        for pno, t, size in span_index:
            if probe in t.replace(" ", ""):
                rendered = size
                break
        if rendered is not None and rendered < expected * 0.85:
            title_shrink.append(
                {
                    "id": pid,
                    "expected": round(expected, 1),
                    "rendered": round(rendered, 1),
                    "text": probe,
                }
            )
    issues += [{"sev": "P1", "type": "title_font_shrink", **x} for x in title_shrink]

    summary = {
        "pdf_pages": len(doc),
        "sheet_rows": len(sheet),
        "translated_rows": len(translated),
        "p0_placeholder_leftovers": issues_count_p0,
        "p0_fallback_rows": len(fallback),
        "p1_low_cjk": len(low_cjk),
        "p1_title_shrink": len(title_shrink),
        "p2_double_punct": len(double_punct),
        "p2_placeholder_double_punct": len(placeholder_punct),
        "p2_rendered_double_punct": len(rendered_double),
        "issues": issues,
    }
    out = Path(args.workdir) / "agent" / "review_report.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in summary.items() if k != "issues"}, ensure_ascii=False, indent=2))
    print("detail ->", out)


if __name__ == "__main__":
    main()
