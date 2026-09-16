# 各阶段契约

本文给出每个阶段的**输入 artifact / 输出 artifact / 关键字段 / 不变量 / 失败模式**。
所有路径相对 `<workdir>`；「落盘」指由该阶段写出的文件。

> 字段名以实际代码为准，示例取自 DeepSeek 样本的真实产物
> （`tmp/link-e2e2`，51 页 / 410 链接 / 54 书签）。

---

## 阶段 0：`_prepare_pdf`（PDF 修复）

- **入口**：`babeldoc/tools/agent/workflow.py::_prepare_pdf`（两条解析入口共用）
- **输入**：源 PDF
- **输出**：`<workdir>/input.pdf`（修复后副本，`state.pkl` 记为 `temp_pdf_path`）
- **关键动作**：`fix_null_page_content` / `fix_filter` / `fix_null_xref` /
  `fix_media_box`（返回 `mediabox_data` 供重建还原）/ `save_pdf_with_same_path_fallback`
- **不变量**：页数与源 PDF 一致；Link 注释与书签保留
- **失败模式**：`open_pdf_with_save_fallback` 抛错（源文件损坏）
- **debug 采集点**：`parse/page-frames` 快照 + `parse/page_frames` 事件（MediaBox/
  CropBox/旋转/裁页页号映射，`original_boxes` 保留归一化前盒子）；`parse/pdf_prepared`
  事件 + 归档 `input.pdf`（原始）/`prepared.pdf`（修复后副本）。

---

## 阶段 1：native parse（字符 IR）

- **入口**：`new_parser/native_parse.py::parse_prepared_pdf_with_new_parser_to_legacy_ir`
- **输入**：`input.pdf`
- **输出**：内存 IR `Document`（`docs.page[*].pdf_character`）
- **关键字段**：`PdfCharacter.{box, visual_bbox, pdf_style{font_id, font_size,
  graphic_state}, char_unicode, advance, xobj_id, vertical, formula_layout_id}`
- **不变量**：无段落结构（纯字符流）；字符顺序 = 内容流顺序
- **失败模式**：解析异常（畸形 PDF）
- **debug 采集点**：`parse/native-chars` 快照（逐页字符 `C<页>-<序>` + 字体表，统一转
  左上原点 `PDF_TOPLEFT`）+ `parse/native_chars` 事件（页数/字符数）。

---

## 阶段 2：`LayoutParser`（布局 + provider IR + 覆盖率门禁）

- **入口**：`midend/layout_parser.py::LayoutParser.process`
- **输入**：IR + `TranslationConfig.doc_layout_model`（`MinerUDocLayoutModel`）
- **输出（内存）**：`page.page_layout`（`PageLayout{id, box, conf, class_name}`）
- **落盘**
  | 路径 | 内容 | 写出者 |
  |---|---|---|
  | `agent/source/mineru/provider_ir.json` | MinerU 结构树 | `MinerUDocLayoutModel._persist_provider_document` |
  | `<workdir>/<pdf名>/layout_coverage.json` | 覆盖率审计 | `LayoutParser._write_coverage_report` |

- **`provider_ir.json` 关键字段**（`docvision/provider_ir.py`）

  ```jsonc
  {
    "version_name": "3.4.4", "backend": "hybrid", "page_count": 51,
    "pages": [{ "page_index": 0,
      "blocks": [{ "block_id": "p0-b0", "type": "title", "sub_type": null,
                   "bbox": [217,97,379,114], "level": 1, "index": 1, "angle": 0.0,
                   "lines": [{ "line_id": "p0-b0-l0", "bbox": [...],
                     "spans": [{ "span_id": "p0-b0-l0-s0", "kind": "text",
                                 "content": "...", "score": 1.0 }] }],
                   "children": [], "parent_block_id": null,
                   "merge_prev": null, "source": "para_blocks" }],
      "reading_order": ["p0-b0", ...] }],
    "unknown_types": []   // 未知 block/span 类型聚合（type/where/page_index/count）
  }
  ```

