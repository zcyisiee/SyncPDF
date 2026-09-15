# Recon: LaTeX-per-bbox hypothesis — existing entry points, data structures, minimal experiment

Timebox: ~12 min, read-only on product code. Worktree is **clean** (`git status --porcelain` empty,
branch `main`). No product code was modified; only a throwaway probe in `/tmp/_lh_probe`.

Hypothesis under test (paraphrased): in the **generate** stage, take MinerU OCR text **including
inline LaTeX**, compile it with a LaTeX engine into the **bbox-sized region**, then **overlay** that
region onto the original PDF — fixing bad line breaking inside bboxes.

---

## 1. Existing entry points the hypothesis touches

| Stage (user's word) | Real entry point | File:line |
|---|---|---|
| parse | `markdown_view.extract_markdown` → `_run_parse` | `babeldoc/tools/agent/markdown_view.py:648`, `:349` |
| parse (CLI) | `bdt parse` | `babeldoc_tools/__main__.py`（parse 子命令） |
| parse (tool layer) | `parse_document` | `babeldoc_tools/parse.py:24` |
| parse→IR | `parse_prepared_pdf_with_new_parser_to_legacy_ir` | `babeldoc/format/pdf/new_parser/native_parse.py:39` |
| layout / MinerU OCR | `MinerUDocLayoutModel.handle_document` | `babeldoc/docvision/mineru_doclayout.py:430` |
| bbox regions | `LayoutParser.process` / `_write_coverage_report` | `babeldoc/format/pdf/document_il/midend/layout_parser.py:114`, `:278` |
| inline formula protect | `InlineMathProtector.process` (+`alignment.json`) | `babeldoc/format/pdf/document_il/midend/inline_math_protector.py:154`, `:217` |
| generate / typeset | `Typesetting.typesetting_document` / `render_page` / `_find_optimal_scale_and_layout` | `babeldoc/format/pdf/document_il/midend/typesetting.py:1245`, `:1264`, `:974` |
| generate / write PDF | `PDFCreater.write` (+`update_page_content_stream`) | `babeldoc/format/pdf/document_il/backend/pdf_creater.py:1662` |
| **generate (orchestrator)** | `workflow.reconstruct` | `babeldoc/tools/agent/workflow.py:468` |
| **existing overlay precedent** | `add_tiled_watermark` / `add_corner_watermark`（PyMuPDF `page.show_pdf_page(..., overlay=True)` 叠加内容流） | 已移除的 executor 死代码（`watermark_transform`），可用 git 历史回溯 |
| render (visual check) | `workflow.render` / `render_pages` tool | `babeldoc/tools/agent/workflow.py:545`, `.../babeldoc_tools/layout.py:84` |
| layout overrides (existing knob) | `layout_overrides` + `layout_geometry` | `babeldoc/tools/agent/layout_overrides.py:45-411`, `layout_geometry.py:150, 358, 607` |

Note: there is **no stage literally named `generate`**. In this repo "generate" = `reconstruct`
(Typesetting + `PDFCreater.write` + link remap + optional dual/watermark).

## 2. Key data structures

1. **MinerU raw `layout.json`** (the OCR truth source with LaTeX):
   `~/.cache/babeldoc/mineru-layout.v1/<sha256-of-source-PDF>.json`
   shape: `{pdf_info: [{page_idx, page_size:[w,h], para_blocks, discarded_blocks}], _version_name, _backend}`
   block: `{bbox, type, angle, index, level?, merge_prev?}`; line: `{bbox, spans}`;
   span: `{bbox, type: 'text'|'inline_equation'|'interline_equation', content, score}`.
   Span content is genuinely LaTeX-ish, e.g. `L _ { 2 } \mathrm { ~ - ~ } \mathrm { n o r m }`
   (note MinerU's spaced tokenization), `\mathbf { w } _ { \mathrm { p r e } }`.
   Loader / cache: `mineru_doclayout.py:394-402`; replay via env `BABELDOC_MINERU_LAYOUT_JSON` (`:439`).
2. **Provider IR** (normalized tree, persisted): `ProviderDocument/ProviderPage/ProviderBlock/ProviderLine/ProviderSpan`
   — `babeldoc/docvision/provider_ir.py:113, 146, 170, 239, 446`; written to
   `<workdir>/agent/source/mineru/provider_ir.json` (`mineru_doclayout.py:263`). Span `kind` is where
   `inline_equation` survives (`provider_ir.py:113`).
3. **Bbox region**: `il_version_1.PageLayout{box, id, conf, class_name}` — `il_version_1.py:314`.
   Coordinate invariant: MinerU y-down → IL y-up, `y' = H - y`, H from `page.cropbox`
   (`docs/toolchain/architecture.md` I1.2; impl `provider_alignment.py:65`).
4. **Paragraph bbox + rendered bbox**: `PdfParagraph{box, pdf_style, pdf_paragraph_composition, unicode, debug_id, layout_label, scale, optimal_scale}` (`il_version_1.py:1152`);
   `PdfCharacter{box, visual_bbox, pdf_style, char_unicode, advance, xobj_id, formula_layout_id}` (`:627`).
5. **Generatable geometry, already on disk for real jobs**: `<workdir>/agent/layout_geometry.json`
   — `{version, pages, overrides, page_info:[{page, cropbox, layout_regions:[{label,box}]}], paragraphs:[{id, page, layout_label, src_box, layout_box, rendered_box, scale, optimal_scale, font_scale, src_font_size, mode_font_size, n_lines, n_unicode, text, space_below_pt}]}` (`layout_geometry.py:150-265`). **This is the natural per-bbox feed for a LaTeX experiment** (real column boxes + real translated text + real font sizes).
6. **Translated text**: `<workdir>/agent/translated.jsonl` (`{id,target}`), canonical translations after `md-apply`.
   Real values verified: `P02-001` target is Chinese body prose; box `src_box=[49.009, 593.771, 301.561, 737.004]`
   for a 612×792 page (column width ≈252.6pt ≈ 9pt font, 9 lines).
7. **Overlay/IPC primitives**: `pymupdf.Document.show_pdf_page(rect, src_doc, pno, keep_proportion=False, overlay=True)`; text stays extractable (proved in §4).

## 3. Real inputs available (no fabrication needed)

Repaired PDFs + full job artifacts (parse → translate → reconstruct → render) already exist:

| Job | PDF | pages | matching MinerU cache (replay) |
|---|---|---|---|
| `2026-f1872` | `/tmp/babeldoc-three-test.eH8NAn/2026-f1872/2026-f1872-paper/input.pdf` | 20 | `~/.cache/babeldoc/mineru-layout.v1/f15a0e00eed725df8ed53c5da37250bb2a09ac1dd4c7710013d6b0b8d5ac062e.json` (20 p) |
| `2312-04432` | `/tmp/babeldoc-three-test.eH8NAn/2312-04432/2312.04432v2/input.pdf` | 16 | `773068fa427a…json` (16 p) — also `519f7090…json` (16 p, duplicate) |
| `ccs2026b` | `/tmp/babeldoc-three-test.eH8NAn/ccs2026b/ccs2026b-paper3764/input.pdf` | 21 | `ebdca8e7e579…json` (21 p) |
| (paper) | — | 51 | `ba68e2e40408…json` (51 p, DeepSeek) |
| (paper) | — | 38 | `4a4192fb88b8…json` (38 p, LawBench; **0 inline_equation**) |

Per job: `agent/{document.md, anchors.json, sheet.jsonl, translated.jsonl, layout_geometry.json, state.pkl, apply_report.json}`
and `output/*.mono.pdf`, `*.dual.pdf`, `output/render/page-*.png`.
**Gotcha (verified):** the cache key is the sha256 of the *source* PDF, but `*/input.pdf` is the
*repaired* copy — hashes do not match (e.g. `921769e4…` vs `f15a0e00…`). Replay must pass
`--mineru-json <cache file>`, not `--mineru-cache-key`.
**Gotcha 2:** replay enforces `len(layout_pages) == mupdf_doc.page_count` (`mineru_doclayout.py:445-452`);
counts above do match, so replay works offline without `MINERU_API_TOKEN`.

Inline-math density (counted from cache): `773068fa` 51+4 equations, `ebdca8e7` 110+7, `519f7090` 51+4,
`ba68e2e4` 103+21, `f15a0e00` 4, `4a4192fb` 0. Text spans also contain HTML-ish markup (`<sup>1</sup>`),
so "MinerU text → LaTeX" is not a pure copy — it needs normalization.

## 4. Environment / dependency probe (executed, all under `/tmp`)

- `which xelatex pdflatex lualatex latexmk` → `/Library/TeX/texbin/*` present (TeX Live 2023).
- `kpsewhich` → `standalone.cls`, `preview.sty`, `geometry.sty`, `xeCJK.sty`, `ctex.sty`, `amsmath.sty` all found.
- CJK fonts present: `Source Han Mono SC` (`~/Library/Fonts/SourceHanMono.ttc`), `Heiti SC`, plus
  BabelDOC's own bundled fonts in `~/.cache/babeldoc/fonts/`.
- Python: 3.12.2; `pymupdf` (1.26.7 per pyproject) and `fitz`, `PIL`, `matplotlib` import OK;
  `reportlab` **missing**. `babeldoc`, `babeldoc.docvision.provider_ir`, `babeldoc.tools.agent.workflow`,
  `babeldoc_tools` all import cleanly from repo root.
- `xelatex` compile of a CJK + inline-math `\documentclass{article}` + `geometry` doc:
  **rc=0, ~0.75–0.9 s**, output page 251.6×142.7 pt for a requested box 252.5×143.2 pt;
  text extractable (92 chars).
- **Negative evidence for the core hypothesis**: with `fontsize{9.963}{14.9}` fixed, the rendered text
  block measured `[-0.0, 1.2, 258.3, 42.7]` on a 251.6 pt-wide page → LaTeX produced an **overfull line
  and clipped/overflowed the box**. LaTeX does not shrink or reflow to fit an arbitrary box; the
  bbox → `geometry` paper size mapping alone does not guarantee fit (no auto `\resizebox` by default,
  and the repo constraint in `task_layout_quality.md` explicitly forbids `resizebox`/`scalebox`).
- **Pitfall**: `\documentclass{standalone}` with CJK produced page 570.9×11.2 pt and `get_text()` = `""`
  (preview-crop box, text not extractable). Use `article`/`minimal` + `geometry` + explicit paper size
  if searchable text matters.
- Overlay round-trip: `page.show_pdf_page(Rect(49.009, 792-737.004, 301.561, 792-593.771), src, 0, keep_proportion=False)`
  + `save(garbage=4)` → **0.06 s**; resulting PDF keeps 20 pages and the overlaid Chinese text
  **is extractable** (`"测试中文排版" in page.get_text()` → True). So "overlay keeps the PDF searchable"
  holds at least for the XObject path.

## 5. Suggested minimal experiment (offline, no API, no product-code change)

```bash
# 0. probe (already done, reproducible in <5s; write under docs/layout-hypothesis/)
which xelatex && kpsewhich xeCJK.sty geometry.sty && fc-list :lang=zh | head

# 1. real bbox + real translated text, no LLM, no MinerU API
python - <<'PY'
import json
g=json.load(open('/tmp/babeldoc-three-test.eH8NAn/2026-f1872/agent/layout_geometry.json'))
tr={json.loads(l)['id']:json.loads(l)['target'] for l in open('/tmp/babeldoc-three-test.eH8NAn/2026-f1872/agent/translated.jsonl')}
p=next(x for x in g['paragraphs'] if x['id']=='P02-001')
print(p['src_box'], p['src_font_size'], len(tr['P02-001']))
PY
# → [49.009, 593.771, 301.561, 737.004] 9.963 <n chars>

# 2. raw MinerU spans incl. LaTeX (replay, offline)
python - <<'PY'
import json
d=json.load(open('/Users/zhengcaiyi/.cache/babeldoc/mineru-layout.v1/f15a0e00eed725df8ed53c5da37250bb2a09ac1dd4c7710013d6b0b8d5ac062e.json'))
pg=d['pdf_info'][1]; print(pg['page_size'])
for b in pg['para_blocks']:
    for l in b.get('lines') or []:
        for s in l['spans']:
            if s['type']!='text': print(s['type'], s['bbox'], s['content'])
PY

# 3. emit one \geometry{paperwidth=<w>pt,paperheight=<h>pt} doc per bbox, compile, overlay
xelatex -interaction=nonstopmode -halt-on-error -output-directory docs/layout-hypothesis box.tex
# then pymupdf: page.show_pdf_page(rect_in_il_yup, out, 0, keep_proportion=False) → save → render PNG

# 4. visual + text-layer check (existing repo tools, read-only w.r.t. product code)
# bdt 无独立 render 子命令：直接调用 workflow.render(pdf, "2", dpi=150, out_dir=...)
# （bdt build --render 2 渲染的是构建产物 mono/dual PDF）
```

Time budget per bbox: ~0.8 s `xelatex` + ~0.06 s overlay. For a full page (≈45 paragraphs) that is
~40 s LaTeX-only, i.e. a whole-paper run is minutes — acceptable for an experiment, **not** for the
product path as-is.

## 6. Open questions / risks surfaced by this pass

1. **Fit is the crux.** Mining: LaTeX with a fixed paper size and font size neither shrinks nor
   reflows into an arbitrary bbox; the observed overfull hbox is the failure mode the hypothesis is
   trying to fix. Candidate mitigations to *experimentally* compare: keep source `font_size` and let
   LaTeX break lines normally (then crop/`\clip` to bbox), or a bounded loop that lowers `\fontsize`
   until `\the\pagetotal`/overfull count is 0 — mirrors `Typesetting._find_optimal_scale_and_layout`
   (`typesetting.py:974`). `resizebox`/`scalebox` are banned by the repo's layout task
   (`task_layout_quality.md` deliverable 2).
2. **MinerU LaTeX is not compilable as-is**: spaced tokens (`\mathrm { ~ - ~ }`), `<sup>` markup inside
   text spans, likely unbalanced `\left`/`\right`. Needs a normalization pass before `xelatex`.
3. **Box→paper mapping**: `src_box` is the *paragraph* box (contract: Typesetting rewrites char boxes in
   place, `docs/toolchain/architecture.md` I6.1). Using the paragraph box as `geometry` paper size
   changes the layout basis vs. the source column (BabelDOC's renderer uses column width + line rules,
   not LaTeX's paragraph model). Whether to use `src_box`, `layout_box` or a layout region box needs a
   decision; `layout_geometry.json.page_info[].layout_regions` gives figure/table/formula boxes only,
   not text-column boxes (that is a gap — column detection is not currently exposed as an artifact).
4. **Ordering / z-order**: overlay must be drawn after the original content; `show_pdf_page` on the
   *original* PDF page appends to the content stream, but `PDFCreater.write` rewrites page streams from
   IR. A LaTeX-overlay feature would either (a) post-process the `PDFCreater` output (watermark-style,
   cheapest, matches `watermark_transform.py` precedent) or (b) become a new render unit in
   `pdf_creater.py`. Path (a) keeps "no product code change" for the experiment.
5. **What gets replaced?** Overlaying LaTeX on top of the existing translated text would double-print.
   A usable experiment must either render on the *original* PDF (no translation) or white-out the
   target box first — both are decisions, not defaults.
6. **Dual PDF**: `reconstruct --dual` composes widths/alternating pages; a page-level overlay pass must
   run before dual composition or be aware of the transform.
7. **Fonts / metrics**: LaTeX CJK via `xeCJK` + `Source Han Mono SC` works, but it will not visually
   match the source paper font; the translated CJK font BabelDOC picks is its own bundling
   (`~/.cache/babeldoc/fonts/`). Font-size parity across the box is the visible-quality question.
8. **Searchability & links**: overlay preserves extractable text (measured), but a LaTeX-overlay path
   bypasses `link_remap.resolve_link_rect` (`.../backend/link_remap.py:145`) and the URI hard gate in
   `PDFCreater.write`; link rectangles would not follow the new text.
9. **Coverage/invariants**: any new path must not break I3.1 (native chars are the only write-back truth),
   I1.3 coverage gate (`layout_coverage_threshold=0.005`), and "no overrides = zero behaviour change"
   (documented in `skills/document-translate/reference/pipeline.md` appendix B).
10. **Unverifiable in this pass** (static analysis only): whether LaTeX output can be made to match
    BabelDOC's per-column line breaks for all sampled paragraphs; per-page total compile time for a
    full paper; whether `xelatex` is available in the eventual product/CI environment (only this
    machine was probed).

## Start here

`babeldoc/tools/agent/layout_geometry.py:150` (`build_geometry`) plus one real
`/tmp/babeldoc-three-test.eH8NAn/2026-f1872/agent/layout_geometry.json`: they jointly give the exact
bbox + translated text + font size needed to drive the first LaTeX-per-bbox experiment, and the same
file already supplies the lint/finding channel (`layout_geometry.py:358`) if the experiment is later
wired into product review.
