# 管线参考（skill 内副本）

> **provenance**：本文件是 `docs/agent-translate-pipeline.md` 的同步副本，供 skill
> 自包含阅读；阶段详解的改动请改主文档再同步。工具层契约与版面微调闭环见文末
> 「附录 A/B」，那两节为新内容（主文档未含）。
>
> 本文描述 `BabelDOC-agy-mvp` 分支上 **Markdown 视图 + 行内锚点** 的端到端翻译流程：
> 从 PDF 解析到 mono/dual 成品，逐步说明**功能、中间产物、schema 与效果**，
> 末尾给出面向「排版质量」的优化空间。
>
> 适用代码：`babeldoc/tools/agent/`（工具层）、`skills/document-translate/tools/`
> （agent 工具包）、`babeldoc/format/pdf/document_il/midend/`（解析中端）、
> `babeldoc/format/pdf/document_il/backend/pdf_creater.py`（重建）。

---

## 1. 总览

```text
输入 PDF
  │
  ├─[1] _prepare_pdf                PDF 修复（null xref / filter / mediabox；保留 Link 注释）
  │
  ├─[2] new parser → legacy IR      扁平字符流：page.pdf_character（只有字符+坐标+字体，无段落）
  │
  ├─[3] LayoutParser                版面识别（MinerU，唯一后端），page.page_layout
  │         → source/mineru/provider_ir.json（MinerU 结构树）
  │         → <workdir>/<pdf名>/layout_coverage.json（覆盖率门禁审计）
  │
  ├─[3b] InlineMathProtector        行内公式 span → formula 区域；→ source/mineru/alignment.json
  │
  ├─[4] EnclosedMarkerFixer         圈号修复：数字+矢量圆圈 → Unicode 圈号字符，删除装饰曲线
  │
  ├─[5] ParagraphFinder             聚类成行、成段；分配 layout_label 与确定性 debug_id
  │
  ├─[5b] TocDetector                目录页条目化（toc_entry / toc_entry_page）；→ source/toc.json
  │
  ├─[6] StylesAndFormulas           同样式 run 合并、公式对象识别
  │
  ├─[6b] 注释快照                     → source/bookmarks.json + source/links.json
  │
  ├─[7] ILTranslator.pre_translate_paragraph
  │         → document.md（连续英文 Markdown + 行内锚点 + 段落 id）
  │         → sheet.jsonl / anchors.json / state.pkl
  │
  ├─[8] 翻译（一次 agy 调用，整篇）  → translated.md + usage.json
  │
  ├─[9] md-apply                    校验（id / 锚点多重集+顺序 / 空 span / label）→ 确定性修复
  │         → translated.jsonl（canonical）→ 写回 IR（state.pkl）
  │
  ├─[10] reconstruct                Typesetting + PDFCreater → mono.pdf / dual.pdf
  │         （链接按源字符身份重映射 + URI 集合门禁；书签搬运到 dual）
  │
  └─[11] render                     代表性页 PNG（视觉审查）
```

> 阶段号与 [`docs/toolchain/pipeline-stages.md`](../../../docs/toolchain/pipeline-stages.md)
> 的「阶段 0–15」一一对应（本文号序为历史编号）。

设计目标：

1. **翻译模型看到一篇连续文档**（而非 40 行一批的 JSONL），术语与语气一次成型自洽；
2. **排版无损**：富文本片段与公式以行内锚点承载，重建时逐 span 还原字体/字号/颜色与公式图形；
3. **协议可校验、可确定性修复**：模型放错锚点也能在不二次调用模型的情况下修回。

---

## 2. 命令速查

在仓库根目录执行：

```bash
# ── Markdown 视图（推荐）────────────────────────────────────────
# 1) 解析 PDF → 连续英文 Markdown + 锚点
python -m babeldoc.tools.agent md-extract <pdf> --workdir <dir> \
    --layout mineru [--mineru-token <tok>] [--mineru-json <cached layout.json>] \
    [--pages 1,2] [--lang-in en --lang-out zh]

# 2) 一次调用翻译 + 写回 + 重建 + 渲染（编排器）
python experiments/markdown_translate.py <dir> \
    --model gemini-3.8-flash-low --effort low --output-dir <dir>/output \
    [--skip-translate] [--dry-run]

# 3) 单独执行写回 / 重建
python -m babeldoc.tools.agent md-apply <dir> <dir>/agent/translated.md
python -m babeldoc.tools.agent reconstruct <dir> --output-dir <dir>/output --dual
python -m babeldoc.tools.agent render <mono.pdf> --pages 1,5,8

# ── 旧 sheet 分批协议（保留兼容）────────────────────────────────
python -m babeldoc.tools.agent extract <pdf> --workdir <dir> --layout mineru
python experiments/batch_translate.py <dir> --model ... --batch-size 40
python -m babeldoc.tools.agent apply <dir> <dir>/agent/translated.jsonl
python -m babeldoc.tools.agent reconstruct <dir> --output-dir <dir>/output --dual

# ── 诊断：导出每一步中间产物 ────────────────────────────────────
python experiments/dump_parse_stages.py <pdf> --out-dir <dir>/parse-stages \
    --layout mineru --mineru-json <cached layout.json> --detail-page 1
```

