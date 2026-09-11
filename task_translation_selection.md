# Task: Audit and fix translation selection and protected regions

## Objective

Make the legacy Markdown translation pipeline send only translatable prose to the LLM. Author/affiliation bands, corresponding-author notes, page footers/footnotes, references, figure internals, and table internals must remain source text and must not appear as translation blocks. Keep captions translatable. The native layout fallback must use geometry/content heuristics when MinerU labels are unavailable.

## Context

The main code paths are `babeldoc/tools/agent/markdown_view.py` (`_run_parse`) and `babeldoc/tools/agent/workflow.py` (`extract`). They currently call `ILTranslator.pre_translate_paragraph` for every paragraph. `TranslationConfig` has MinerU skip aliases but only enables them for MinerU and the direct agent paths do not check them before creating rows. Native ONNX parsing labels many paragraphs as `fallback_line` or `plain text`; `page.page_layout` contains larger `figure`/`table` regions and paragraph boxes/layout ids.

## Deliverables

1. Add a small shared, tested selection helper (or equivalent) that answers whether a paragraph should be sent to translation, with a stable skip reason.
2. Apply it in both `_run_parse` and `workflow.extract` before `pre_translate_paragraph`.
3. Always honor explicit protected MinerU labels when present, while preserving figure/table captions as translatable.
4. For native/fallback layout, classify protected paragraphs by:
   - overlap/containment in page layout regions named figure/image/table/table_text/figure_text/reference/header/footer/page_number/page_footnote/author/aside_text/abandon;
   - first-page author/affiliation band between document title and abstract;
   - corresponding-author/footnote/copyright permission text;
   - references section from a REFERENCES/BIBLIOGRAPHY heading until the next appendix/open-science major heading.
   Do not skip ordinary body text merely because it is `plain text` or `fallback_line`.
5. Keep skipped paragraphs in IR and include them in an explicit `skipped`/reason report and anchors metadata, but omit them from `document.md` and translation rows. Existing apply/resume behavior must still work with only translatable ids in `state["inputs"]`.
6. Update translator prompt/skill text to state that the model receives only translatable blocks and must not translate protected content.

## Constraints

- Preserve current public APIs and existing tests.
- Do not call any LLM.
- Do not rewrite unrelated layout code.
- Captions (`figure_caption`, `table_caption`, `code_caption`) remain translatable.
- Use `apply_patch`; add focused unit tests for the helper and at least one markdown extraction/selection behavior test with lightweight fake objects.

## Validation

Run focused tests and the full existing test suite if practical. At minimum run `pytest -q tests/test_markdown_labels.py tests/test_agent_protocol.py` plus new tests.

## Additional audit points for this pass

- Inspect the current implementation rather than assuming this brief is still
  accurate.  It is already partially implemented in the shared worktree.
- Fix document-level reference tracking: once a REFERENCES/BIBLIOGRAPHY section
  starts, ordinary headings inside a reference entry must not accidentally end
  it.  Only an explicit appendix/open-science/artifact/supplementary boundary
  should resume translation.
- Normalize extra/explicit labels (spaces, slashes, case) consistently in both
  `markdown_view` and `workflow`.
- Preserve captions as translatable, but never send figure/table body text,
  author/affiliation bands, footer/footnote/legal text, URLs/DOIs, or reference
  entries to the model.  Keep skipped rows in IR/report metadata.
- Add or strengthen lightweight tests for the above edge cases and for Markdown
  extraction omitting skipped rows.  Do not call an LLM and do not change
  layout/typesetting files.
- Do not delegate further work.  Do not make a commit; the top-level
  orchestrator will inspect and commit the focused change.

## Focus for the delegated pass

Implement only the reference-state/label-normalization fixes and their tests.
Do not spend time on a broad repository redesign.  In particular, change
`select_paragraph` so a normal section heading (for example `1. METHOD`) does
not terminate a references section; only the explicit appendix/open-science/
artifact/supplementary boundary listed above may do so.  Make `extra_labels`
normalization match `_norm_label` and ensure callers pass normalized values.

## Report back

Summarize files changed, selection rules, tests, and any assumptions. Do not delegate further work.
