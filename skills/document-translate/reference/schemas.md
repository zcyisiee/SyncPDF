# 数据契约（JSON schema 速查）

工具层所有交换文件都在 `<workdir>/agent/`。本文件是**唯一契约表**：改字段先改这里，
再改 `babeldoc/tools/agent/`（IR 侧）与 `skills/document-translate/tools/`（工具侧）。

发布 API 的契约由 `babeldoc_core` / `babeldoc_tools` 提供：所有工具成功返回
`{ok, tool, job_id, data, warnings, artifacts}`，失败返回稳定的 `error.code`；状态真源为
`agent/manifest.json`，`state.pkl` 只作 IR 缓存，不能替代 manifest。

核心状态转换为：`created → parsed → translated → protocol_validated → reviewed →
reconstructed → rendered → accepted`；`blocked_protocol`、`blocked_translation`、
`blocked_layout` 和 `needs_human_review` 是显式阻断状态。

---

## 1. `sheet.jsonl` / `anchors.json`（解析产物）

`sheet.jsonl` 每行：

```json
{"id": "P01-005", "page": 0, "layout_label": "text", "source": "<style id='1'>…</style>{v3}…"}
```

- `id`：确定性段落 id，`P<页号 1-based>-<页内序号>`；**排版覆盖与重译都用它对齐**。
- `page`：0-based 页号（与 BabelDOC 内部一致）。
- `source`：canonical 形式（`<style id='N'>…</style>` / `{vN}`）。

`anchors.json`：

```json
{"rows": [{"id": "P01-005", "page": 0, "layout_label": "text",
           "canonical": "<style id='1'>A</style>",
           "markdown": "[[S1]]A[[/S1]]",
           "anchors": [["", "S", "1"], ["/", "S", "1"]]}]}
```

## 2. `translated.jsonl`（写回输入）

```json
{"id": "P01-005", "target": "<style id='1'>甲</style>"}
```

`target` 与 `source` 的锚点/占位符**多重集必须一致**（apply 程序化校验）。

## 2.5 新增审计产物（板块 1–5）

以下文件在解析阶段由各新 pass 写出，路径相对 `<workdir>/agent/`（`layout_coverage.json` 除外）。

### `source/mineru/provider_ir.json`（MinerU 结构树，阶段 3）

```json
{"version_name": "3.4.4", "backend": "hybrid", "page_count": 51,
 "pages": [{"page_index": 0, "reading_order": ["p0-b0"],
   "blocks": [{"block_id": "p0-b0", "type": "title", "sub_type": null,
               "bbox": [217, 97, 379, 114],   // 页面坐标（左上原点）
               "level": 1, "index": 1, "angle": 0.0,
               "lines": [{"line_id": "p0-b0-l0", "bbox": [...],
                 "spans": [{"span_id": "p0-b0-l0-s0", "kind": "text",
                            "content": "…", "score": 1.0, "page_index": 0}]}],
               "children": [], "parent_block_id": null, "merge_prev": null,
               "source": "para_blocks"}]}],
 "unknown_types": [{"type": "…", "where": "block|span", "page_index": 3, "count": 2}]}
```

- `kind` ∈ `text / inline_equation / interline_equation / image / table / chart`。
- `source` ∈ `para_blocks / discarded_blocks`；`discarded_blocks` 不进 `reading_order`。
- 坐标是 MinerU 页面坐标；消费者按页高转 IL 坐标（`y' = H - y`）。

### `source/mineru/alignment.json`（字符↔span 对齐，阶段 3b）

```json
{"version": 1,
 "summary": {"page_count": 51, "total_chars": 135415, "matched_chars": 127751,
             "unmatched_chars": 7664, "ambiguous_chars": 5475,
             "native_char_coverage": 0.943404,
             "inline_equation_total": 126, "inline_equation_matched": 103,
             "span_text_mismatch": 24, "protected_inline_math": 104},
 "pages": [{"page_index": 0, "coverage": 1.0, "span_to_chars": {"span_id": [下标]},
            "inline_equation": [{"span_id": "…", "kind": "inline_equation",
                                  "matched": true, "method": "char_bbox",
                                  "char_count": 3, "ambiguous": false}],
            "span_text_mismatch": 0, "span_text_samples": []}],
 "protected_inline_math": [{"page_index": 3, "layout_id": 12, "box": [...]}]}
```

- `inline_equation_matched` = 已对齐（→ 会被保护成 `{vN}`）的行内公式 span 数。
- `protected_inline_math` = 实际追加的 `formula` 布局区域数。
- 对齐规则：span 中心包含 → span 覆盖率 ≥0.6 → line bbox 回退。

### `source/toc.json`（目录条目，阶段 5b）

