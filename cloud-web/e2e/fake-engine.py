"""e2e 用假 syncpdf-cli：事件形状照真实引擎，译文是给原文逐页盖上中文的快照。

- 结局由原文 PDF 的 subject 元数据决定：``fake:success`` / ``fake:partial`` / ``fake:fail``；
- ``FAKE_PAGE_DELAY``：每页耗时（秒，默认 1.2）；
- ``FAKE_HOLD``：该文件存在时，发完第一页就停住（给排队、取消留出稳定窗口）；
- ``FAKE_FAIL``：该文件存在时，不论元数据都按 ``fail`` 结局走（重跑失败回滚）。
"""

import json
import os
import sqlite3
import sys
import time
from pathlib import Path

import pymupdf

args = sys.argv[1:]


def arg(name):
    return args[args.index(name) + 1]


def emit(**event):
    print(json.dumps(event), flush=True)


if args[0] == "dual":
    src, tr = pymupdf.open(arg("--source")), pymupdf.open(arg("--translated"))
    out = pymupdf.open()
    for a, b in zip(src, tr, strict=True):
        w, h = a.rect.width, a.rect.height
        page = out.new_page(width=w * 2, height=h)
        page.show_pdf_page(pymupdf.Rect(0, 0, w, h), src, a.number)
        page.show_pdf_page(pymupdf.Rect(w, 0, w * 2, h), tr, b.number)
    out.save(arg("--output"))
    sys.exit(0)

source = pymupdf.open(arg("--input"))
mode = (source.metadata.get("subject") or "fake:success").removeprefix("fake:")
fail = os.environ.get("FAKE_FAIL")
if fail and Path(fail).exists():
    mode = "fail"
pages = source.page_count
output = arg("--output")
delay = float(os.environ.get("FAKE_PAGE_DELAY", "1.2"))
hold = os.environ.get("FAKE_HOLD")

# 块级译文缓存：重新编译（--cache-only）要求它存在且是合法 SQLite
cache_dir = Path(arg("--cache-dir"))
cache_dir.mkdir(parents=True, exist_ok=True)
with sqlite3.connect(cache_dir / "translate.db") as conn:
    conn.execute("CREATE TABLE IF NOT EXISTS translations(source_html TEXT, translated_html TEXT)")


def publish(done):
    doc = pymupdf.open(arg("--input"))
    for page in list(doc)[:done]:
        r = page.rect
        page.draw_rect(pymupdf.Rect(40, 60, r.width - 40, r.height * 0.55), color=None, fill=(1, 1, 1))
        page.insert_text((56, 110), f"第 {page.number + 1} 页译文", fontname="china-s", fontsize=26)
        page.insert_textbox(
            pymupdf.Rect(56, 140, r.width - 56, r.height * 0.55),
            "本页已由假引擎翻译：公式、排版、链接原样保留。" * 6,
            fontname="china-s",
            fontsize=13,
        )
    tmp = output + ".part"
    doc.save(tmp)
    Path(tmp).replace(output)


emit(type="run_started", pages=0)
for stage in ("preflight", "source_analysis", "layout_analysis", "paragraph_analysis"):
    emit(type="stage_started", stage=stage)
    time.sleep(delay / 2)
    emit(type="stage_finished", stage=stage, elapsed_ms=int(delay * 500))
box = {"x0": 56, "y0": 400, "x1": 540, "y1": 700}
for page in range(1, pages + 1):
    emit(type="paragraph", paragraph_id=f"P{page:02d}-001", page=page, status="pending",
         boxes=[box], coord_system="pdf_user", source_bbox=box)
emit(type="stage_started", stage="translating")
time.sleep(delay)
if mode == "fail":
    emit(type="error", fatal=True, code="translator_failed", message="fake translator failed")
    emit(type="run_finished", ok=False, elapsed_ms=1)
    print("fatal: fake translator failed", file=sys.stderr)
    sys.exit(1)
emit(type="stage_started", stage="typesetting")
for page in range(1, pages + 1):
    # partial：第 2 页有一段排不进去，保留原文（回退框）
    status = "fallback" if mode == "partial" and page == 2 else "typeset"
    emit(type="paragraph", paragraph_id=f"P{page:02d}-001", page=page, status=status,
         boxes=[box], coord_system="pdf_user", source_bbox=box)
    publish(page)
    emit(type="page_ready", page=page, preview_path=None, revision=page)
    while hold and Path(hold).exists():
        time.sleep(0.05)
    time.sleep(delay)
if mode == "partial":
    emit(type="issue", severity="warning", code="coverage_gap", paragraph_id=None, page=pages, message="")
for stage in ("validating", "publishing"):
    emit(type="stage_started", stage=stage)
    time.sleep(delay / 4)
emit(type="document_finished", output=output, stats={})
emit(type="run_finished", ok=mode == "success", elapsed_ms=int(delay * 1000 * pages))
sys.exit(0 if mode == "success" else 1)