关键配置：

| 配置 | 默认 | 说明 |
|---|---|---|
| `--layout mineru` | 唯一 | MinerU 云端版面识别；作者区/参考文献/图表内部/代码等自动跳过（本地 ONNX 后端 `native` 已移除） |
| `--mineru-json <path>` | — | 回放已缓存的 layout.json（不消耗 API） |
| `--mineru-cache-key <sha256>` | — | 按 PDF 内容哈希直接指定缓存 layout.json（`~/.cache/babeldoc/mineru-layout.v1/<key>.json`）；未命中明确报错 |
| `MINERU_API_TOKEN` | — | MinerU 云端 token；结果按 PDF 内容 sha256 缓存在 `~/.cache/babeldoc/mineru-layout.v1/` |
| `--layout-coverage-threshold` | `0.005` | 布局覆盖率门禁：未命中任何 layout 区域的原生字符占比上限，超阈值解析失败并落盘 `<workdir>/<pdf名>/layout_coverage.json`（解析中止，不产出 document.md） |
| `config.fix_enclosed_markers` | `True` | 圈号修复开关（`getattr` 读取，可程序化关闭） |

> **本轮新增产物**（板块 1–5）：`agent/source/mineru/{provider_ir,alignment}.json`
> （MinerU 结构树与字符对齐）、`agent/source/{toc,bookmarks,links}.json`
> （目录条目、书签、超链接快照）。字段与门禁详见
> [`docs/toolchain/`](../../../docs/toolchain/)（架构 / 阶段契约 / 工具 API /
> 标签字典 / 门禁 / 排查）。

---

## 3. 阶段详解

### [1] `_prepare_pdf` — PDF 修复

**功能**：复制输入 PDF，修复常见结构问题，捕获 mediabox。

- `open_pdf_with_save_fallback` / `save_pdf_with_same_path_fallback`
- `fix_null_page_content`、`fix_filter`、`fix_null_xref`、`fix_media_box`
- **`fix_null_xref` 只清除非 Link 注释**：历史实现会把所有 `/Annots` 置 null，
  导致超链接全失效；现在保留 `/Link` 注释（见 `_keep_link_annotations`）。

**产物**：`<dir>/input/input.pdf`（修复后的临时 PDF）

**schema**（`dump_parse_stages` 的 `01_prepare.json`）：

```json
{
  "temp_pdf_path": "<dir>/input/input.pdf",
  "temp_pdf_size": 1228672,
  "page_count": 20,
  "mediabox_data": { "1": {"MediaBox": "[0 0 612 792]"}, "...": {} }
}
```

**效果**：后续所有阶段都基于这个临时 PDF；原文件不被修改。

---

### [2] 解析 → legacy IR（扁平字符流）

**功能**：`parse_prepared_pdf_with_new_parser_to_legacy_ir()` 读取内容流，
产出 IL `Document`。

**关键事实**：此阶段**只有字符，没有段落/行**。一页的 `page.pdf_paragraph` 为空。

**产物 schema**（`02_raw_ir.json`）：

```json
{
  "per_page": [
    {"page": 1, "mediabox": [0,0,612,792], "n_chars": 5004,
     "n_lines": 0, "n_paragraphs": 0, "n_fonts": 5, "n_xobjects": 5}
  ],
  "first_chars": [
    {"text": "A",
     "box":         {"x": 96.58, "y": 719.70, "x2": 113.85, "y2": 743.61},
     "visual_bbox": {"x": 96.94, "y": 719.70, "x2": 113.46, "y2": 735.82},
     "font_id": "F114", "font_size": 23.91,
     "xobj_id": 0, "vertical": false, "formula_layout_id": null}
  ]
}
```

**效果**：得到 100% 覆盖的字符层（含坐标、字体、xobj），是后续一切结构化的原料。

---

### [3] `LayoutParser` — 版面识别

**功能**：调用 MinerU 布局模型，把识别框写入 `page.page_layout`；同时保留完整结构树、
执行覆盖率门禁。