- **`layout_coverage.json` 关键字段**

  ```jsonc
  {
    "pages": [{ "page_index": 0, "total_chars": 1354, "uncovered_chars": 0,
                "coverage": 1.0, "uncovered_samples": [{"box": [...], "text": "…"}],
                "uncovered_text_preview": "…" }],
    "global": { "total_chars": 135415, "uncovered_chars": 59,
                "uncovered_ratio": 0.0004357, "coverage": 0.99956 },
    "threshold": 0.005, "passed": true
  }
  ```

  > `global.uncovered_ratio > threshold` ⇔ `passed == false`。
  > 无字符页（扫描件）`total_chars == 0`，`coverage` 记 1.0，不参与门禁。
  > 未覆盖字符的 bbox 与文本片段（每页前 200 字符，`uncovered_text_preview`）
  > 用于人工定位「漏译源头」。

- **不变量**：I1.1–I1.3（见 [`README.md`](README.md#2-数据流与分层)）
- **失败模式**
  - `layout_coverage_gate`（**硬**）：超阈值 → `RuntimeError`，消息含
    `uncovered=…/…`、阈值、`layout_coverage.json` 路径与两条修复建议。
    解析在此中止，**不产出** `document.md`/`anchors.json`。
  - MinerU 页覆盖不匹配：`MinerU page coverage mismatch: missing_pages=[…]`（回放错样本时）
  - 回放页数不匹配：`MinerU replay layout does not match input PDF page count`

> **审计产物始终落盘**（即使过门禁），因此 `layout_coverage.json` 可随时查看
> 「哪些字符没被任何布局区域覆盖」。

- **debug 采集点**：`parse/layout` 快照 + `parse/layout_parsed` 事件（区域计数与
  label 直方图，`error` 标注门禁失败）、`parse/layout_coverage` 事件、
  `parse/provider_artifacts` 事件；归档 `layout-coverage.json` / `provider-ir.json` /
  `provider-layout.json`。成功路径的 `layout` 快照在**阶段 3 行内公式保护之后**采集
  （含追加的保护区）；门禁抛错路径**同样先采集再抛**（证据不丢）。

---

## 阶段 3：`InlineMathProtector`（行内公式 + 字符对齐审计）

- **入口**：`midend/inline_math_protector.py::InlineMathProtector.process`
- **输入**：IR + `agent/source/mineru/provider_ir.json`
- **输出（内存）**：追加 `class_name="formula"` 的 `PageLayout`（覆盖
  MinerU `inline_equation` span 的 bbox，已转 IL 坐标）
- **落盘**：`agent/source/mineru/alignment.json`

  ```jsonc
  {
    "version": 1,
    "summary": { "page_count": 51, "total_chars": 135415, "matched_chars": 127751,
                 "unmatched_chars": 7664, "ambiguous_chars": 5475,
                 "native_char_coverage": 0.943404,
                 "inline_equation_total": 126, "inline_equation_matched": 103,
                 "span_text_mismatch": 24, "protected_inline_math": 104 },
    "pages": [{ "page_index": 0, "coverage": 1.0, "span_to_chars": {...},
                "inline_equation": [{ "span_id": "...", "kind": "inline_equation",
                                      "matched": true, "method": "char_bbox",
                                      "char_count": 3, "ambiguous": false }],
                "span_text_mismatch": 0, "span_text_samples": [] }],
    "protected_inline_math": [{ "page_index": 3, "layout_id": 12, "box": [...] }]
  }
  ```

- **机制**（为什么要「追加布局区域」而不是直接设 `formula_layout_id`）：
  `ParagraphFinder` 会**无条件重写** `char.formula_layout_id`
  （`is_character_in_formula_layout` 的结果）。因此唯一可行入口是让这些字符
  落进一个 `formula` 布局区域 → `ParagraphFinder` 自然赋值 →
  `StylesAndFormulas` 自然聚成 `PdfFormula` → `ILTranslator` 自然产出 `{vN}`。
  **零侵入**。
- **对齐规则**（`utils/provider_alignment.py`）：span 中心包含 → span 覆盖率 ≥0.6
  → line bbox 回退；多命中记 `ambiguous` 取覆盖率最高。
- **不变量**：I2.1（MinerU `content` 不覆盖原生字符）；无 `provider_ir.json` 时
  该 pass **静默跳过**（返回原 IR，不写 `alignment.json`）
- **失败模式**：软。构建/落盘/保护失败只 warning，不阻断解析。
  唯一例外：范围外的 `4a4192fb…`（LawBench）等样本可能出现
  `inline_equation` span 落在 `code_body` 内而无原生字符 → 计入 `unmatched`（正常）。

> **如何确认「公式没被翻译」**：看 `alignment.json.summary.inline_equation_matched`
> 与 `anchors.json` 里 `{vN}` 计数。公式区间会变成占位符，模型看不到数学记号。

- **debug 采集点**：`parse/inline_math` 事件（`summary` 摘要 + `protected` 计数；
  provider IR 缺失时记 `status=skipped`，不编造）+ 归档 `alignment.json`。

---

## 阶段 4：`EnclosedMarkerFixer`（圈号修复）

- **入口**：`midend/enclosed_marker_fixer.py`
- **输入/输出**：IR（原地改）
- **动作**：识别小尺寸、近似圆形、仅描边的矢量图形 + 其内单字符 →
  替换为 Unicode 圈号（①…⑳ / ⓐ…ⓩ / Ⓐ…Ⓩ），删除装饰曲线
- **不变量**：图/表内的圆圈不动（`is_curve_in_figure_table_layout`）
- **失败模式**：软（按 `config.fix_enclosed_markers` 开关，异常只 warning）
- **debug 采集点**：`parse/enclosed_marker` 事件（`EnclosedMarkerFixer.last_stats`；
  未启用记 `enabled=false`）。

---

## 阶段 5：`ParagraphFinder`（段落聚类）

- **入口**：`midend/paragraph_finder.py::ParagraphFinder.process`
- **输入/输出**：IR（`page.pdf_paragraph`）
- **关键字段**：`PdfParagraph.{box, pdf_style, pdf_paragraph_composition,
  unicode, debug_id, layout_label, layout_id, xobj_id}`
- **动作**：按 layout 区域分类字符 → 聚行 → 聚段 → 分配 `layout_label` 与
  `debug_id`；`char.formula_layout_id` 在本阶段赋值（I4.3）
- **不变量**：`page.pdf_character` 在本阶段后**只剩被跳过的字符**（I3.2 提醒）
- **失败模式**：异常向上抛（解析失败）
- **debug 采集点**：`parse/paragraphs` 快照（实体 `P<页>-<序>` + `in_layout` 关系，
  必须在 `_deterministic_ids` 之后采集）+ `parse/paragraphs_found` 事件。

---

## 阶段 6：`TocDetector`（目录条目化）

- **入口**：`midend/toc_detector.py::TocDetector.process`
- **输入**：IR（段落已聚好）
- **输出（内存）**：目录页的整段被替换为「一条目两段」：
  - `toc_entry`（**可翻译**）：标题文本，独立 id
  - `toc_entry_page`（**受保护**）：点引导线 + 印刷页码，原字符 passthrough
- **落盘**：`agent/source/toc.json`

  ```jsonc
  {
    "version": 1,
    "summary": { "toc_pages": 2, "entries": 54, "replaced_paragraphs": 2,
                 "low_confidence_pages": [], "warnings": [], "skipped_pages": [] },
    "pages": [{ "page_index": 1, "is_toc": true, "confidence": 0.94,
                "low_confidence": false, "candidate_lines": 31, "total_lines": 33,
                "title_heading": "Contents",
                "entries": [{ "entry_index": 1, "heading_text": "1 Introduction",
                              "printed_page_label": "4", "leader_kind": "spaces",
                              "indent_x0": 71.248, "level": 1 }] }]
  }
  ```

- **判定逻辑**
  - 页级：标题命中 `Contents` / `Table of Contents` / `目录`，
    **或** 条目行（行尾右对齐数字页码）占比 ≥ 0.6 且条数 ≥ 5
  - 条目行切分：尾部数字 run = 页码（右缘须贴页内文本右边界，容差 8pt）；
    向前吞 `.`/空格 = 引导线；剩余须含字母且长度 ≥ 3 = 标题
  - `level`：编号模式优先（`2.1` → 2）；无编号按缩进聚类
  - 置信度 = 已解析条目行 / 页内文本行；`< 0.6` → **不改结构**，记
    `toc_low_confidence`（软警告）
- **不变量**：I4.4（只替换「整段所有行都是条目」的段落）；条目总数/顺序/
  印刷页码/层级/缩进全部程序保持
- **失败模式**：软。识别/条目化异常只 warning；低置信度只记警告

> **验收口径**：DeepSeek 样本印刷目录 54 条（页 2×31 + 页 3×23，与书签数一致）。
> `anchors.json` 中 `layout_label == "toc_entry"` 的条数应等于 54。

- **debug 采集点**：`parse/toc` 事件（`summary` 摘要）+ 归档 `toc.json`。

---

## 阶段 7：`StylesAndFormulas`（样式与公式）

- **入口**：`midend/styles_and_formulas.py::StylesAndFormulas.process`
- **动作**：合并同样式 run；把公式字符（含 `formula_layout_id` 非空者）聚成
  `PdfFormula`；判断 `is_translatable_formula`（纯数字/逗号/空格 → 转回普通文本）
- **不变量**：`all(char.formula_layout_id …)` 的公式**不可翻译**
  （`is_translatable_formula` 直接返回 False）——这正是行内公式保护的落点
- **失败模式**：异常向上抛
- **debug 采集点**：`parse/styles_formulas` 事件（段落数与 `PdfFormula` 计数）。

---

## 阶段 8：注释快照（书签 + 链接）

- **入口**：`tools/agent/link_snapshot.py`（`snapshot_bookmarks` / `build_link_state`）
- **调用点**：`markdown_view._run_parse` 与 `workflow.extract`
  （**Typesetting 之前**：坐标还是源坐标）
- **落盘**
  | 路径 | 内容 |
  |---|---|
  | `agent/source/bookmarks.json` | 书签数组 `[{level, title, page, to, nameddest, collapse}]` |
  | `agent/source/links.json` | 页号(str) → 链接数组（见下） |

  ```jsonc
  // links.json["0"] 的一条：
  { "link_index": 0, "page_index": 0, "kind": "URI",
    "uri": "https://huggingface.co/deepseek-ai/DeepSeek-V4.1-Flash",
    "page": null, "to": null, "from": [206.05, 487.14, 495.71, 500.15],
    "char_indices": [1897, 1898, ...],        // 页内字符下标（与 state 里的对象同序）
    "paragraph_ids": ["P01-004"],             // 覆盖到的段落 id
    "src_rect_ratio": [0.298, 0.0, 0.933, 0.040] }  // 源矩形在源段内的相对位置
  ```

- **同时进 `state.pkl`**：`link_snapshot`（同上）+ `page_char_objects`
  （页号 → 字符对象列表，与 `char_indices` 同序，pickle 保持对象身份）
- **不变量**：I3.3（对象身份跨 pickle）；快照失败不阻断解析（返回空状态，
  重建时跳过重映射）
- **失败模式**：软。`link_snapshot` 异常 → warning + 空状态
- **debug 采集点**：`parse/links_snapshot` 事件（链接数/书签数 + 归档引用）+
  归档 `links.json` / `bookmarks.json`。

---

## 阶段 9：`ILTranslator.pre` + Markdown 渲染

- **入口**：`markdown_view._run_parse` → `ILTranslator.pre_translate_paragraph`
- **输入**：IR + `select_page_paragraphs`（翻译选择）
- **输出（内存）**：每段 canonical 源文（`<style id='N'>…</style>` / `{vN}`）
- **落盘**
  | 路径 | 内容 |
  |---|---|
  | `agent/document.md` | 整篇连续 Markdown（含文件头注释 + 段落标记 + 行内锚点） |
  | `agent/anchors.json` | `{rows: [{id, page, layout_label, canonical, markdown, anchors}], skipped: [{id, page, layout_label, source, reason}]}` |
  | `agent/sheet.jsonl` | 每行 `{id, page, layout_label, source}`（canonical） |
  | `agent/state.pkl` | IR + inputs + 注释快照（供 apply/reconstruct） |

- **Markdown 渲染规则**（`render_rows_markdown`）

  | label | 渲染 |
  |---|---|
  | `doc_title` / 首个 `title` | `# <正文>` |
  | 其余 `title` | `## <正文>` |
  | `paragraph_title` | `### <正文>` |
  | `figure_caption` | `*<正文>*` |
  | `table_caption` | `**<正文>**` |
  | 列表项（首可见字符是项目符号/圈号） | `- <正文>` |
  | `toc_entry` | 裸行（条目本身是短行，无前缀） |
  | 其余 | 裸行 |

- **不变量**：I5.1–I5.4
- **失败模式**：`text is None`（短文本 < `min_text_length=5`、纯占位符、
  vertical 文本、skip label）→ 该段不进翻译，进 `skipped_rows`
- **debug 采集点**：`parse/selection` 快照 + 事件（选中行、跳过行及
  `reason`、`label_counts`/`skipped_label_counts`）；`parse/source_geometry` 事件
  （LaTeX bbox 源行几何条数）；`parse/stage_finished` 事件 + 归档
  `document.md`/`anchors.json`/`sheet.jsonl`。

---

## 阶段 10：`bdt translate` / `translate_document`（整篇翻译）

- **入口**：`babeldoc_tools/translate.py`（CLI = `bdt translate`）
- **输入**：`agent/document.md` + 提示词 `agents/translator.md`
- **输出**：`agent/translated.md`
- **不变量**：**一次调用整篇**（I5.1）；被调命令必须原样保留段落标记与锚点
- **协议**：`--translator <command>`（或 `BDT_TRANSLATOR`）从 stdin 读提示词、
  stdout 出译文、退出码 0 表成功；模型/档位由该命令自管
- **失败模式**
  - `translator_missing`：未给 `--translator`/`BDT_TRANSLATOR` → 用
    `--markdown <文件>` 导入，或 `--prompt-only` 取提示词
  - `translator_failed` / `translator_timeout` / `translator_empty`：命令失败/超时/无输出
  - `document_missing`：未先 `bdt parse`
- **debug 采集点**：每次被调命令 `translate/calls/<call_id>`（prompt 输入、stdout、
  stderr、退出码与耗时）配 `call_started` / `call_finished` 事件；
  `translate/texts/<version>` 快照 + `text_version` 事件（phase =
  `imported`/`raw`/`before_merge`/`merged`，逐段 target 与匹配方式）；
  `translate/missing_ids` 事件；`stage_finished` 事件 + 归档
  `prompt*.md` / `translated*.md`。

---

## 阶段 11：`bdt apply`（`apply_translation` 校验与写回）

- **入口**：`babeldoc_tools.translate.apply_translation`（内部 `markdown_view.apply_markdown`）
- **输入**：`translated.md` + `state.pkl` + `anchors.json`
- **输出**
  | 路径 | 内容 |
  |---|---|
  | `agent/translated.jsonl` | `{id, target}`（canonical 译文）——重建的输入 |
  | `agent/il_translated.applied.json` | 写回后的 IR（诊断） |
  | `agent/apply_report.json` | 结构化报告（`bdt apply` 落盘） |

- **校验与修复顺序**（`apply_markdown`）
  1. 按 `<!-- id=… -->` 切块；`extra_ids`（多出的 id）→ **阻断**
  2. 逐段：漏行 → 回退原文（`fallback_ids`，不阻断，I5.3）
  3. 标记存在但正文为空 → `empty_translation` 警告 + 回退原文
  4. label 与 anchors 不一致 → `label_mismatch` 警告（不阻断）
  5. 锚点多重集不一致 → `repair_target` 确定性修复（proportional）
     后仍不一致 → `anchor_multiset_mismatch` → **阻断**；锚点顺序与源文不同不阻断
     （逐段记 `anchor_reordered` 警告，尊重模型语序）
  6. 空样式 span → `empty_style_span` 警告（不阻断）
  7. `workflow.apply`：占位符多重集校验（`protocol.check_placeholders`）+
     双标点归一 + 写回 composition
- **失败模式**：`violations` 非空 → `"ok": false`，CLI 退出码 1
- **debug 采集点**：`apply/<validation_id>` 快照（源文/解析结果/entries + 校验明细）+
  `apply/apply_validation` 事件；`apply/anchor_repair`（proportional 修复）、
  `apply/placeholder_validation`、`apply/canonical_writeback`、`apply/writeback_saved`
  事件；`stage_finished` 事件 + 归档 `translated.jsonl` /
  `il_translated.applied.json` / `translated.md` / `apply_report.json`。

---

## 阶段 12：`Typesetting`（重排）

- **入口**：`midend/typesetting.py::Typesetting.typesetting_document`
- **输入**：写回后的 IR + `layout_overrides.json`（可选）
- **输出**：`agent/layout_geometry.json`（每段 `rendered_box` / `scale` / 警告）
- **不变量**：I6.1（源字符 box 原地改写）、I6.2（无覆盖 = 零行为变化）
- **失败模式**：`layout_warnings` 记入 geometry（软）
- **debug 采集点**：`build/typesetting_geometry` 快照（完整几何，重排前还会先写
  `build/source_state` 快照作对照）+ 同名事件（页数/段数/是否有覆盖/缺
  `rendered_box` 的告警）。

---

## 阶段 13：`PDFCreater`（重建 mono/dual）

- **入口**：`backend/pdf_creater.py::PDFCreater.write`
- **输入**：IR + `input.pdf` + `mediabox_data` + `link_remap_state`（来自 `state.pkl`）
- **输出**：`<output_dir>/*.mono.pdf`、`*.dual.pdf`
- **处理顺序**
  1. 从源 PDF 打开 → 逐页重写内容流
  2. 链接按源字符身份重映射（三级回退，见下）
  3. **URI 集合门禁**（删页模式跳过）
  4. `subset_fonts`
  5. save（`garbage=1`；OCR workaround 时 `garbage=4`）
  6. dual（可选）：拼宽/交替页 + 链接搬运 + 书签搬运/页码重映射
- **链接三级回退**（`backend/link_remap.py::resolve_link_rect`）

  | 优先级 | 方法 | 条件 | 说明 |
  |---|---|---|---|
  | 1 | `char_union` | 链接覆盖的源字符对象仍存活于 composition（公式/富文本/跳过段 passthrough） | 精确：字符 box 并集 |
  | 2 | `paragraph` | 字符对象已不在 composition（所在段被翻译重排） | 段落 box + **段内相对投影**（`src_rect_ratio`） |
  | 3 | `unresolved` | 两者都不可用 | 保持原矩形，记入 `link_unresolved`（**不删链接**） |

- **返回指标**（`workflow.reconstruct` → `reconstruct_report.json`）
  `link_total` / `link_remapped` / `link_fallback_paragraph` /
  `link_unresolved[]` / `link_uri_set_match`
- **失败模式**
  - `link_uri_set_mismatch`（**硬**）：URI 集合不一致 → `RuntimeError`，消息含
    缺失/新增样例（各至多 5 条）
  - `link_unresolved`（软）：进 `reconstruct_report.json` 与 `FINAL_REPORT.md`
  - 单条链接 insert/delete 失败 → 该条计入 unresolved，不中断整篇

- **实测**（DeepSeek，identity 回填）：`total=410, remapped=408,
  fallback_paragraph=350, unresolved=2`（2 条为 code 图内无字符细线链接），
  `uri_set_match=true`。
- **debug 采集点**：`build/latex_summary` 事件（贴片/回滚摘要）；归档 `mono.pdf` /
  `dual.pdf` / `layout_geometry.json` / `latex_bbox_report.json` /
  `reconstruct_report.json`；失败路径额外采 `build/build_failed` 事件 +
  `artifact_bundle(phase=partial_output_pdfs)` 归档半成品 PDF（不归档残留的旧
  `reconstruct_report.json`，避免误报为本次输出）。

---

## 阶段 13b：LaTeX bbox 排版（默认开启，`--no-latex-bbox` / `enable_latex_bbox_layout=False` 可关）

以 `enable_latex_bbox_layout=True` 为默认：每个正文段在 MinerU bbox 内用 XeLaTeX 重排
两端对齐译文并贴回。**显式关闭时零行为变化**（不采集几何、不预选、不贴片、报告为空），
输出与旧渲染路径逐字节一致；缺 XeLaTeX/字体时也会自动回退到旧渲染路径。

- **入口**：`backend/latex_bbox/`（`source_geometry` / `fusion` / `renderer_batch` /
  `stamp_cache` / `overlay` / `capability`）
- **四个阶段**

  | 阶段 | 时机 | 说明 |
  |---|---|---|
  | 采集 | `extract` / `high_level` 中，`ILTranslator` **之前** | `capture_source_line_geometry` 读仍是源坐标的 composition（`pdf_line` 字符 box）→ `n_lines` / `first_line_dx` / `baseline_pitch` / `ascent_top` / `space_below_pt`，随 `state.pkl` 落盘；译文回填后这些信息就丢了（根因 5） |
  | `prepare` | `PDFCreater.write` 的**内容流生成之前** | 选段（正文标签 ∧ 源行数 ≥2 ∧ 已翻译）+ 融合 `{vN}` 占位符 + 批编译贴片；此时页面仍是源文，可量源行距/首行缩进、判断下方净空 |
  | 内容流 | 同上循环内 | `stamped_ids` 的段**字符不进内容流**（无双层文本靠构造保证），失败段照旧走默认路径 |
  | `stamp` | 内容流生成之后 | 按 rect 贴片；链接快照 → 贴片 → 硬校验链接多重集与 URI 集合，失败则重生成受影响页回滚 |

- **`{vN}` 分类**（顺序硬约束，任何不确定即降级）：`text` → `mineru`（alignment
  一一对应 + 原生字符一致性校验）→ `simple_math`（Unicode 数学转写）→
  `fragment`（从源 PDF 裁区域 `\includegraphics` 内嵌）。`(cid:N)` 占位串
  一律降级 `fragment`。
- **批编译**：`renderer_batch.BatchStampRenderer` 每轮把待定段拼一份 tex（每段一页，
  逐页显式 `\pdfpagewidth/\hsize/...`，不用 `\newgeometry`），段前后 `\message{@@S n@@}` /
  `@@E n@@` + `at lines X--Y` 归属 Overfull 与 `!` 错误；坏段内部回退单段渲染。
- **缓存**：`<workdir>/<pdf名>/latex_cache/`（key = 模板版本 + 字体签名 + 请求内容，
  `mkstemp` + `os.replace` 原子写）；二次回放命中后近乎零编译。
- **产物**：`<workdir>/<pdf名>/latex_bbox_report.json` —— 聚合统计 + 逐段
  `decisions[]`（`debug_id/page/label/reason/fuse_kinds/fuse_classes/expected_text/
  fill_before/fill_after/font_scale/lead/attempts/expanded_pt/n_lines_source/indent_pt`）。
- **失败模式**（都只影响该段，绝不阻断翻译）：`compile:<原因>`（TeX 错误/超时）、
  `text-mismatch`、`single-line`、`untranslated`、`formula-fusion-failed`、
  `fragment-source-missing`、`capability-unavailable`（缺 XeLaTeX/宏包/字体只 warning）。
- **验收**：见 `experiments/acceptance_latex.py` 与
  `docs/layout-hypothesis/ACCEPTANCE.md` 的「LaTeX bbox 排版验收」一节。
- **debug 采集点**：`build/latex_capability`（能力探测）、`latex_prepare`、
  `latex_candidates`（选段 + 候选）、`compile_requests`（进渲染器的原始请求，含
  去重别名）、`compile_reuse`（`kind=deduplicated`，`actual_compile_calls=0`）、
  `compile_fallback`（批编译坏段交回单段）、`compile_expand`（高度扩展重试）、
  `candidate_selected`（段内最终采用候选）、`candidate_evaluated` + 快照
  `build/candidates/<candidate_id>`（逐候选正文/TeX/PDF 证据）、`latex_stamp`
  （贴片与 `reverted` 回滚、链接多重集校验结果）事件；缓存生命周期事件
  `cache_bypass` / `cache_miss` / `cache_hit` / `cache_write`
  （`--debug-recompile` 走 `cache_bypass`）。

---

## 阶段 14：渲染（`bdt build --render`）

- **入口**：`bdt build --render 1,2`（内部 `babeldoc_tools.layout.render_pages` /
  `tools/agent/workflow.py::render`）
- **输入**：mono/dual PDF + 页号串（`2,3` 或 `1-3`）
- **输出**：`render/page-NN.png`（默认 dpi 110）
- **失败模式**：页号越界静默跳过（返回已渲染的列表）
- **debug 采集点**：无独立采集点；查看器直接对归档 PDF 走
  `/api/v1/runs/<run_id>/render/<page>.png?pdf=<artifact rel>&dpi=N`（`pdf` 限
  `artifacts/` 下的 `.pdf`，`dpi` 限 50–200，默认 110），结果缓存到
  `debug/render-cache/<run_id>/`。

---

## 阶段 15：`bdt report`（报告）

- **入口**：`babeldoc_tools/report.py`（CLI = `bdt report`）
- **输出**：`<workdir>/FINAL_REPORT.md`（或 `--output-dir`）
- **内容**：token 用量、apply 指标、review verdict、layout lint 前后对比、遗留项
  （供人工抽检）
- **debug 采集点**：`report/stage_started` / `stage_finished` / `stage_error` 事件 +
  归档 `FINAL_REPORT.md`。

---

## 附：产物一览

```text
<workdir>/
├── input.pdf                                  # 修复后 PDF（临时，重建依赖）
├── <pdf名>/layout_coverage.json               # 覆盖率审计（LayoutParser）
├── agent/
│   ├── document.md                            # 给模型的整篇 Markdown
│   ├── anchors.json                           # 段 id → canonical/markdown/锚点
│   ├── sheet.jsonl                            # 段清单（canonical）
│   ├── state.pkl                              # IR + inputs + 链接/字符对象
│   ├── translated.md                          # 模型译文 Markdown
│   ├── translated.jsonl                       # canonical 译文
│   ├── il_translated.applied.json             # 写回后 IR（诊断）
│   ├── apply_report.json                      # apply 报告（工具层）
│   ├── layout_geometry.json                   # 重排几何（框/缩放/警告）
│   ├── layout_overrides.json                  # 排版微调覆盖（可选，agent 写）
│   ├── layout_lint.json                       # 排版 lint（可选）
│   ├── reconstruct_report.json                # 重建报告（含链接指标）
│   ├── FINAL_REPORT.md                        # 汇总报告
│   ├── source/
│   │   ├── bookmarks.json                     # 书签快照
│   │   ├── links.json                         # 链接快照 + 字符/段落绑定
│   │   ├── toc.json                           # 目录条目审计
│   │   └── mineru/
│   │       ├── provider_ir.json               # MinerU 结构树
│   │       └── alignment.json                 # 字符↔span 对齐审计
│   └── render/page-NN.png                     # 渲染页
├── <pdf名>/latex_bbox_report.json             # LaTeX bbox 统计 + 逐段 decisions（默认开启时）
├── <pdf名>/latex_cache/                       # 批编译贴片缓存（默认开启时，可删）
└── output/*.mono.pdf / *.dual.pdf             # 交付产物
```

---

## 附：debug 归档结构

`--debug` / `bdt debug` 的证据落在 `<workdir>/debug/runs/<run_id>/`（每个 run 一个
版本化归档：追加式事件流 + 阶段快照 + 稳定证据副本；历史 run 不覆盖、不自动删除）：

```text
<workdir>/debug/
├── viewer.json            # {pid, port, token, started_at, heartbeat_at}（复用判据）
├── write.lock             # 写互斥（fcntl 独占；查看器只读，不拿锁）
├── bindings.json          # 旧目录回放的显式 PDF 绑定（可选）
├── render-cache/<run_id>/ # 查看器页渲染缓存
└── runs/<run_id>/
    ├── manifest.json      # 版本化运行清单（原子写）
    ├── events.jsonl       # 追加式事件流（每行一个 JSON 对象，含 seq）
    ├── snapshots/         # 按阶段与页面拆分的快照 JSON
    └── artifacts/         # 复制的稳定证据文件（PDF 副本、TeX、日志等）
```
