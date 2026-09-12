# Experiment report — "compile MinerU/Markdown text with LaTeX into the bbox and overlay it back"

**Date:** 2026-09-12
**Status:** reproducible experiment complete; no product code modified
**Scope:** verify the hypothesis without touching the product pipeline

---

## 1. Hypothesis under test (restated)

> The translated PDF's text comes from BabelDOC parse + MinerU OCR cross-validation.
> MinerU OCR text contains inline LaTeX. Per `bbox` (position + class) the current
> rendering breaks lines badly. Proposal: feed that LaTeX-bearing text to a LaTeX
> compiler, have it lay the text out inside the `bbox`-sized region, then stamp that
> region onto the original PDF — would that fix the layout?

Two things have to be true for this to work:

- **H1 (content):** MinerU OCR LaTeX can be compiled by a real LaTeX engine.
- **H2 (fit):** a `bbox`-sized LaTeX page at the source font size actually contains the text.
- **H3 (mechanics):** the compiled region can be placed back onto the PDF with correct
  coordinates, without destroying searchable text.

This experiment tests H1–H3 against real BabelDOC job artifacts, and additionally asks
what the proposal is actually fed, given how the pipeline represents formulas.

---

## 2. Environment (probed, not assumed)

| Item | Value |
|---|---|
| TeX engine | `/Library/TeX/texbin/xelatex`, TeX Live 2023 |
| Packages | `xeCJK`, `geometry`, `standalone`, `preview`, `amsmath`, `ctex` all found via `kpsewhich` |
| CJK font used | `~/.cache/babeldoc/fonts/SourceHanSansCN-Regular.ttf` (BabelDOC's own bundled font) |
| Python | 3.12.2, PyMuPDF 1.27.1 |
| Missing | `reportlab` (not needed) |

A bare family name (`Source Han Sans CN`) fails in `fontspec` on this machine; the font
must be referenced **by path**. `UprightFont` must omit the extension when
`Extension=.ttf` is set — getting this wrong produces
`The font "SourceHanSansCN-Regular.ttf" cannot be found`.

---

## 3. Real inputs (no fabricated fixtures)

| Job | PDF | Pages | MinerU cache (replay) |
|---|---|---|---|
| `2026-f1872` | `/tmp/babeldoc-three-test.eH8NAn/2026-f1872/2026-f1872-paper/input.pdf` | 20 | `f15a0e00…json` |
| `2312-04432` | `/tmp/babeldoc-three-test.eH8NAn/2312-04432/2312.04432v2/input.pdf` | 16 | `773068fa…json` |
| (extra equation sample) | — | 21 | `ebdca8e7…json` |
| (extra equation sample) | — | 51 | `ba68e2e4…json` |

Per job the following already existed and were reused read-only:
`agent/layout_geometry.json` (bbox + font size + current line count + current scale),
`agent/translated.jsonl` (canonical translated text),
`agent/document.md`, `output/*.mono.pdf` (current pipeline output).

---

## 4. What was actually run

All scripts live in `docs/layout-hypothesis/scripts/`; all outputs in
`docs/layout-hypothesis/out/` and `docs/layout-hypothesis/work/`.

```bash
cd /Users/zhengcaiyi/Desktop/博0/杂项/Github小玩意/BabelDOC/ieeTranslater

python3 docs/layout-hypothesis/scripts/run_experiment.py        # H1,H2,H3 + fit sweep
python3 docs/layout-hypothesis/scripts/verify_coordinates.py    # coordinate round trip
python3 docs/layout-hypothesis/scripts/check_side_effects.py    # text-layer / fonts / search
python3 docs/layout-hypothesis/scripts/compare_line_quality.py  # LaTeX vs current line breaking
python3 docs/layout-hypothesis/scripts/page_pass.py             # whole-page overlay pass
python3 docs/layout-hypothesis/scripts/check_mineru_latex.py    # 300 real MinerU equations
```

| Script | Role |
|---|---|
| `latex_box.py` | helper: build `.tex`, run `xelatex`, measure ink/fit/fill, overlay via `show_pdf_page` |
| `run_experiment.py` | main driver (translated-text, MinerU-line, MinerU-block, fit decomposition, sweep, overlay) |
| `verify_coordinates.py` | proves IL y-up ↔ MuPDF y-down ↔ MinerU bbox agreement |
| `check_side_effects.py` | `none` / `draw` / `redact` overlay modes, font embedding, searchability |
| `compare_line_quality.py` | measures non-final-line fill in current output vs LaTeX output |
| `page_pass.py` | compiles + overlays every body paragraph of a whole page |
| `check_mineru_latex.py` | compiles every real MinerU equation span as-is |

Representative outputs:

- `out/experiment-results.json`, `out/page-pass.json`, `out/line-quality-comparison.json`,
  `out/side-effects.json`, `out/mineru-latex-compilability.json`, `out/coordcheck.json`
- `out/2026-f1872_page2_redact_pad1.0_overlay.pdf` (whole page, text replaced)
- `out/2312-04432_page2_redact_pad1.0_overlay.pdf`
- `out/crop_P02-001_*.png` (side-by-side crop), `out/*_stamp.png`, `out/*_overlay.png`

---

## 5. Results

### H1 — MinerU OCR LaTeX compiles as-is: **confirmed (300/300)**

Every real `inline_equation` / `interline_equation` span from four MinerU caches was
wrapped in a minimal `displaymath` document and compiled with `xelatex`:

| Cache | Compile as-is |
|---|---|
| `f15a0e00` (2026-f1872) | 4 / 4 |
| `773068fa` (2312-04432) | 55 / 55 |
| `ebdca8e7` (ccs2026b) | 117 / 117 |
| `ba68e2e4` (DeepSeek) | 124 / 124 |
| **Total** | **300 / 300 (100%)**, zero errors |

MinerU emits spaced tokens (`\mathbf { w } _ { \mathrm { p r e } }`), `\tag{1}`,
`\text{...}`, `\textstyle`, `\begin{array}` — all compiled without repair.

**But naive pass-through of a whole line still fails.** Pasting a MinerU line verbatim
(prose + LaTeX mixed, no `$…$`) gives `! Missing $ inserted.` for 3/3 sampled lines.
Wrapping `*_equation` spans in `$…$` and escaping plain spans fixes it: `math_wrapped`
compiled 3/3. So *"feed MinerU text to LaTeX"* requires a normalization step, not a raw copy.

### H1b — the decisive formula test: **confirmed**

The fairest test is a real MinerU paragraph block containing inline equations, compiled
into a page whose paper size equals that block's own bbox:

| Block | MinerU lines | inline equations | bbox | Result |
|---|---|---|---|---|
| block 9, page 1 of 2312-04432 | 12 | 7 | 257.0 × 145.0 pt | **compiled, fits, 11 lines** |
| block 11, page 1 of 2312-04432 | 2 | 2 | 256.0 × 25.0 pt | **compiled, fits, 2 lines** |

Rendered proof: `out/2312-04432_block9_p1_preview.png` — prose plus `$t$`,
`$i\,(i \in \{1,\dots,K\})$`, `$d_i$`, `$G_t$`, `$W_i^t$`, `$G_{t+1}$` all typeset
correctly inside the block box. This is the strongest positive result for the hypothesis.

### H2 — does it fit? **Mostly yes at the source size; 100% with bounded shrink**

Sweep over 30 real body paragraphs per job, evaluated at the paragraph's own
`src_font_size` (9.963 pt):

| Job | Sampled | Fits at source size | Rate |
|---|---|---|---|
| `2026-f1872` | 30 | 28 | **93.3%** |
| `2312-04432` | 30 | 29 | **96.7%** |

Residual failures were only two classes: 1 × `vertical-overflow`, 1–2 × `overfull-hbox`.

**Key discovery — apparent overflow was a xeCJK punctuation artifact, not a capacity limit.**
For `2026-f1872 / P02-001` (box 252.55 × 143.23 pt, source font 9.963 pt):

| Variant | Ink x1 | Page width | Fits |
|---|---|---|---|
| xeCJK default punctuation | **258.50** | 251.61 | **no** (ink 6.9 pt past the edge) |
| `\xeCJKsetup{PunctStyle=plain}` | **251.61** | 251.61 | **yes** |
| halfwidth / kaiming | 258.29 | 251.61 | no |

With `PunctStyle=plain` the paragraph fits at the **original 9.963 pt** with no shrink,
all 9 lines, non-final-line fill ≥ 0.996, and **no glyph loss** (230/230 characters
extractable, identical to source). This is what makes the proposal viable at all.

**Bounded shrink is the reliable fallback.** Applying the `_find_optimal_scale_and_layout`
idea (lower `\fontsize` in small steps until ink fits) reached **30/30 = 100%** for both jobs.
Note the mitigation is not monotonic per-paragraph: `pad1` or `pad1_sloppy` fix some
paragraphs and break others, so the bounded shrink — not a single fixed padding — is the
robust recipe.

Tight boxes (a title of height 10.07 pt) overflow by ~1.1 pt purely from glyph metrics;
**1 pt of vertical paper padding fixes it** (`pad=0` fails, `pad=1.0/1.5/2.0` all fit).

### H3 — coordinate round trip and overlay: **confirmed exactly**

For `2026-f1872 / P02-001` (`out/coordcheck.json`):

```
IL box (y-up)       : [49.009, 593.771, 301.561, 737.004]
IL box (as y-down)  : [49.009,  54.996, 301.561, 198.229]      page height 792.0
closest MinerU bbox : [ 45.000,  55.000, 303.000, 201.000]     (L1 distance 8.22 pt)
overlay rect (MuPDF): [49.009,  54.996, 301.561, 198.229]      exact match
```

- Stamp page size 251.61 × 142.70 pt for a requested box 252.55 × 143.23 pt (rounding only).
- Overlay cost **0.033–0.057 s** per paragraph (`show_pdf_page`, `garbage=4, deflate=True`).
- Overlaid Chinese is extractable inside the rect (990 chars) and `search_for()` finds it at
  the expected position: `[[49.0, 56.2, 123.7, 66.9]]`.
- Fonts in the stamp are embedded: `SourceHanSansCN-Regular` (Type0) + `LMRoman10-Regular`.
  All 7 page fonts remain embedded after overlay. Stamp is ~29 KB.

### Text layer: `draw` doubles text, `redact` truly replaces

This is a real correctness trap, measured on the same paragraph:

| Mode | chars in box clip | original English still in clip | translated in clip | searchable |
|---|---|---|---|---|
| `none` (overlay only) | 990 | **yes** | yes | yes |
| `draw` (white rectangle) | 990 | **yes** | yes | yes |
| `redact` (`apply_redactions`) | **242** | **no** | yes | yes |

`draw_rect` is a *visual* erase — the original glyphs stay in the text layer, so extraction
returns English **and** Chinese stacked. Only `apply_redactions()` genuinely replaces the
text-layer content (original clip 747 chars → 242 chars, no English left). Any real
implementation must use the redaction path, and must account for link/annotation remapping.

### Line-breaking quality vs the current pipeline

Measured non-final-line fill (1.0 = line reaches the box edge) on pages 2–5 of both jobs:

| Job | Current pipeline paragraphs | Current short non-final lines | Current min fill | LaTeX min fill |
|---|---|---|---|---|
| `2026-f1872` | 83 | 3 | **0.185** | **0.996** |
| `2312-04432` | 40 | 7 | **0.293** | **0.996** |

LaTeX produces justified lines: every non-final line fills ≥ 0.996. The current pipeline
shows genuine defects, e.g. `2026-f1872 / P03-011` with a fill sequence
`[… 0.923, 0.883, 0.956, 0.965, **0.185**, 0.860, 0.958, 1.0 …]` — a line ending at 18.5%
width while text continues. So the *premise* of the hypothesis (bad line breaking inside
bboxes) is real and measurable.

**Caveat:** on the 9 (resp. 11) paragraph IDs where current and LaTeX outputs are directly
paired, **neither** side had a short non-final line (0/9, 0/11). The observed defects fall
on paragraphs where the translated body contained `{vN}` formula placeholders or short
list items and therefore were excluded from the LaTeX path. The LaTeX advantage is
demonstrated across the aggregate, not on the exact same defective paragraph.

### Whole-page pass

Every body paragraph of a page compiled and overlaid:

| Variant | Paragraphs | Fit |
|---|---|---|
| `box_pad = 0.0` (redact and draw) | 16 | 14 / 16 |
| `box_pad = 1.0` (redact and draw) | 16 | **16 / 16** |

Cost: ~0.46–0.62 s `xelatex` per paragraph; a 5-paragraph page ≈ **2.3–2.5 s compile +
0.06 s compose**. A 20-page paper at ~5 paragraphs/page ≈ 4–5 minutes of compile time.
Full-page visual: `out/2312-04432_page2_redact_pad1.0_overlay.png` (clean, well-justified),
`out/2026-f1872_page3_redact_pad1.0_overlay.png`.

---

## 6. Verdict on the hypothesis

| Claim | Verdict | Evidence |
|---|---|---|
| MinerU OCR LaTeX is real, usable LaTeX | **Supported** | 300/300 spans compile |
| Text can be laid out into a bbox-sized region | **Supported with caveats** | 93–97% fit at source size; 100% with bounded shrink; `PunctStyle=plain` required |
| The compiled region can be stamped back correctly | **Supported** | exact coordinate round trip; 0.03–0.06 s/box |
| Searchability preserved | **Supported, but only with redaction** | `redact` mode; `draw` doubles the text layer |
| The current layout defect is real | **Supported** | non-final-line fill as low as 0.185 vs 0.996 |
| "Feed the translated text to LaTeX" as stated | **Not as stated** | translated text contains **no LaTeX**; see §7 |

**Overall:** the *mechanism* — LaTeX per bbox, compiled at the bbox's paper size, stamped
back — is **feasible and demonstrably works**, including on real MinerU blocks with inline
equations. The *framing* ("give the translated text to LaTeX") does not match how the
pipeline actually stores formulas.

---

## 7. The most important finding: what text would actually be fed?

This is the gap between the hypothesis and the codebase.

1. **Translated text contains no LaTeX.** `agent/translated.jsonl` for both jobs has zero
   `\` sequences. Formulas are replaced by BabelDOC placeholders `{vN}`:

   | Job | Translated paragraphs | With `{vN}` |
   |---|---|---|
   | `2026-f1872` | 1030 | **445 (43.2%)** |
   | `2312-04432` | 587 | **178 (30.3%)** |

   Example: `P01-002` → `…Hyungjoon Koo{v1}韩国成均馆大学…`.

2. **`layout_geometry.json` formula paragraphs are tiny glyph fragments.** Only 19/1690
   paragraphs report `n_formula_chars > 0`, and they are `fallback_line` boxes like
   `↑` / `↓` / `<` of size 3×6 pt — not the equation text.

3. **Formulas are currently drawn from the original PDF vector curves**, not re-typeset
   (`PdfFormula` / `formula_layout_id` in `typesetting.py`; `InlineMathProtector` turns
   MinerU `inline_equation` spans into protected `formula` layout regions).
   So the LaTeX-bearing text exists on the **source/OCR side**, and is deliberately *kept
   out* of the translation text.

**Consequence:** "feed MinerU OCR text (with LaTeX) to LaTeX and overlay" cannot be applied
to the *translated* output without a new substitution step that:
maps `{vN}` → the corresponding MinerU equation content, re-merges translated prose with
source formulas, and handles the ~30–43% of paragraphs that contain placeholders.
The experiment applied LaTeX to translated paragraphs that had **no** placeholders, which is
exactly why those are the cleanly-fitting ones.

---

## 8. Residual risks / open questions

1. **Formula fusion is unsolved.** The `{vN}` → MinerU-LaTeX mapping was not implemented or
   tested. It is the main missing piece for the proposal as literally stated.
2. **Sample size.** 2 jobs, pages 2–5, ~30 paragraphs/job for the sweep; 4 pages for the
   full-page pass. Not a whole-document measurement.
3. **Long unbreakable tokens** (emails such as `ahmad.sadeghi@trust.tu-darmstadt.de`,
   `3.00GHz、512GB`) defeat a fixed box; `\sloppy` helps some and hurts others, so only the
   bounded shrink reliably fits.
4. **Font mismatch.** LaTeX typesets with `SourceHanSansCN`; the product picks its own font.
   Visual parity with the current output is not guaranteed.
5. **Page-geometry coupling.** Only the paragraph box is used. Full correctness needs
   column-aware widths, spacing between blocks, and no drift into figures/tables.
6. **Not integrated with product invariants.** Link rectangles (`link_remap`), the URI hard
   gate, dual-PDF composition, and coverage gates were not touched or tested.
7. **Cost.** ~0.5 s/paragraph means minutes per paper, and requires a TeX toolchain in the
   runtime/CI environment. This is a batch/post-process shape, not a per-request one.
8. **"Bad line breaking" is approximated** by a `min_fill < 0.8` heuristic; a human
   visual review of the current-vs-LaTeX pairs was not performed at scale.
9. **`vertical-overflow` false positives.** The fit check uses a 0.5 pt tolerance against a
   tight glyph box; some reported overflows are ~1 pt metric artifacts, not real clipping.

---

## 9. Recommendation

- The mechanism is worth prototyping **as an optional post-process** on top of
  `PDFCreater` output (watermark-style), reusing the `show_pdf_page` overlay precedent.
- Before that, resolve the **content path**: decide whether the LaTeX input is
  (a) translated prose + re-injected MinerU formulas replacing `{vN}`, or
  (b) source-side text only. The experiment shows (a) is the only path that both fixes
  layout *and* preserves translation.
- Adopt the measured recipe: `PunctStyle=plain`, `XeTeXlinebreaklocale "zh"`, escaped prose,
  1 pt vertical padding, and a bounded font-size shrink as the final fallback.
- Use `apply_redactions()` (never `draw_rect`) so the text layer is replaced, not doubled.

---

## 10. Reproduction

```bash
cd /Users/zhengcaiyi/Desktop/博0/杂项/Github小玩意/BabelDOC/ieeTranslater
rm -rf docs/layout-hypothesis/out docs/layout-hypothesis/work
mkdir -p docs/layout-hypothesis/out docs/layout-hypothesis/work
for s in run_experiment verify_coordinates check_side_effects \
         compare_line_quality page_pass check_mineru_latex; do
  python3 docs/layout-hypothesis/scripts/$s.py
done
```

Requires: `xelatex` (TeX Live with `xeCJK`, `geometry`, `amsmath`), PyMuPDF, the two jobs
under `/tmp/babeldoc-three-test.eH8NAn/`, and the MinerU caches under
`~/.cache/babeldoc/mineru-layout.v1/`. No network, no MinerU API, no LLM, no product-code
mutation. Product pipeline files were never imported for mutation and remain unchanged.