- MinerU 适配器 `MinerUDocLayoutModel` 消费 bbox + block type 得到 layout 区域；
  **文字仍来自 PDF 原生文本层**（MinerU 的 `content` 只用于 token 决策与一致性校验，
  不覆盖原生字符）；
- 同时构建 **provider IR**（完整 block/line/span 树 + 阅读顺序）落盘
  `agent/source/mineru/provider_ir.json`；
- 字符聚类兜底 `fallback_line` **已删除**，替换为覆盖率门禁（未命中任何 layout 区域的
  原生字符占比 > 阈值即失败）；
- 图/表内部的标签用于跳过语义。

**产物 A：`agent/source/mineru/provider_ir.json`**

```json
{"version_name": "3.4.4", "backend": "hybrid", "page_count": 51,
 "pages": [{"page_index": 0,
   "blocks": [{"block_id": "p0-b0", "type": "title", "bbox": [217,97,379,114],
               "level": 1, "index": 1, "lines": [{"line_id": "p0-b0-l0",
                 "spans": [{"span_id": "p0-b0-l0-s0", "kind": "text",
                            "content": "…", "score": 1.0}]}],
               "children": [], "source": "para_blocks"}],
   "reading_order": ["p0-b0"]}],
 "unknown_types": []}
```

**产物 B：`<workdir>/<pdf名>/layout_coverage.json`**（覆盖率门禁审计，始终落盘）

```json
{"pages": [{"page_index": 0, "total_chars": 1354, "uncovered_chars": 0,
            "coverage": 1.0, "uncovered_text_preview": "…"}],
 "global": {"total_chars": 135415, "uncovered_chars": 59,
            "uncovered_ratio": 0.0004357, "coverage": 0.99956},
 "threshold": 0.005, "passed": true}
```

常见 `class_name`：`title / text / author / reference / figure / figure_caption /
table / table_text / table_caption / code / code_caption / header / footer /
page_number / page_footnote / aside_text / formula / isolate_formula /
toc_entry / toc_entry_page`。完整字典见
[`docs/toolchain/label-dictionary.md`](../../../docs/toolchain/label-dictionary.md)。

**效果**：为段落打语义标签，决定「译 / 不译」；保留 MinerU 结构供行内公式保护、
目录识别与超链接映射复用。

---

### [4] `EnclosedMarkerFixer` — 圈号修复

**功能**：把「数字字符 + 一圈矢量贝塞尔曲线」的圈号（① ② ③…）合并为单个
Unicode 圈号字符，并删除装饰曲线。否则译文重排后圆圈留在原坐标、文字移走，
形成漂浮小圆圈。

1. 识别：小尺寸（3–24pt）、长宽比 ≤1.6、仅描边、含贝塞尔曲线的闭合图形；
2. 找到完全落在图形内部的**单个**字母/数字；
3. 替换为 Unicode 圈号：`①-⑳` / `ⓐ-ⓩ` / `Ⓐ-Ⓩ`；
4. 从 `page.pdf_curve` 删除该图形；
5. 图/表内部的圆圈不动（`is_curve_in_figure_table_layout`）。

**连带修复**：

- `layout_helper.get_char_unicode_string` 的 NFKC 会把 `①` 拆成 `1` →
  新增 `_nfkc_preserving_enclosed`（私有区哨兵保护圈号）；
- `il_translator.parse_translate_output` 在「译文==原文」时复用原 composition
  （原 Latin 字体无 `①` 字形）→ 译文含圈号时强制走 unicode 路径，由 CJK 字体供字形。

**产物**：无独立文件（就地修改 IR）；`page.pdf_curve` 数量减少，字符 `char_unicode` 变化。

**效果**：实测 20 页论文 24 个圈号全部转为文本圈号，最终 PDF 文本层 24 个圈号字形正确。

---

### [5] `ParagraphFinder` — 段落识别

**功能**：把扁平字符聚类成行、行聚类成段；处理空格、行号交替、首行缩进等。

**产物 schema**（`04_paragraphs.json`）：

```json
{
  "paragraphs": [
    {"debug_id": "P01-005", "layout_id": 5, "layout_label": "text",
     "box": {"x": 48.98, "y": 353.01, "x2": 301.06, "y2": 602.93, "w": 252.08, "h": 249.92},
     "unicode": "Abstract—A function inlining optimization ...",
     "n_chars": 1592, "n_compositions": 25,
     "composition_types": ["line", "line", "..."],
     "vertical": false, "first_line_indent": false}
  ]
}
```

