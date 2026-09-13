# 架构：分层与不变量

本文说明工具链的**分层结构**、每层的职责边界与「不变量」（invariant）。
改动任何一层时，先确认本文的不变量是否仍然成立。

---

## 1. 分层图

```text
┌──────────────────────────────────────────────────────────────────────────┐
│ L7 交付层    mono.pdf / dual.pdf / render/*.png / FINAL_REPORT.md        │
│              PDFCreater（backend/pdf_creater.py）+ 链接重映射             │
├──────────────────────────────────────────────────────────────────────────┤
│ L6 排版层    Typesetting：源字符 box 原地改写 / 译文新建字符对象            │
│              midend/typesetting.py → layout_geometry.json                │
├──────────────────────────────────────────────────────────────────────────┤
│ L5 翻译协议层 段落 id（P02-003）+ 样式锚点 [[S1]] + 公式锚点 [[F3]]        │
│              ILTranslator（midend/il_translator.py）+ markdown_view      │
├──────────────────────────────────────────────────────────────────────────┤
│ L4 段落 IR   PdfParagraph / PdfParagraphComposition                      │
│              （pdf_line / pdf_formula / pdf_same_style_characters /      │
│                pdf_same_style_unicode_characters / pdf_character）       │
│              ParagraphFinder + StylesAndFormulas + TocDetector           │
├──────────────────────────────────────────────────────────────────────────┤
│ L3 原生字符 IR  page.pdf_character：PdfCharacter（字符 + box + 字体 +      │
│                visual_bbox + formula_layout_id）＝ 唯一写回真源            │
│                new_parser/native_parse.py                                │
├──────────────────────────────────────────────────────────────────────────┤
│ L2 provider IR  ProviderDocument（MinerU 的 block/line/span 树 +        │
│                 reading_order + unknown_types）                          │
│                 docvision/provider_ir.py                                │
├──────────────────────────────────────────────────────────────────────────┤
│ L1 布局层     page.page_layout：PageLayout（区域 box + class_name + conf） │
│              MinerUDocLayoutModel → LayoutParser                        │
├──────────────────────────────────────────────────────────────────────────┤
│ L0 PDF 对象层  pymupdf：页面内容流、注释（Link/TOC）、xobject、字体         │
└──────────────────────────────────────────────────────────────────────────┘
```

数据流自上而下「解析」，自下而上「重建」：
解析把 L0 收敛到 L5（文本 + 协议），重建把 L5 展开回 L0（内容流 + 注释）。

---

## 2. 每层职责与不变量

### L0 PDF 对象层

**职责**：PDF 字节 ↔ pymupdf 对象。`_prepare_pdf` 在此做修复（null xref / filter /
mediabox），并**保留 Link 注释与书签树**。

**不变量**

- **I0.1** `_prepare_pdf` 之后页数与源 PDF 一致（回放校验依赖这一点）。
- **I0.2** Link 注释不在内容流里，改内容流不会删链接；但它们**不会自动跟随文字移动**。
- **I0.3** 书签（outline）绑定「页号 + 目标点」，页序变化时必须重映射。

### L1 布局层

**职责**：把每页可翻译/不可翻译区域标注为 `PageLayout(box, class_name, conf)`。

**不变量**

- **I1.1** MinerU 是**唯一**布局后端（本地 ONNX 已移除）；无布局模型时
  `TranslationConfig` 直接报错，不做静默回退
  （`translation_config.py`：`未提供布局模型：本地 ONNX 后端…已移除`）。
- **I1.2** 布局 bbox 从 MinerU 页面坐标（左上原点）转到 IL 坐标（左下原点），
  公式 `y' = H - y`；`H` 取 `page.cropbox` 高度。
- **I1.3** 覆盖率门禁：未命中任何 `page_layout` 的原生字符占比 ≤
  `layout_coverage_threshold`（默认 0.005）。超阈值 → 抛
  `layout_coverage_gate`，**不产出行内公式保护/段落/sheet**。
  审计产物无论是否过门禁都落盘。

### L2 provider IR

**职责**：保留 MinerU 的完整结构（block/line/span 层级、阅读顺序、
`index`/`level`/`sub_type`/`score`），供下游做 token 决策与一致性校验。

