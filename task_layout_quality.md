# Task: Stabilize translated PDF layout quality

## Objective

Fix the most visible regressions in the legacy Markdown PDF reconstruction pipeline: bullet/list markers need a readable gap from following Chinese text; translated body text must not show dramatic paragraph-to-paragraph font shrinkage; section headings/titles must be horizontally centered in their original column/box; and available vertical whitespace should be used to reflow adjacent blocks before shrinking type.

## Context

Relevant code: `babeldoc/format/pdf/document_il/midend/typesetting.py`, `babeldoc/tools/agent/layout_overrides.py`, `babeldoc/tools/agent/layout_geometry.py`, and `babeldoc/tools/agent/workflow.py`. Existing uncommitted changes added force-break, scale caps, line-skip overrides, geometry dump, and font-size diagnostics. The current real outputs and geometry are under `/tmp/babeldoc-three-test.eH8NAn`; examples include `2026-f1872` RQ list paragraphs where scales descend to ~0.5–0.85 and a title that is left-biased, plus list bullets touching text.

## Deliverables

1. Inspect the existing Typesetting diff and preserve public APIs/tests. Implement deterministic bullet/list spacing (prefer a layout-unit gap or an explicit synthetic space; do not depend on the LLM adding an ASCII space).
2. Establish sane, auditable font-size constraints for translated prose and headings. Do not let the current global/mode scale collapse headings or short list items merely because a Chinese translation is longer. Prefer reflow/available whitespace and bounded scaling; retain original style hierarchy and avoid `resizebox`/`scalebox`.
3. Center title/section-title paragraphs within their source column/layout box. Use a dedicated alignment path based on measured rendered width; do not fake centering by changing source text. Keep body paragraphs left aligned.
4. Use neighboring paragraph geometry/column boundaries to consume safe vertical gaps and move lower blocks when a preceding translated block grows. Never move text into figures/tables/other columns or outside cropbox; record a warning/finding when impossible.
5. Extend `layout_geometry`/lint diagnostics if needed to expose alignment, scale, and overflow decisions. Add focused unit tests for bullet gap, title centering, and scale bounds/reflow using lightweight fake IR objects.
6. Ensure `workflow.reconstruct` applies the behavior for existing jobs without requiring a manual patch file. Existing explicit layout overrides remain supported.

## Constraints

- Do not call an LLM or retranslate PDFs.
- Do not modify font packages or body visual font family.
- Do not delete figures, tables, captions, or source objects.
- Use `apply_patch` for edits. Preserve unrelated changes in the dirty worktree.

## Validation

Run the focused layout tests plus the full test suite if practical. If feasible, reconstruct/render a small existing job or synthetic fixture and inspect geometry/PNG output; report commands and residual limitations.

## Report back

List files changed, exact layout invariants introduced, tests/results, and any assumptions. Do not delegate further work.