**确定性 id**：本分支把随机 base58 `debug_id` 改为
`P<页号(2位)>-<页内序号(3位)>`（如 `P01-005`），跨运行可复现。

**效果**：得到 181–208 个可翻译单元（含标题、正文、图注、表注）。

---

### [6] `StylesAndFormulas` — 样式与公式

**功能**：把段落拆成「同样式 run」，识别公式对象（含字形/曲线/图形），
并处理公式与字符的归属。

**产物 schema**（`05_styles_formulas.json`）：

```json
{
  "paragraphs": [
    {"debug_id": "P01-005", "layout_label": "text",
     "runs": [
       {"type": "same_style_characters",
        "box": {"x": 53.03, "y": 686.59, "x2": 567.06, "y2": 743.61},
        "style": {"font_id": "F114", "font_size": 23.91, "graphic_state": {...}},
        "text": "A Deep Dive into Function Inlining ..."},
       {"type": "formula", "box": {...}, "text": "=",
        "n_chars": 1, "n_curves": 0, "n_forms": 0}
     ]}
  ]
}
```

**效果**：`font_id/font_size/颜色` 的边界被固定下来，成为重建时的还原依据。

---

### [7] `ILTranslator.pre_translate_paragraph` — 生成翻译视图

**功能**：对每个段落生成带占位符的文本，并落盘三种产物。

占位符协议（canonical）：

| 语义 | canonical | Markdown 视图 |
|---|---|---|
| 富文本片段 | `<style id='1'>…</style>` | `[[S1]]…[[/S1]]` |
| 公式/符号图形 | `{v3}` | `[[F3]]` |

同时做**文本层修复**（只改空白/标点，不动锚点）：

- 缺空格：`eitherstatically → either statically`（基于 `advance` 的间距判定）；
- 断词连字符：`inlin- ing → inlining`、`high- dimensional → high-dimensional`
  （词典 + 前缀/非常规复合词规则）；
- 标点后缺空格：`graphs,which → graphs, which`、`structures.These → structures. These`。

**产物 A：`agent/document.md`**（连续英文 Markdown，交给翻译模型）

```markdown
<!-- babeldoc-markdown v1 -->
<!-- id=P01-001 label=title -->
# A Deep Dive into Function Inlining and its Security Implications for ML-based Binary Analysis

<!-- id=P01-005 label=text -->
[[S1]]Abstract[[/S1]]—A function inlining optimization is ... which we term [[S3]]extreme inlining[[/S3]]. ...

<!-- id=P01-006 label=title -->
## I. INTRODUCTION
```

结构约定：

| `layout_label` | Markdown 呈现 |
|---|---|
| `doc_title` / 首个 `title` | `# …` |
| 其它 `title` | `## …` |
| `paragraph_title` | `### …` |
| `figure_caption` | `*…*` |
| `table_caption` | `**…**` |
| 列表项（首可见字符是项目符号/圈号） | `- …` |
| `toc_entry` | 裸行（目录条目本身是短行） |
| `text` | 普通段落 |

> 结构前缀只给翻译模型看：回填时由 `_clean_markdown_body` 按 label 剥离，
> 不进入译文 IR（不会渲染成 `#`/`- ` 正文字符）。

**产物 B：`agent/sheet.jsonl`**（与旧协议兼容的清单，canonical 文本）

```json
{"id": "P01-005", "page": 0, "layout_label": "text",
 "source": "<style id='1'>Abstract</style>—A function inlining optimization ..."}
```

**产物 C：`agent/anchors.json`**（锚点明细，诊断用）

```json
{"rows": [
  {"id": "P01-005", "page": 0, "layout_label": "text",
   "canonical": "<style id='1'>Abstract</style>—A function ...",
   "markdown":  "[[S1]]Abstract[[/S1]]—A function ...",
   "anchors": [["", "S", "1"], ["", "S", "3"], ["", "F", "3"], ...]}
]}
```

**产物 D：`agent/state.pkl`**（IR 状态，`apply`/`reconstruct` 的唯一真源）

```python
{
  "doc": Document,            # IL：page → pdf_paragraph / page_layout / pdf_curve ...
  "inputs": { "P01-005": TranslateInput },   # id → {unicode, base_style, placeholders[]}
  "temp_pdf_path": ".../input/input.pdf",
  "pdf_path": "输入 PDF 路径",
  "lang_in": "en", "lang_out": "zh",
  "mediabox_data": {...}
}
```

`TranslateInput.placeholders[]` 元素：

