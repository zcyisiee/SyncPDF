# layout label 字典

本文列出全部 layout label：**来源**（MinerU block type / span type / 启发式）、
**语义**、**是否翻译**、**保护原因**、**对应开关**。

label 由两级映射产生：

```text
MinerU block.type ──(_map_mineru_block_to_layout_label)──▶ layout_label ──▶ 翻译决策
                                                              ▲
增补来源：TocDetector(toc_entry / toc_entry_page)、InlineMathProtector(formula 区域)、
         LayoutParser(词表外的区域名)、启发式(author)
```

判定「是否翻译」有两个入口，**必须保持一致**：

- agent 工具链：`tools/agent/translation_selection.py::PROTECTED_LABELS`
  （外加 `CAPTION_LABELS` 白名单与几何/内容启发式）
- 主 CLI：`TranslationConfig.MINERU_DEFAULT_SKIP_TRANSLATE_LAYOUT_LABELS`
  经 `MINERU_SKIP_TRANSLATE_ALIAS_MAP` 展开

---

## 1. MinerU block type → layout_label

来源：`docsvision/mineru_doclayout.py::_map_mineru_block_to_layout_label`。

| MinerU block type | layout_label | 翻译 | 说明 |
|---|---|---|---|
| `text` | `text` | ✓ | 正文 |
| `title` | `title` | ✓ | 标题（含论文主标题与章节标题，MinerU 不细分） |
| `interline_equation`, `equation` | `formula` | ✗ | 行间公式（整块图形，保护） |
| `ref_text` | `reference` | ✗ | 参考文献条目 |
| `table_caption` | `table_caption` | ✓ | 表注（标题型，默认翻译） |
| `table_body` | `table_text` | ✗ | 表格内部文字 |
| `table_footnote` | `table_footnote` | ✗ | 表脚注 |
| `image_caption`, `chart_caption` | `figure_caption` | ✓ | 图注/图题 |
| `image_footnote` | `figure_text` | ✗ | 图内说明文字 |
| `image_body`, `chart_body`, `chart` | `figure` | ✗ | 图/图表本体 |
| `header` | `header` | ✗ | 页眉 |
| `footer` | `footer` | ✗ | 页脚 |
| `page_number` | `page_number` | ✗ | 页码 |
| `page_footnote` | `page_footnote` | ✗ | 页脚注 |
| `aside_text` | `aside_text` | ✗ | 边栏文字 |
| `code`, `algorithm`, `code_body` | `code` | ✗ | 代码/算法/伪码块 |
| `code_caption` | `code_caption` | ✓ | 代码题注 |
| `phonetic` | `text` | ✓ | 注音（当正文处理） |
| `table` | `table` | ✗ | 容器（仅在无可用 children 时） |
| `image` | `figure` | ✗ | 容器 |
| `list` + `sub_type=ref_text` | `reference` | ✗ | 容器 |
| `list` | `list_item` | ✓ | 列表项 |
| **未知类型** | `text`（有 lines）/ `abandon`（无） | — | 同时记入 `provider_ir.unknown_types[]` |

`abandon` 的语义：**不渲染也不翻译**（区域被丢弃）。

---

## 2. 增补 label（非 MinerU 直接产出）

| label | 来源 | 翻译 | 说明 |
|---|---|---|---|
| `toc_entry` | `TocDetector` | ✓ | 目录条目的**标题**部分；独立 id，只译标题文字 |
| `toc_entry_page` | `TocDetector` | ✗ | 目录条目的**点引导线 + 印刷页码**；原字符 passthrough，位置由程序保持 |
| `formula`（区域） | `InlineMathProtector` | — | 由 MinerU `inline_equation` span 追加；使区域内字符变公式占位符 |
| `author` | 启发式（首页 + 标题与摘要之间） | ✗ | MinerU 不标作者区，用几何+文本启发布尔判定 |
| `paragraph_title` | 词表（LLM/其他布局后端可能给出） | ✓ | 小节标题（MinerU 路径下通常不出现） |
| `doc_title` | 词表 | ✓ | 文档主标题（MinerU 路径下通常不出现） |
| `plain text` / `tiny text` | 词表（旧布局后端） | ✓ | 旧词表遗留；MinerU 路径不出现 |

### `toc_entry` / `toc_entry_page` 的分工