**不变量**

- **I2.1** provider IR 是**只读参考**：MinerU 的 `content` 文本**不覆盖**原生字符，
  只用于（a）行内公式 token 决策、（b）`span_text_mismatch` 一致性审计。
- **I2.2** 未知 block/span 类型不静默：记进 `unknown_types[]`（按页/位点/类型聚合）。
- **I2.3** `preproc_blocks` 不进 IR（与 `para_blocks` 重复）；`discarded_blocks`
  进 `blocks` 但**不进** `reading_order`。

### L3 原生字符 IR（唯一写回真源）

**职责**：`page.pdf_character` 是重建时唯一的字符真源——每个字符带
`box`（PDF 坐标）、`visual_bbox`、`pdf_style`（字体/字号/颜色）、`xobj_id`。

**不变量**

- **I3.1（核心）** 原生字符是**唯一写回真源**：译文只改变「段落如何渲染」，
  不替换真源字符数据；公式/被跳过段落的字符原样 passthrough。
- **I3.2** 覆盖率统计必须发生在 `ParagraphFinder` **之前**：之后
  `page.pdf_character` 只剩被跳过的字符，分母会错。
- **I3.3** 字符对象**身份跨 `pickle` 保持**（`state.pkl`）；Typesetting 之后
  同一对象上的 `box` 已是重排后坐标——链接重映射正是依赖这一点。

### L4 段落 IR

**职责**：把字符聚成 `PdfParagraph`，composition 承载「行 / 公式 / 同样式字符 /
同样式 unicode / 单字符」五种形态，并挂 `layout_label` 与确定性 `debug_id`。

**不变量**

- **I4.1** `debug_id` 格式 `P{页:02d}-{序:03d}`（如 `P02-003`），
  在 `_deterministic_ids` 中按页内顺序重写，跨运行可复现。
- **I4.2** `layout_label` 决定「是否翻译」；判定入口有两个且必须一致：
  - agent 工具链：`tools/agent/translation_selection.py::PROTECTED_LABELS`
  - 主 CLI：`TranslationConfig.MINERU_DEFAULT_SKIP_TRANSLATE_LAYOUT_LABELS`
    经 `MINERU_SKIP_TRANSLATE_ALIAS_MAP` 展开
- **I4.3** `char.formula_layout_id` 由 `ParagraphFinder` **无条件重写**
  （`is_character_in_formula_layout` 的结果）；因此「让某段字符变公式」只能通过
  **追加 formula 布局区域**实现，不能直接设该字段。
- **I4.4** 目录条目化（TocDetector）只替换「整段所有行都是目录条目」的段落；
  混合内容的段落不动（记 `skipped_pages`）。

### L5 翻译协议层

**职责**：把段落转成「可校验、可确定性修复」的文本协议，并渲染成连续 Markdown。

**协议要素**

| 形态 | 含义 |
|---|---|
| `<!-- id=P02-003 label=text -->` | 段落标记；id 必须一一对应回填 |
| `<style id='1'>…</style>` | 富文本片段（字体/字号/颜色 run），canonical 形式 |
| `[[S1]]…[[/S1]]` | 同一 run 的 Markdown 锚点形式（模型看到的形式） |
| `{v3}` / `[[F3]]` | 公式占位符（canonical / 锚点形式），不可翻译不可移动 |

**不变量**

- **I5.1** 翻译单元是**整篇**：`document.md` 一次调用翻译完，不分块并发。
  单词/术语一致性由此保证。
- **I5.2** 锚点多重集必须与源文一致；**顺序不强制**；锚点有增删时先确定性
  修复（`repair_target`：proportional），修复后多重集仍不一致 →
  `anchor_multiset_mismatch` 阻断。顺序与源文不同不阻断，只记
  `anchor_reordered` 警告。
- **I5.3** 漏行回退原文（`fallback_ids`），不阻断重建——**这是有意的**：
  宁可交付带原文的行，也不因模型漏行而整篇失败。
- **I5.4** Markdown 结构前缀（`#`/`##`/`###`/`*`/`**`/`- `）只给模型看，
  回填时由 `_clean_markdown_body` 剥掉，不进入译文 IR。