```json
{"type": "rich_text", "id": 1,
 "left_placeholder": "<style id='1'>", "right_placeholder": "</style>",
 "left_regex_pattern": "<\\s*style\\s*id\\s*=\\s*'\\s*1\\s*'\\s*>",
 "right_regex_pattern": "<\\s*\\/\\s*style\\s*>",
 "composition_chars": "Abstract"}

{"type": "formula", "id": 3, "placeholder": "{v3}",
 "regex_pattern": "{\\s*v\\s*3\\s*}", "formula_chars": "="}
```

**跳过语义**（MinerU 模式默认跳过，保留原文渲染）：

`MINERU_DEFAULT_SKIP_TRANSLATE_LAYOUT_LABELS` = `reference / table / image /
code / header / footer / page_number / page_footnote / aside_text / author`，
经别名表展开为实际生效的标签：

`reference / table_text / table_footnote / figure / figure_text / code /
header / footer / page_number / page_footnote / aside_text / author`

即：作者区、参考文献、图内文字、表格内部、代码、页眉页脚页码全部保留原文；
**图注 / 表注（`figure_caption` / `table_caption` / `code_caption`）正常翻译**。
`--skip-labels` 可追加跳过标签。

**效果**：一份连续 Markdown + 完整锚点 + 可校验状态。实测 20 页论文：
208 段、881 个样式锚点、294 个公式锚点、100,891 字符。

---

### [8] 翻译（单次 agy 调用）

**功能**：`experiments/markdown_translate.py` 读取 `document.md`，套用
`skills/document-translate/prompts/markdown-translator.md`，**整篇一次调用**。

**产物 A：`agent/prompt.md`**（实际发出的提示词，便于复盘）

**产物 B：`agent/translated.md`**（模型输出，结构与输入一致，锚点原样保留）

**产物 C：`agent/usage.json`**（token 计量）

```json
{"model": "gemini-3.8-flash-low", "effort": "low",
 "input_tokens": 26178, "output_tokens": 19150, "thinking_tokens": 0,
 "cache_read_tokens": 8164, "total_tokens": 45328,
 "duration_seconds": 96.34, "num_turns": 1, "conversation_id": "...",
 "retry": {"...": "仅当发生漏行补译时出现"}}
```

**漏行补译**：模型偶尔会合并/漏掉段落。编排器在应用前用
`markdown_view.missing_ids()` 对比 `state.pkl` 的 id 集合与译文中的 `<!-- id -->`，
若缺失则**只对缺失段落再调用一次**（产物 `prompt.retry.md` / `translated.retry.md`），
并把结果追加到 `translated.md`。若补译仍失败，`md-apply` 回退
`target = source`（原文保留）并在报告里记 `fallback_ids`，保证流程不中断。

**效果**：一次调用即完成全篇；术语天然一致（实测「极限内联」59 次、
「极端/极致内联」0 次）。实测 16 页论文 96s、53,492 tokens（含缓存）；
21 页论文主调用 64,569 + 补译 16,048 tokens。

---

### [9] `md-apply` — 校验、修复、写回

**功能**：把译文 Markdown 切回逐段 canonical 文本，校验后写回 IR。

1. 按 `<!-- id=… -->` 切块，剥离 Markdown 装饰（`#`、`*`、`**`）；
2. 锚点回写：`[[S1]]→<style id='1'>`、`[[/S1]]→</style>`、`[[F3]]→{v3}`；
3. 校验：
   - id 完整性（缺/多即拒绝）；
   - **锚点顺序**必须与源文完全一致（旧协议只查多重集，会漏检跨 span 搬运）；
   - 空 span 记为 warning（模型省略无中文对应片段，如英文冠词）；
4. 确定性修复（不二次调用模型）：
   - `reorder`：锚点多重集一致、仅顺序错乱 → 保留模型切分位置，按源文顺序重贴；
   - `proportional`：锚点有增删 → 按源文各文本段长度占比等比投放；
5. 通过后调 `workflow.apply()`：占位符多重集校验 + 占位符后重复标点归一化 +
   `post_translate_paragraph` 重建 composition 写回 IR。

**产物 A：`agent/translated.jsonl`**（canonical 译文，apply 的输入）

```json
{"id": "P01-005", "target": "<style id='1'>摘要</style>——函数内联优化是..."}
```

**产物 B：`agent/apply_report.json`**

```json
{
  "ok": true,
  "applied": 208,
  "unknown_ids": [],
  "violations": [],
  "warnings": ["empty_style_span: id P02-004 style 3", "..."],
  "repaired": [{"id": "P05-012", "mode": "reorder"},
               {"id": "P06-015", "mode": "proportional"}],
  "punctuation_fixes": [{"id": "P01-010", "change": "{v4}: removed 1 duplicate punctuation"}],
  "markdown_sheet": ".../translated.jsonl"
}
```