```json
{"version": 1,
 "summary": {"toc_pages": 2, "entries": 54, "replaced_paragraphs": 2,
             "low_confidence_pages": [], "warnings": [], "skipped_pages": []},
 "pages": [{"page_index": 1, "is_toc": true, "confidence": 0.9394,
            "low_confidence": false, "candidate_lines": 31, "total_lines": 33,
            "title_heading": "Contents",
            "entries": [{"entry_index": 1, "heading_text": "1 Introduction",
                          "printed_page_label": "4", "leader_kind": "spaces",
                          "indent_x0": 71.248, "level": 1}]}]}
```

- `leader_kind` ∈ `dots / spaces / none`；`level` 由编号模式（`2.1` → 2）或缩进聚类推断。
- `entry_index` 是**页内**序号；全局顺序按页序拼接。

### `source/bookmarks.json`（书签快照，阶段 6b）

```json
[{"level": 1, "title": "Introduction", "page": 4, "to": [70.866, 756.85],
  "nameddest": "section.1", "collapse": false}]
```

### `source/links.json`（超链接快照，阶段 6b）

```json
{"0": [{"link_index": 0, "page_index": 0, "kind": "URI",
        "uri": "https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash",
        "page": null, "to": null, "from": [206.05, 487.14, 495.71, 500.15],
        "char_indices": [1897, 1898, "…"],        // 页内字符下标
        "paragraph_ids": ["P01-004"],            // 覆盖到的段落 id
        "src_rect_ratio": [0.298, 0.0, 0.933, 0.040]}]}  // 源矩形在源段内的相对位置
```

- 键是页号（字符串，0-based）；`kind` ∈ `URI / GOTO / NAMED`。
- `char_indices` 与 `state.pkl` 里的 `page_char_objects[页]` 同序（对象身份保持）。
- `src_rect_ratio` 供段落回退时的「段内相对投影」用；为空时退化为整段 box。

### `<workdir>/<pdf名>/layout_coverage.json`（覆盖率门禁，阶段 3）

```json
{"pages": [{"page_index": 0, "total_chars": 1354, "uncovered_chars": 0,
            "coverage": 1.0, "uncovered_samples": [{"box": [...], "text": "…"}],
            "uncovered_text_preview": "…"}],
 "global": {"total_chars": 135415, "uncovered_chars": 59,
            "uncovered_ratio": 0.0004357, "coverage": 0.99956},
 "threshold": 0.005, "passed": true}
```

- `global.uncovered_ratio > threshold` ⇔ `passed == false` → 解析阶段抛 `layout_coverage_gate`。
- 无字符页 `total_chars == 0`，`coverage` 记 1.0（不参与门禁）。

### `reconstruct_report.json`（重建报告，阶段 13）

```json
{"mono_pdf": "…mono.pdf", "dual_pdf": "…dual.pdf",
 "layout_geometry": "…/layout_geometry.json",
 "layout_override_stats": {"font_scaled": 0, "box_changed": 0, "clamped": 0,
                          "unmatched_ids": [], "warnings": []},
 "layout_warnings": [],
 "link_total": 410, "link_remapped": 408, "link_fallback_paragraph": 350,
 "link_unresolved": [{"page": 15, "link_index": 0, "uri": null}],
 "link_uri_set_match": true,
 "stats": {"mono": {"pages": 51, "toc_entries": 54, "links": 410}}}
```

- `link_fallback_paragraph`：走「段落 box + 段内相对投影」的链接数。
- `link_uri_set_match == false` → 重建阶段硬失败（`link_uri_set_mismatch`）。

## 3. `apply_report.json`

```json
{"ok": true, "applied": 209, "unknown_ids": [], "violations": [],
 "punctuation_fixes": [{"id": "P03-002", "change": "{v1}: removed 1 duplicate punctuation"}],
 "markdown_sheet": "…/translated.jsonl",
 "repaired": [{"id": "P05-001", "mode": "reorder"}],
 "warnings": ["empty_style_span: id P02-010 style 2"],
 "fallback_ids": ["P11-020"]}
```

- `fallback_ids`：模型漏行、已回退原文渲染的段落（**不是** `target==source` 判定的）。
- `repaired[].mode`：`reorder`（锚点顺序修正）或 `proportional`（锚点增删后等比投放）。

## 4. `layout_overrides.json`（排版覆盖，唯一真源）

```json
{"version": 1,
 "paragraphs": {"P05-012": {"scale_cap": 0.92, "font_scale": 0.95, "line_skip": 1.35,
                            "box_scale": 1.05, "box": [44, 500, 300, 620],
                            "force_break_after_text": ["（1）"],
                            "force_break_after_offset": [23]}},
 "pages": {"5": {"font_scale": 0.98}},
 "history": [{"ts": "2026-09-10T12:00:00", "reason": "P05-012 溢出", "patch": {...}}]}
```