### L6 排版层

**职责**：为译文本计算行内位置、缩放、换行；输出 `layout_geometry.json`。

**不变量**

- **I6.1** 源字符 box 被**原地改写**（passthrough 分支）；
  译文新字符是**新建对象**。因此「源字符对象 → 重排后 box」的映射天然存在。
- **I6.2** 无排版覆盖（`layout_overrides.json` 不存在）时行为与改造前一致。

### L7 交付层

**职责**：mono（源容器 + 新内容流）/ dual（拼宽或交替页）；链接与书签搬运。

**不变量**

- **I7.1** mono = **从源 PDF 打开** + 重写每页内容流。注释（链接/书签）天然在容器里，
  但坐标/页号需重映射。
- **I7.2** 链接矩形按**源字符身份**重算（三级回退），不再按译文文字搜索。
- **I7.3** URI 集合门禁：mono 输出的 URI 集合必须等于源快照集合；
  `only_include_translated_page`（删页模式）跳过该门禁。
- **I7.4** dual 不重复做 URI 门禁（其链接来自 mono + 源页，mono 已验）。

---

## 3. 关键对象生命周期

以「一个被翻译的正文段 `P09-006`」为例：

```text
[解析] page.pdf_character[i]  ← PdfCharacter 对象 A（box=源坐标, 字体 F137）
          │  ParagraphFinder：A 进入 P09-006 的 pdf_line composition
          │  ILTranslator.pre：段 → "<style id='1'>…</style>{v1}…"（canonical）
          │  markdown_view：canonical → "…[[S1]]…[[F2]]…"，写 document.md
[快照] link_snapshot.build_link_state：把「链接 → A 的页内下标」写进 state.pkl
          │  （A 对象本身也在 state.pkl 里，身份保持）
[翻译] LLM 只看到文本与锚点；段 id P09-006 不变
[写回] apply_markdown → post_translate_paragraph：
          P09-006.pdf_paragraph_composition ← 【新建】PdfSameStyleUnicodeCharacters（译文）
          公式 composition 仍引用原公式字符对象（passthrough）
          ★ 此时 A 已不在 composition 里（被翻译的段落），但 state.pkl 仍持有 A
[重排] Typesetting.typesetting_document：
          ・passthrough 单元：原地改写其字符 box（公式字符 → 新坐标）
          ・需重排单元：为译文新建 PdfCharacter 对象
[重建] PDFCreater.write：
          ① 用源 PDF 打开 → 逐页 update_page_content_stream（内容流整体重发）
          ② 链接重映射：A 若仍存活于 composition（公式/跳过段）→ 用 A 的新 box 并集；
             否则退回「所在段落 box + 段内相对投影」；都没有 → unresolved（保持原矩形）
          ③ URI 集合门禁
          ④ dual（可选）：拼宽 + 用 mono 页链接缩放搬运 + 书签搬运/重映射
```

**为什么这个生命周期重要**：链接与公式的「跟随」能力完全依赖
「源对象身份在 state.pkl 内保持」这一事实（I3.3）。若将来把 IR 序列化成纯数据
（丢弃对象身份），链接字符并集回退会失效——届时应改走段落投影路径（已有实现）。

---

## 4. 两条入口的同构性

解析链有两条入口，排序必须一致：

| 步骤 | `markdown_view._run_parse` | `workflow.extract` |
|---|---|---|
| `_prepare_pdf` | ✓ | ✓ |
| `LayoutParser` | ✓ | ✓ |
| `InlineMathProtector` | ✓（`LayoutParser` 之后） | ✓ |
| `EnclosedMarkerFixer` | ✓ | ✓ |
| `ParagraphFinder` | ✓ | ✓ |
| `TocDetector` | ✓（`ParagraphFinder` 之后） | ✓ |
| `StylesAndFormulas` | ✓ | ✓ |
| 书签/链接快照 | ✓ | ✓ |
| `_deterministic_ids` | ✓（仅 markdown_view） | — （extract 保留随机 id） |

> 改动解析链时**两条都要改**。`tests/test_provider_alignment.py` 里有一条
> 回归断言钉住 `InlineMathProtector` 在两条入口都被调用。
