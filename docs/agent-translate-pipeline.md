# Agent 编排的 PDF 翻译流水线（历史背景，不在导航中）

> **历史记录**：本文是重构前的逐步详解，保留作为背景与设计动机的存档，
> **不在 mkdocs 导航中**，也不作为推荐命令来源。当前入口只有 `bdt`，
> 权威的阶段契约见 [`toolchain/pipeline-stages.md`](toolchain/pipeline-stages.md)，
> 门禁与数据结构见 [`toolchain/README.md`](toolchain/README.md)，编排流程见
> [`skills/document-translate/SKILL.md`](https://github.com/zcyisiee/ieeTranslater/blob/main/skills/document-translate/SKILL.md)。
>
> 本文描述 `BabelDOC-agy-mvp` 分支上 **Markdown 视图 + 行内锚点** 的端到端翻译流程：
> 从 PDF 解析到 mono/dual 成品，逐步说明**功能、中间产物、schema 与效果**，
> 末尾给出面向「排版质量」的优化空间。
>
> 适用代码：`babeldoc/tools/agent/`（工具层）、`babeldoc_tools/`（`bdt` 编排器）、
> `babeldoc/format/pdf/document_il/midend/`（解析中端）、
> `babeldoc/format/pdf/document_il/backend/pdf_creater.py`（重建）。

---

> **M4 更新（agent 工具层 + 稳定性检测 + 排版微调已落地；U2 起入口为 `bdt` 子命令）**：
> 本文描述的解析/写回/重建链路未变，但上层已封装为 agent 工具包（`babeldoc_tools/`，
> 仓库根，CLI = `bdt`），并新增：
> - 结构化审查 gate：`bdt check`（apply 报告 + 段内完整性 + 页数/目录/链接 +
>   占位符残留 + 标题字号 → `verdict: pass|needs_fix`）、回译校验
>   `review.backtranslate_check`（回译 + Levenshtein）；
> - 排版微调通道：`agent/layout_overrides.json`（`scale_cap` / `font_scale` / `line_skip` /
>   `box_scale` / `box` / `force_break_after_*`，页级 `font_scale`）+ `bdt layout-set` /
>   `layout.layout_lint` / `layout.layout_locate` / `layout_geometry.json`；
> - 回归硬约束：**无覆盖 = 零行为变化**（三篇论文重建的文本层哈希已比对一致）。
> 详见 `skills/document-translate/SKILL.md` 与 `reference/{schemas,troubleshooting}.md`；
> 附录 A/B（工具层契约与版面闭环）在 `skills/document-translate/reference/pipeline.md`。

---

## 1. 总览

```text
输入 PDF
  │
  ├─[1] _prepare_pdf                PDF 修复（null xref / filter / mediabox；保留 Link 注释）
  │
  ├─[2] new parser → legacy IR      扁平字符流：page.pdf_character（只有字符+坐标+字体，无段落）
  │
  ├─[3] LayoutParser                版面识别（MinerU 云端 / 本地 DocLayout），page.page_layout
  │
  ├─[4] EnclosedMarkerFixer         圈号修复：数字+矢量圆圈 → Unicode 圈号字符，删除装饰曲线
  │
  ├─[5] ParagraphFinder             聚类成行、成段；分配 layout_label 与确定性 debug_id
  │
  ├─[6] StylesAndFormulas           同样式 run 合并、公式对象识别
  │
  ├─[7] ILTranslator.pre_translate_paragraph
  │         → document.md（连续英文 Markdown + 行内锚点 + 段落 id）
  │         → sheet.jsonl / anchors.json / state.pkl
  │
  ├─[8] 翻译（一次用户指定的命令调用，整篇）→ translated.md
  │
  ├─[9] bdt apply                    校验（id / 锚点多重集+顺序 / 空 span）→ 确定性修复
  │         → translated.jsonl（canonical）→ 写回 IR（state.pkl）
  │
  ├─[10] reconstruct                Typesetting + PDFCreater → mono.pdf / dual.pdf
  │         （超链接保留、搬运到 dual、按译文重定位）
  │
  └─[11] render                     代表性页 PNG（视觉审查）
```

设计目标：

1. **翻译模型看到一篇连续文档**（而非 40 行一批的 JSONL），术语与语气一次成型自洽；
2. **排版无损**：富文本片段与公式以行内锚点承载，重建时逐 span 还原字体/字号/颜色与公式图形；
3. **协议可校验、可确定性修复**：模型放错锚点也能在不二次调用模型的情况下修回。

---

## 2. 命令速查

在仓库根目录执行：

```bash
# ── 端到端（唯一入口 bdt run）────────────────────────────────────
# 1) 解析 PDF → 连续英文 Markdown + 锚点
bdt parse <pdf> --workdir <dir> \
    --layout mineru [--mineru-token <tok>] [--mineru-json <cached layout.json>] \
    [--pages 1,2] [--lang-in en --lang-out zh]

# 2) 翻译 + 写回 + 重建 + 审查 + 报告（编排器）
bdt run <pdf> --workdir <dir> --mineru-json <cached layout.json> \
    --translator <cmd> --dual
#    --markdown self 表示直接用 <dir>/agent/document.md 自译（离线验证链路）；
#    任一阶段产物缺失或上游被改动时以 missing_artifact / stale_upstream 报错。

# 3) 单独执行写回 / 重建
bdt apply --workdir <dir> --markdown <dir>/agent/translated.md
bdt build --workdir <dir> --output-dir <dir>/output --dual --render 1,5,8

# ── 诊断：导出每一步中间产物 ────────────────────────────────────
python experiments/dump_parse_stages.py <pdf> --out-dir <dir>/parse-stages \
    --layout mineru --mineru-json <cached layout.json> --detail-page 1
```

关键配置：

| 配置 | 默认 | 说明 |
|---|---|---|
| `--layout mineru` | 唯一 | MinerU 云端版面识别；作者区/参考文献/图表内部/代码等自动跳过（本地 ONNX 后端 `native` 已移除） |
| `MINERU_API_TOKEN` | — | MinerU 云端 token；结果按 PDF 内容 sha256 缓存在 `~/.cache/babeldoc/mineru-layout.v1/` |
| `--layout-coverage-threshold` | `0.005` | 布局覆盖率门禁：未命中任何 layout 区域的原生字符占比上限，超阈值解析失败并落盘 `layout_coverage.json` |
| `config.fix_enclosed_markers` | `True` | 圈号修复开关（`getattr` 读取，可程序化关闭） |

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

**功能**：调用版面模型（MinerU / 本地 DocLayout），把识别框写入 `page.page_layout`。

- MinerU 适配器 `MinerUDocLayoutModel` **只消费 bbox + block type 标签**，
  文字仍来自 PDF 文本层；
- BabelDOC 自己再按字符聚类补 `fallback_line`（覆盖模型未覆盖的字符）；
- 图/表内部的标签用于跳过语义。

**产物 schema**（`03_layout.json`）：

```json
{
  "per_page": [{"page": 1,
    "counts": {"title": 2, "author": 3, "text": 7, "header": 1,
               "page_footnote": 2, "fallback_line": 109}}],
  "layouts": [
    {"id": 1, "class_name": "title", "conf": 1.0,
     "box": {"x": 48.0, "y": 685.0, "x2": 571.0, "y2": 742.0, "w": 523.0, "h": 57.0}}
  ]
}
```

常见 `class_name`：`title / text / author / reference / figure / figure_caption /
table / table_text / table_caption / code / code_caption / header / footer /
page_number / page_footnote / aside_text / formula / isolate_formula / fallback_line`。

**效果**：为段落打语义标签，决定「译 / 不译」；实测一页 124 个框中只有 15 个来自
MinerU，109 个是 `fallback_line`。

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
| 首个 `title` | `# …` |
| 其它 `title` | `## …` |
| `figure_caption` | `*…*` |
| `table_caption` | `**…**` |
| `text` / `fallback_line` | 普通段落 |

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
跳过标签集由 `translation_selection.py::PROTECTED_LABELS` 与
`TranslationConfig.MINERU_DEFAULT_SKIP_TRANSLATE_LAYOUT_LABELS` 决定（`bdt` 无单独的
`--skip-labels` 旗标；要改跳过集需改这两处配置）。

**效果**：一份连续 Markdown + 完整锚点 + 可校验状态。实测 20 页论文：
208 段、881 个样式锚点、294 个公式锚点、100,891 字符。

---

### [8] 翻译（一次用户指定的命令调用）

**功能**：`bdt translate`（`babeldoc_tools/translate.py`）读取 `document.md`，套用
`skills/document-translate/agents/translator.md` 提示词，**整篇一次调用**
（translator 为 stdin/stdout 子进程；`--markdown self` 则直接使用已有译文）。

**产物 A：`agent/prompt.md`**（实际发出的提示词，便于复盘）

**产物 B：`agent/translated.md`**（模型输出，结构与输入一致，锚点原样保留）

**漏行补译**：模型偶尔会合并/漏掉段落。编排器在应用前用
`markdown_view.missing_ids()` 对比 `state.pkl` 的 id 集合与译文中的 `<!-- id -->`，
若缺失则**只对缺失段落再调用一次**（产物 `prompt.retry.md` / `translated.retry.md`），
并把结果追加到 `translated.md`。若补译仍失败，`bdt apply` 回退
`target = source`（原文保留）并在报告里记 `fallback_ids`，保证流程不中断。

**效果**：一次调用即完成全篇；术语天然一致（实测「极限内联」59 次、
「极端/极致内联」0 次）。实测 16 页论文 96s、53,492 tokens（含缓存）；
21 页论文主调用 64,569 + 补译 16,048 tokens。

---

### [9] `bdt apply` — 校验、修复、写回

**功能**：把译文 Markdown 切回逐段 canonical 文本，校验后写回 IR。

1. 按 `<!-- id=… -->` 切块，剥离 Markdown 装饰（`#`、`*`、`**`）；
2. 锚点回写：`[[S1]]→<style id='1'>`、`[[/S1]]→</style>`、`[[F3]]→{v3}`；
3. 校验：
   - id 完整性（缺/多即拒绝）；
   - **锚点多重集**必须与源文一致；锚点顺序**不强制**——中英翻译调整锚点位置是
     合法语序（如"在 S 个子智能体间协调 T 轮"），强制回贴会颠倒语义。顺序与
     源文不同只记 `anchor_reordered: id X` 警告（供观察跨 span 搬运的真实发生率）；
   - 空 span 记为 warning（模型省略无中文对应片段，如英文冠词）；
4. 确定性修复（不二次调用模型）：
   - `accepted`：锚点多重集一致、仅顺序不同 → 接受模型语序，只修空 span；
   - `proportional`：锚点有增删/幻觉 → 按源文各文本段长度占比等比投放；
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
  "repaired": [{"id": "P05-012", "mode": "accepted"},
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
  3. `_remap_links_by_text` 按「链接覆盖的原文文字」在译文中搜索同名 token
     （`[94]`、URL 等），就近重定位矩形。
- **目录（书签）**：`show_pdf_page` 同样不复制 outline。
  `_copy_toc_to_dual` 把原 PDF 的 `get_toc()` 写入 dual（左右拼宽模式页序一致，
  页码 1:1 映射）；交替页模式按 `2p-1` / `2p` 重映射页码。
  mono 因是原 PDF 的就地修改，目录天然保留。

**产物**：

| 文件 | 说明 |
|---|---|
| `output/<name>.zh.mono.pdf` | 中文单语 |
| `output/<name>.zh.dual.pdf` | 中英左右对照 |

**效果**（实测）：

| PDF | 原文链接 | mono | dual |
|---|---:|---:|---:|
| `input.pdf` | 429 | 429 | 856 |
| `2312.04432v2.pdf` | 323 | 323 | 646 |
| `ccs2026b-paper3764.pdf` | 319 | 319 | 638 |

mono 中 77% 链接矩形按译文重定位。目录（书签）同样保持：
`input.pdf` 53 条、`ccs2026b-paper3764.pdf` 28 条，mono/dual 均一致。

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
| `translated.md` | 8 | 模型译文 Markdown | **是**（改后可重跑 bdt apply） |
| `prompt.retry.md` / `translated.retry.md` | 8 | 漏行补译的提示词与输出（仅缺失时生成） | — |
| `translated.jsonl` | 9 | canonical 译文（apply 输入） | 是 |
| `apply_report.json` | 9 | 校验/修复/告警报告 | — |
| `il_translated.applied.json` | 9 | 写回后的 IR 快照 | — |

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
| 1 | **锚点错位仍需人工观察** | 208 段中 13 段锚点顺序与源文不同：删除 reorder 后不再回贴（那是合法语序调整），只记 `anchor_reordered` 警告；仍被 `proportional` 修复的是多重集不一致（锚点增删）的段落，其样式边界是近似值 | 在审查阶段抽样 `anchor_reordered` 段落，区分「合法语序调整」与「跨 span 搬运」；对后者用提示词给出「锚点必须原位」的对照示例，或在 apply 阶段用「原文 span 内字符 → 译文 span 内字符」的对齐模型替代比例投放 |
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
2. `repaired` 数量（理想趋近 0，只应有 proportional 兜底）、`warnings` 里的
   `anchor_reordered` 数量（合法语序调整，抽样确认非跨 span 搬运）；
3. mono/dual 页数与原文一致；
4. 链接数：mono ≈ 原文、dual ≈ 2×原文；**目录条目数：mono/dual 与原文一致**；
5. 渲染首页、图注密集页、表格页、末页，检查标题字号、溢出、重叠；
6. 术语一致性抽样（同一英文术语在全文的中文译名是否唯一）。