| key | 类型 | 范围 | 语义 |
|---|---|---|---|
| `scale_cap` | number | 0.1–5 | 缩放上限 `min(optimal_scale, cap)`（**只降不升**） |
| `font_scale` | number | 0.2–5 | 字号乘数（段落级 × 页级叠乘） |
| `line_skip` | number | 0.8–3 | 行距系数覆盖（默认 CJK 1.50 / 其它 1.3） |
| `box_scale` | number | 0.3–5 | 布局框等比扩缩（锚定左上角，右/下生长） |
| `box` | [x,y,x2,y2] | y2>y, x2>x | 显式布局框（PDF 坐标，y 向上；裁剪到 cropbox） |
| `force_break_after_text` | string[] | 非空 | 子串**最后一次**匹配后强制换行 |
| `force_break_after_offset` | int[] | ≥0 | 字符偏移处强制换行（新行从该偏移开始） |
| `pages.<n>.font_scale` | number | 0.2–5 | 页级字号乘数 |

字段写 `null` = 删除该字段；`layout_set {clear: true}` = 清空全部覆盖。

## 5. `layout_geometry.json`（重排后的 IR 几何）

```json
{"version": 1, "pages": 21,
 "overrides": {"paragraphs": {...}, "pages": {...}},
 "warnings": ["P05-012: force_break_after_text 未命中: '（1）'"],
 "ir_overrides": {"paragraphs_touched": 1, "font_scaled": 1, "box_changed": 0,
                  "clamped": 0, "unmatched_ids": [], "warnings": []},
 "page_info": [{"page": 1, "cropbox": [0,0,612,792],
                "layout_regions": [{"label": "figure", "box": [...]}]}],
 "paragraphs": [{"id": "P05-012", "overridable": true, "page": 5, "layout_label": "text",
   "src_box": [44,500,300,620], "layout_box": [44,498,301,620], "rendered_box": [...],
   "scale": 0.92, "optimal_scale": 0.92, "font_scale": 1.0,
   "src_font_size": 10.0, "mode_font_size": 9.2, "min_font_size": 9.2, "max_font_size": 9.2,
   "n_chars": 312, "n_formula_chars": 0, "n_lines": 12, "n_unicode": 315,
   "space_below_pt": 8.4, "text": "…前 60 字…"}]}
```

- `src_box`：Typesetting 的输入框（已含 box/box_scale 覆盖）。
- `rendered_box`：渲染字符框的并集（含视觉框修正）。
- `src_font_size`：**应用覆盖之前**的段级字号（字号塌缩判定的基准）。
- `mode_font_size`：渲染色号的众数（段内混排小字号/上下标不计入）。
- `n_lines`：按 em 框底边聚类出的行数（同行的上下标算一行）。
- `space_below_pt`：同列下方到最近障碍的净空（决策用：>20pt 可用 `box_scale` 放宽框）。
- `overridable=false`：跳过翻译的段落（`P05-S01234` 形式），**不可被覆盖命中**。

## 6. `layout_lint.json`（排版缺陷）

```json
{"findings": [
   {"code": "out_of_page", "sev": "P0", "id": "P05-012", "page": 5,
    "evidence": {"rendered_box": [...], "mediabox": [...], "overflow_pt": 3.2},
    "hint": "scale_cap 下调，或 box_scale 收窄，或 force_break 分行"},
   {"code": "paragraph_overlap", "sev": "P1", "ids": ["P05-012","P05-013"], "page": 5,
    "evidence": {"iou": 0.23, "containment": 0.4, "boxes": [[...],[...]]}},
   {"code": "figure_overlap", "sev": "P1", "id": "P08-002",
    "evidence": {"region_label": "figure", "overlap_ratio": 0.62}},
   {"code": "font_shrink", "sev": "P1", "id": "P01-001",
    "evidence": {"src_font_size": 12.0, "mode_font_size": 8.4, "ratio": 0.70,
                 "font_scale": 1.0, "optimal_scale": 0.7}},
   {"code": "text_layer_compat_ideograph", "sev": "P2", "page": 1,
    "evidence": {"count": 63, "samples": [{"compat": "了", "canonical": "了"}]}},
   {"code": "link_misaligned", "sev": "P2", "page": 5,
    "evidence": {"count": 1, "rects": [[x0,y0,x1,y1]]}},
   {"code": "force_break_unresolved", "sev": "P2", "page": null,
    "evidence": {"warning": "P05-012: force_break_after_text 未命中: '（1）'"}],
 "counts": {"font_shrink": 26}, "metrics": {...},
 "summary": {"total": 49, "by_severity": {"P1": 26, "P2": 23},
             "blocking": 0, "defects": 26, "info": 23, "pdf": "…mono.pdf",
             "overrides": {...}, "ir_overrides": {...}}}
```