```text
印刷目录一行：  2.1  Overview . . . . . . . . . . 7
                └── toc_entry ──┘ └─ toc_entry_page ─┘
                    （可翻译）        （保护，原字符 passthrough）
```

- 条目总数/顺序/印刷页码/层级/缩进**全部由程序保持**；
- `printed_page_label` 不进翻译 prompt；
- 无编号条目的 `level` 由缩进聚类推断（编号模式 `2.1` → level 2）。

---

## 3. span 类型（provider IR）

MinerU 的 span `type` 与 block 类型同源，用于 token 决策而非 layout：

| span kind | 用途 |
|---|---|
| `text` | 普通文本 span；参与字符对齐审计 |
| `inline_equation` | **行内公式**：其 bbox 转成 `formula` 区域 → 字符受保护为 `{vN}` |
| `interline_equation` | 行间公式（通常作为整块 block 出现） |
| `image` / `table` / `chart` | 图内/表内/图表 span；视为已知，不记 unknown |

> `inline_equation` 的语义是**唯一依赖 MinerU 的信号**（不靠正则/字体启发式），
> 这是本轮「行内公式保护」比旧启发式更可靠的原因。

---

## 4. 保护原因分类（为什么这些 label 不翻译）

| 原因 | label |
|---|---|
| **坐标/图形资产**：翻译后无法重排，必须原样 passthrough | `figure`, `formula`, `table_text`, `toc_entry_page` |
| **结构化短文本**：翻译会破坏语义（编号/引用/页码） | `reference`, `page_number`, `code`, `toc_entry_page` |
| **非正文元数据**：作者区/版权/页眉页脚 | `author`, `author_info`, `header`, `footer`, `page_footnote`, `aside_text` |
| **丢弃** | `abandon` |

---

## 5. 开关与自定义

| 开关 | 位置 | 效果 |
|---|---|---|
| `--skip-labels a,b` | `extract` CLI | 追加跳过标签（逗号分隔，如 `reference,author,figure`） |
| `MINERU_DEFAULT_SKIP_TRANSLATE_LAYOUT_LABELS` | `translation_config.py` | 默认跳过集（主 CLI） |
| `MINERU_SKIP_TRANSLATE_ALIAS_MAP` | 同上 | 粗标签 → 细标签展开（`table` → `table_text` + `table_footnote`） |
| `PROTECTED_LABELS` | `translation_selection.py` | agent 工具链的跳过集 |
| `CAPTION_LABELS` | 同上 | 图注/表注白名单（优先翻译，即使落进受保护区） |
| `protected_layout_labels` | `get_character_layout(…)` | 传入后保护标签优先级最高（-1） |

**别名展开刻意不含 `*_caption`**：图注/表注/代码题注默认要翻译；
若要跳过 caption 必须显式传细粒度标签（如 `table_caption`）。

---

## 6. 已移除项

| 项 | 状态 | 说明 |
|---|---|---|
| `fallback_line` | **已移除** | 字符聚类兜底标签；由覆盖率门禁替代 |
| `provides_complete_layout` | **已移除** | ONNX 后端撤离后无意义 |
| 本地 ONNX 后端（`--layout native`） | **已移除** | MinerU 是唯一布局后端；无布局模型时 `TranslationConfig` 直接报错 |
| `DocLayoutModel.load_onnx` / `load_available` | **已移除** | — |

> `rpc_doclayout6/7/8`（executor 网关链路）**保留**，它们复用
> `extract_char.py` 的字符聚类函数（与已删除的 `fallback_line` 无关）。

---

## 7. 实测标签分布（DeepSeek 样本）

```text
翻译行（anchors.json rows=352）：
  text 220 | title 59 | toc_entry 54 | figure_caption 14 | table_caption 5

跳过行（anchors.json skipped=234）：
  reference 100 | toc_entry_page 54 | page_number 50 | figure 20 | table_text 5
  | table_footnote 2 | author 1 | title 1 | page_footnote 1
```

> 跳过集中的 `title 1` 是 `References` 标题（`P38-001`，reason=`references_heading`）——
> 不是 layout 保护，而是翻译选择的「参考文献区起始」信号
> （`select_paragraph` 命中后阻断该区段并以 `select_paragraph` 的读物顺序恢复）。
> `toc_entry 54` 与印刷目录条目数、书签数一致（54）。
