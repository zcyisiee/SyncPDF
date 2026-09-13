# Task: A3 — InlineMathProtector 按字体与括号平衡修剪 MinerU 公式 span 边界

## Objective

MinerU 偶尔把正文片段误判为 inline_equation span（实测：`(e.g.` 被吞进 `'( \mathsf { e . g . }'`，左括号进公式、右括号留正文，最终译文出现裸 `(e.g.,` 括号不配对）。`InlineMathProtector` 现按 span bbox 全量保护区域内原生字符。本任务给保护加护栏：span 边界处的**正文字体字符**且触发**括号不平衡/字母集不一致**时收缩 bbox，防止正文被吞。

## Context

- 分支 `feature/latex-bbox-layout`。**前置依赖：A1 已合入**（验收要在干净文本基线上做）。
- 坏例数据（`测试.pdf`，workdir `tmp/md-test/`）：
  - MinerU span `p0-b7-l0-s1` content=`'( \mathsf { e . g . }'`，区域内原生字符 = `( e . g . ,`，**全部是正文字体 Ty1（AAAAAB+XCharter-Roman）**；
  - 该页公式字体集合：`XCharterMathMI、XCharterMathRM、txmiaX、txsys、SFTT1095`；
  - 对照：正常公式 span `p0-b0-l0-s1` `'( | A | = 1'` 的原生字符 `(|𝐿|=1` 是 XCharterMathMI → 不该被修剪；
  - **陷阱**：`span_text_consistent`（`provider_alignment.py:438`，字母集 ≥0.6 重合）对这个案例**恰好返回 True**（span 字母 {e,g} ⊆ native {e,g}）——单一判据分不出 `(e.g.`，必须叠加字体/括号判据。
- 必读：`babeldoc/format/pdf/document_il/midend/inline_math_protector.py`（`protect_page` 全文，尤其 `_overlap_ratio`/`MIN_REGION_SIZE`/追加布局区域循环）、`babeldoc/format/pdf/document_il/utils/provider_alignment.py`（`align_page` 的 char_bbox 匹配与 `span_text_consistent`）、`babeldoc/tools/agent/workflow.py:120 _page_font_maps`（页字体表怎么取）。
- 对齐审计已存在：`alignment.json` 的 `span_text_samples` 记录 mismatch；`inline_matches` 有 `char_indices`（span 区域内的原生字符索引）。

## Deliverables

1. `inline_math_protector.py` 新增修剪函数（在追加保护区域之前执行）：
   - 输入：page、region（ProviderBox）、provider_page 的 span content；
   - 取区域内原生字符（复用 `align_page` 的 char_bbox 逻辑或按 box 相交重取）；
   - **双判据 AND 才修剪**：
     a. 边界处字符字体 ∉ 公式字体集合（公式字体集合 = 该页所有出现在既有 MinerU formula 布局区域内的字符 font_id 集合；无法确定时退化为"非区域内多数字体"）；
     b. span content 括号不平衡（`(` 数 ≠ `)` 数）**或** `span_text_consistent` 为 False；
   - 修剪动作：从左边界逐字符剔除判据 a 命中的字符直到不命中或字符数归零（右侧对称处理右括号多余的情况）；剔除后区域宽 < `MIN_REGION_SIZE` → 放弃整个保护（span 退回正文，记 `dropped`）。
2. `protected_inline_math` 审计条目增加 `trimmed` 字段：`{"old_box": [...], "new_box": [...], "reason": "font+paren-unbalanced"}`；`alignment.json` summary 增加 `trimmed_inline_math` 计数。
3. 单测 `tests/test_inline_math_trim.py`（或并入合适的既有测试文件）：
   - `(e.g.` 案例：左界剔除 `(`（若 native 以正文字体 `(` 开头且 span 括号不平衡）→ `e.g.` 保留为公式或按判据继续收缩；
   - 正常公式 `(|A|=1`：数学字体 → 不修剪；
   - 正文 Italic 单变量（字体不同但括号平衡且字母集一致）→ 不修剪（回归保护）；
   - 全正文字体且括号平衡（假公式但判据 b 不满足）→ 不修剪（保守）。
4. `apply_report.json` / `alignment.json` 消费方（`experiments/protocol_report.py` 等）字段兼容检查——新增字段不破坏旧读取。

## Constraints

- 修剪只收缩不扩张；最坏退化 = 该 span 不保护（回到"正文被当公式翻译"的旧行为），不产生新错误类型。
- 不改 `provider_alignment.align_page` 的匹配逻辑（只读复用）。
- `DUPLICATE_REGION_IOU`、`MIN_REGION_SIZE` 等既有常量语义不变。
- 用户明确关注回归风险：**有些论文的斜体变量用正文字体**（如 Italic 的 `n`）→ 双判据 AND 就是为这个边界设计的，测试必须覆盖。

## Validation

```bash
python3 -m pytest tests/test_inline_math_trim.py tests/test_provider_alignment.py -q
python3 -m pytest tests/ -q
# 坏例（A1 合入后重跑基线）：
python3 -m babeldoc.tools.agent md-extract "测试.pdf" --workdir tmp/a3-verify
# 断言：document.md 中 P01-009 段 (e.g., 为正文文本（不再是 {vN} 占位符）；(|A|=1 等仍为公式锚点
# 好例：2512 p7 同样重跑，公式锚点数量不变
python3 -m babeldoc.tools.agent md-extract 2512.08296v3.pdf --workdir tmp/a3-verify-2512 --pages 7
# 全链路 + 渲染目视：
python3 experiments/markdown_translate.py tmp/a3-verify --output-dir tmp/a3-verify/output
# 断言：最终 mono PDF 中 (e.g., 括号配对；公式图形无丢失
```

## Report back

- 修改/新增文件清单与每处改动目的；
- 验证命令完整输出摘要（`(e.g.` 修复前后对照、公式锚点数量对照）；
- 发现并修复的缺陷（若有）；
- 任何偏离 brief 的决定及理由。