**产物 C：`agent/il_translated.applied.json`**（写回后的 IR 快照，便于调试）

**效果**：实测 208/208 段写回、0 violation；13 段锚点错位被确定性修复。

---

### [10] `reconstruct` — 重排与 PDF 生成

**功能**：`Typesetting` 重新排版，`PDFCreater` 生成 mono / dual。

- Mono：在原临时 PDF 上替换文字内容 → `*.zh.mono.pdf`
- Dual：左右拼宽（原页 + 译页）→ `*.zh.dual.pdf`（宽度 2×）
- **超链接**：
  1. 保留原 Link 注释（`fix_null_xref` 不再清空 `/Annots`）；
  2. `_copy_page_links_to_dual` 把链接搬到 dual 左右两半（`show_pdf_page` 不复制注释）；
  3. **链接矩形按源字符身份重算**（`backend/link_remap.py`）：
     ① 字符并集（源字符对象仍存活于 composition）→ ② 段落 box + 段内相对投影 →
     ③ `unresolved`（保持原矩形，不删链接）。旧的按译文文字搜索（`_remap_links_by_text`）
     已删除；URI 集合不一致会硬阻断（`link_uri_set_mismatch`）。
- **目录（书签）**：`show_pdf_page` 同样不复制 outline。
  `_copy_toc_to_dual` 把原 PDF 的 `get_toc()` 写入 dual（左右拼宽模式页序一致，
  页码 1:1 映射）；交替页模式按 `2p-1` / `2p` 重映射页码。
  mono 因是原 PDF 的就地修改，目录天然保留；解析阶段已把书签快照到
  `source/bookmarks.json` 供对照。

**产物**：

| 文件 | 说明 |
|---|---|
| `output/<name>.zh.mono.pdf` | 中文单语 |
| `output/<name>.zh.dual.pdf` | 中英左右对照 |

**效果**（实测，`link_remap` 重写后）：

| 样本 | 源链接 | mono | dual |
|---|---:|---:|---:|
| `DeepSeek_V41_Tech_Report.pdf` | 410 | 410 | 820 |
| `2312.04432v2.pdf` | 323 | 323 | 646 |
| `ccs2026b-paper3764.pdf` | 319 | 319 | 638 |