阈值（`babeldoc/tools/agent/layout_geometry.py::THRESHOLDS`）：
越界 > 1pt；段落 IoU > 0.15 或单向包含 > 30%（且面积比 ≥ 0.2）；
字号 `mode_font_size / src_font_size` < 0.85；压图 > 30%（原文已在该区域则跳过）。

## 7. `review_verdict.json`

```json
{"verdict": "pass | needs_fix",
 "blockers": [{"code": "missing_ids", "ids": ["P11-020"]},
              {"code": "empty_target", "ids": ["P10-005"]},
              {"code": "markdown_comment_leak", "ids": ["P21-001"]},
              {"code": "placeholder_leftover", "pdf": "mono", "style_tags": 2},
              {"code": "page_count_mismatch", "expected": 21, "actual": 20},
              {"code": "toc_missing", "expected": 28, "actual": 0},
              {"code": "links_missing", "expected": 319, "actual": 12},
              {"code": "apply_failed", "violations": [...]}],
 "warnings": [{"code": "intra_paragraph_truncated", "sev": "P1", "id": "P07-012",
               "len_ratio": 0.183, "rel_ratio": 0.484,
               "source_tail": "…", "target_tail": "…"},
              {"code": "low_cjk", "sev": "P1", "id": "P01-009", "cjk_ratio": 0.0},
              {"code": "sentence_end_mismatch", "sev": "P1", "id": "P04-002"},
              {"code": "suspect_merge", "sev": "P2", "id": "P11-019",
               "missing_ids": ["P11-020"]},
              {"code": "fallback_to_source", "sev": "P1", "ids": ["P11-020"]},
              {"code": "formula_splice", "sev": "P2", "id": "P09-017", "count": 4,
               "samples": ["varying⟨公式⟩influences"]},
              {"code": "title_font_shrink", "sev": "P1", "items": [...]}],
 "metrics": {"rows": 209, "applied": 209, "repaired": 15, "fallback": 0,
             "median_len_ratio": 0.378,
             "pages_mono": 21, "toc_mono": 28, "links_mono": 319,
             "pages_dual": 21, "toc_dual": 28, "links_dual": 638,
             "placeholder_leftovers_mono": 0, "placeholder_leftovers_dual": 0,
             "p1_title_shrink": 0},
 "apply_report": {...}}
```

**确定性阈值**（`babeldoc/tools/agent/quality_checks.py::THRESHOLDS`）：

| 检查 | 规则 |
|---|---|
| `intra_paragraph_truncated` | `rel_ratio = len_ratio / 全文中位 len_ratio` < 0.50 且源文 ≥ 40 字符 |
| `low_cjk` | `cjk_ratio` < 0.15 且源文 ≥ 40 字符、锚点密度 < 0.30、非参考文献/版权行 |
| `sentence_end_mismatch` | 源文以 `[A-Za-z)]` + `.!?` 结束而译文不以 `。！？…` 结束 |
| `suspect_merge` | 存在缺失/空译段，且同页（±1）有段落 `rel_ratio > 1.60` |
| `formula_splice` | 源文里公式占位符把英文词切断（`[A-Za-z]{2,}\{vN\}[A-Za-z]{2,}`）：**解析层限制**，不要当翻译缺陷重译 |
| `empty_target` | 去掉锚点后目标为空而源文非空（blocker） |
| `markdown_comment_leak` | 译文残留 `<!--`（blocker；apply 会程序化剔除） |

## 7.5 `dump_text_layer` 产物（`output/text_layer/`）

`page-XX.txt`（可选 `page-XX.spans.txt` 带字号/坐标）：页首注释列出该页的
CJK 兼容表意文字（`兼容表意文字 N 种: 了->了, …`），正文为 pymupdf 抽取的文本层。
审查 agent 的视觉结论必须回到这里复核。

## 8. `backtranslation_check.json`

```json
{"ids": ["P07-012"], "threshold": 0.55,
 "per_id": [{"id": "P07-012", "similarity": 0.31, "verdict": "needs_retranslate",
             "source": "…", "backtranslation": "…"}],
 "needs_retranslate_ids": ["P07-012"], "usage": {...}, "prompt": "…"}
```

相似度 = 归一化（去锚点、去 CJK、小写、去标点）后的 Levenshtein ratio；< 0.55 → 需重译。

## 9. `usage.json` / `lint_history.json` / `snapshots/`

```json
// usage.json
{"translate": {"input_tokens": 39590, "output_tokens": 24979, "total_tokens": 64569,
               "duration_seconds": 188.6, "model": "…", "effort": "low"},
 "retry": {...}, "backtranslate": {...}}

// lint_history.json（report 用）
[{"ts": "2026-09-10T12:00:00", "summary": {...}, "counts": {...}}]

// snapshots/<name>/ ：translated.jsonl + translated.md + layout_overrides.json
//                    + apply_report.json + review_verdict.json + meta.json
```