DeepSeek 样本 mono 的链接映射分布：`total=410, remapped=408`
（字符并集 58 / 段落投影 350）、`unresolved=2`（code 图内无字符细线链接），
`uri_set_match=true`。定位与排查见
[`docs/toolchain/troubleshooting.md`](../../../docs/toolchain/troubleshooting.md#2-链接丢失--矩形漂移)。

---

### [11] `render` — 页面渲染

**功能**：PDF 指定页 → PNG（`--pages 1,5,8 --dpi 110`）。

**产物**：`output/render/page-XX.png`

**效果**：供视觉审查（标题字号、溢出、重叠、dual 对齐）。

---

## 4. 产物清单（`<workdir>/agent/`）

| 文件 | 阶段 | 作用 | 可手改 |
|---|---|---|---|
| `document.md` | 7 | 给模型的连续英文 Markdown | 否 |
| `sheet.jsonl` | 7 | canonical 段落清单（旧协议兼容） | 否 |
| `anchors.json` | 7 | 锚点明细（诊断） | 否 |
| `state.pkl` | 7 | IR 状态，apply/reconstruct 真源 | **否** |
| `prompt.md` | 8 | 实际发出的提示词 | — |
| `translated.md` | 8 | 模型译文 Markdown | **是**（改后可重跑 md-apply） |
| `usage.json` | 8 | token 用量（含 `retry`） | — |
| `prompt.retry.md` / `translated.retry.md` | 8 | 漏行补译的提示词与输出（仅缺失时生成） | — |
| `translated.jsonl` | 9 | canonical 译文（apply 输入） | 是 |
| `apply_report.json` | 9 | 校验/修复/告警报告 | — |
| `il_translated.applied.json` | 9 | 写回后的 IR 快照 | — |
| `layout_geometry.json` | 10 | 重排几何（框/缩放/警告） | 否 |
| `reconstruct_report.json` | 10 | 重建报告（含 `link_*` 指标） | — |
| `FINAL_REPORT.md` | 15 | 汇总报告 | — |

### 新增审计产物（板块 1–5）

| 文件 | 阶段 | 作用 |
|---|---|---|
| `source/mineru/provider_ir.json` | 3 | MinerU 完整 block/line/span 树 + `reading_order` + `unknown_types` |
| `source/mineru/alignment.json` | 3b | 原生字符 ↔ span 对齐统计（`inline_equation_matched` / `native_char_coverage`） |
| `source/toc.json` | 5b | 目录页判定与条目明细（`heading_text` / `printed_page_label` / `level`） |
| `source/bookmarks.json` | 6b | 书签快照（`level/title/page/to/nameddest`） |
| `source/links.json` | 6b | 超链接快照 + 覆盖的源字符下标/段落 id/段内相对比例 |
| `<workdir>/<pdf名>/layout_coverage.json` | 3 | 覆盖率门禁审计（逐页未覆盖字符 + 文本片段） |

> 完整 schema 与字段语义见
> [`docs/toolchain/pipeline-stages.md`](../../../docs/toolchain/pipeline-stages.md)
> 与 [`reference/schemas.md`](schemas.md)。

`<workdir>/input/` 保存修复后的临时 PDF；`<workdir>/output/` 保存成品。

### 诊断产物（`dump_parse_stages.py`）

| 文件 | 对应阶段 |
|---|---|
| `00_input.json` | 输入 PDF 元信息（页数、mediabox、sha256） |
| `01_prepare.json` | 修复后的临时 PDF + mediabox_data |
| `02_raw_ir.json` | 扁平字符流（每页字符数/字体/xobject、前 40 字符样例） |
| `03_layout.json` | 版面框（class_name/box/conf） |
| `04_paragraphs.json` | 段落（debug_id/layout_label/box/unicode） |
| `05_styles_formulas.json` | 同样式 run + 公式对象 |
| `06_sheet.jsonl` / `06_translate_inputs.json` | 与 extract 一致的清单 + 占位符明细 |

---

## 5. 优化空间（面向排版质量）

按「收益 / 成本」排序，供后续迭代选择。

### 5.1 高收益、低成本

| # | 问题 | 现状 | 建议 |
|---|---|---|---|
| 1 | **锚点错位仍需修复** | 208 段中 13 段被 `reorder/proportional` 修复，后者样式边界是近似值 | 缩短锚点 token（如 `[[S1]]` 已较短）、把公式密集长段单独成块、在提示词里给出「锚点必须原位」的对照示例；或在 apply 阶段用「原文 span 内字符 → 译文 span 内字符」的对齐模型替代比例投放 |
| 2 | **空 span（模型省略内容）** | 10 处 warning，如英文冠词 `The` 无中文对应 | 允许空 span 合法化（已不阻断），但在审查阶段标记「原文有内容而译文为空」的片段，提示模型补译或改写 |
| 3 | **表格内部未翻译** | `table_text` 跳过、保留英文 | 增加 `table` 模式：解析表格单元格，按行翻译并回填（需处理列宽/换行） |
| 4 | **图内文字未翻译** | `figure` 跳过（多为位图） | 对矢量图内文字做 OCR/提取并翻译；位图图内文字需 OCR（成本高，可作可选） |
| 5 | **Abstract 不被识别为标题** | 摘要段 `layout_label=text`，Markdown 里没有 `## Abstract` | 增加「首页标题与摘要之间的加粗/字号 run」启发式，提升为标题，改善结构可读性 |

### 5.2 中收益、中成本

| # | 问题 | 建议 |
|---|---|---|
| 6 | **其它装饰性矢量**（下划线/方框/高亮）同样会漂浮 | 复用 `EnclosedMarkerFixer` 框架，先做全库扫描统计分布，再决定「删除 / 跟随文本移动 / 转语义」。下划线可映射为 `<u>`、高亮可映射为背景色属性 |
| 7 | **链接矩形未 100% 对齐** | 用 IL 段落「原 box → 新 box」的仿射映射重定位链接，而不是文字搜索；需要 Typesetting 暴露段落新旧坐标映射 |
| 8 | **CJK 换行/标点挤压** | 引入 CJK 禁则处理（行首禁则、行尾禁则、标点挤压），避免行首出现 `，。` 或行尾孤字 |
| 9 | **文本层兼容表意文字** | 生成 PDF 文本层出现 `器`→U+FA30 等（既有字体 ToUnicode 问题），影响复制/检索；可在 `pdf_creater` 的 ToUnicode 生成阶段做兼容区映射 |
| 10 | **缺空格仍有残留** | `advance` 判定已修复大部分；对 `eitherstatically` 这类无间距信号的粘连，可加「词典 DP 分词 + 只在唯一切分且不产生技术词误切时切分」的保守后处理 |
| 11 | **长文档单次调用上限** | 16–20 页顺利；书稿级输出会触顶 | 按 `<!-- id -->` 分块续写，携带「已确定术语表」；仍用现有协议校验兜底 |

### 5.3 需要架构级改动

| # | 方向 | 说明 |
|---|---|---|
| 12 | **表格/公式的语义化重建** | 目前公式是「图形占位符」；若要把公式转 LaTeX/MathML 再重排，需要公式识别模型 + 数学排版引擎 |
| 13 | **版式风格迁移** | 双栏/单栏、栏宽、行距、段间距的自动估计与保持，需要版面重排器（现有 Typesetting 偏保守） |
| 14 | **阅读顺序与跨栏合并** | 当前段落顺序基本按栏内从上到下；跨栏长段落、脚注与正文交织时可能出现顺序错位，需要全局阅读顺序模型 |
| 15 | **多轮排版反馈闭环** | 用 render PNG + 视觉模型检测溢出/重叠，自动回退缩放或重排；目前审查是「发现→人工/程序化修复」 |

### 5.4 快速自检清单（发版前）

1. `apply_report.json`：`ok=true`、`violations=[]`；
2. `repaired` 数量（理想趋近 0）、`warnings` 数量（空 span）；
3. mono/dual 页数与原文一致；
4. 链接数：mono ≈ 原文、dual ≈ 2×原文；**目录条目数：mono/dual 与原文一致**；
5. 渲染首页、图注密集页、表格页、末页，检查标题字号、溢出、重叠；
6. 术语一致性抽样（同一英文术语在全文的中文译名是否唯一）。


---

## 附录 A：agent 工具层（`skills/document-translate/tools/`）

统一入口（`registry + dispatch + CLI`，JSON in / JSON out）：

```bash
PYTHONPATH=skills/document-translate/tools python -m babeldoc_tools list
PYTHONPATH=skills/document-translate/tools python -m babeldoc_tools call layout_lint \
    --workdir tmp/md-ccs3764 --arg min_sev='"P1"'
skills/document-translate/tools/bin/bdt call review_document --workdir tmp/md-ccs3764
```

| 组 | 工具 | 关键产物 |
|---|---|---|
| parse | `parse_document` | `document.md` / `anchors.json` / `sheet.jsonl` / `state.pkl` |
| translate | `translate_document` / `retranslate_ids` / `apply_translation` | `translated.md` / `translated.jsonl` / `apply_report.json` |
| review | `review_document` / `backtranslate_check` | `review_verdict.json` / `backtranslation_check.json` |
| layout | `reconstruct_pdf` / `render_pages` | mono+dual PDF / `layout_geometry.json` / `render/*.png` |
| layout | `layout_set` / `layout_lint` / `layout_locate` | `layout_overrides.json` / `layout_lint.json` |
| version | `snapshot` / `restore` / `list_snapshots` | `snapshots/<name>/`（小文件，不含 state.pkl） |
| report | `report` | `FINAL_REPORT.md` |

返回值统一为 `{"ok": true, "tool": ..., "data": {...}}` /
`{"ok": false, "tool": ..., "error": {"code", "message", ...}}`；CLI 退出码 0/1/2。
`registry.dispatch(name, args)` 可直接被 MCP wrapper 复用。

## 附录 B：排版微调闭环（layout overrides）

```text
review_document（结构 gate: pass/needs_fix）
   ├─ blockers（漏行/锚点/占位符/空译/注释残留）
   │     → retranslate_ids(feedback) → apply_translation → 重新 review
   ├─ 高风险段 → backtranslate_check（回译 + Levenshtein）→ 仍不过 → retranslate_ids
   └─ pass → reconstruct_pdf（应用 layout_overrides.json，dump layout_geometry.json）
              → render_pages → 并行审查（protocol / fidelity / layout）
              → findings + layout-fixer 决策 → snapshot → layout_set → reconstruct_pdf
              → layout_lint 复核（≤2 轮；每轮可 restore 回滚）
              → report → FINAL_REPORT.md
```

三条硬约束：

1. **无覆盖 = 零行为变化**：`layout_overrides.json` 缺失/为空时，Typesetting 与既有
   路径完全一致（回归用三篇论文的文本层哈希比对守住）。
2. **覆盖不写 `state.pkl`**：`reconstruct` 只在内存中应用覆盖，因此删 key / 删文件 /
   `restore` 都能回滚。
3. **视觉结论必须回到数据**：lint 结论来自 IR 几何 dump（精确 id），PNG 只用于定位；
   P2（兼容表意文字 / 链接错位）只记录不阻断。
