---
name: document-translate
description: Agent 编排的高保真 PDF 文档翻译（BabelDOC 解析/重构 + subagent 翻译）。适用于英文学术论文→中文等文档翻译任务，保留排版、公式与富文本样式。当用户要求翻译 PDF 文档并保留格式时使用。
---

# document-translate：agent 编排的 PDF 文档翻译

本 skill 把 BabelDOC 管道拆成可编排的工具调用：解析与排版重构由确定性工具完成，
**翻译本身由你（翻译 subagent）完成**。翻译通过"translation sheet"交换，必须遵守占位符协议。

## 工具层

在仓库根目录（BabelDOC-agy-mvp 分支）执行，四个子命令：

```bash
# 1. 解析 + 布局 + 段落 → 翻译清单（每行 {id, page, layout_label, source}）
python -m babeldoc.tools.agent extract <pdf> --workdir <dir> [--pages 1,2] [--lang-in en --lang-out zh]

# 2. 校验译文（id 对齐 + 占位符完整）并写回 IR；失败输出违规清单，退出码 1
python -m babeldoc.tools.agent apply <workdir> <translated.jsonl>

# 3. 从 IR 重排生成 PDF（mono 单语；--dual 加拼宽双语对照）
python -m babeldoc.tools.agent reconstruct <workdir> [--output-dir <dir>] [--dual]

# 4. 页渲染 PNG（视觉审查用）
python -m babeldoc.tools.agent render <pdf> --pages 1,2 [--dpi 120] [--out-dir <dir>]
```

产物路径：`<workdir>/agent/sheet.jsonl`（待译清单）、`<workdir>/agent/state.pkl`（IR 状态，勿手改）。

## 占位符协议（翻译时必须严格遵守）

- `{v1}` `{v2}` … 是公式占位符：**原样保留**，不得改写、增删、翻译、调换顺序。
- `<style id='1'>…</style>` 是富文本标记：**标签原样保留**，只翻译标签内外的自然语言文本。
- 人名、邮箱、URL、引用标记（如 [1]）原样保留。
- 译文 target 中占位符的集合与出现次数必须与 source 完全一致——apply 会程序化校验，不符即拒绝。

## 编排流程

1. **extract**：解析文档（大文档先用 `--pages` 限制页数试点）。检查返回的 `layout_label_counts`，了解文档构成。
2. **翻译 subagent**：把 `sheet.jsonl` 逐行译出，输出 `translated.jsonl`，每行 `{"id": ..., "target": ...}`，不增行不漏行。翻译提示词模板：
   - 角色：专业 lang_in→lang_out 译者；
   - 附占位符协议全文（上述）；
   - 输出要求：仅输出 JSONL，无解释、无 markdown 代码块。
3. **apply**：校验通过 → 进入 4；失败 → 读取违规清单（缺/多哪个占位符、哪个 id），只把违规行发回重译，**重试 ≤2 次**；仍失败的段落回退 `target = source`（恒通过校验，原文保留）。
4. **reconstruct** → **render**：生成中文 PDF 并渲染 PNG。
5. **审查 subagent**（视觉双轨）：
   - 逐页 PNG 检查：文字溢出/重叠、缺字豆腐块、明显漏译、占位符残留；
   - 程序检查：`render` 后用 pymupdf 抽取文本，正则查残留 `\{v\d+\}` / `<style` 标签应为 0。
6. **迭代**：发现段落级问题 → 只重译问题段（改 sheet 对应行重新 apply，可多次 apply 覆盖）；文档级排版问题 → 调整后 reconstruct。**文档级迭代 ≤2 轮**，超出则接受现状并在报告中说明。

## 预算与产出

- 成本护栏：目标 ≤3x 固定管道（当前无内建计费，凭 sheet 行数与重试次数估算）。
- 最终交付：mono PDF（+dual 如需）、render PNG、审查报告（每页问题清单 + 已修复项 + 遗留项）。

## 已知边界（MVP）

- `page` 字段为 0-based 页号。
- 段落粒度由 ParagraphFinder 决定：作者区（姓名/单位/邮箱混排）可能拆段生硬，属审查迭代的目标类型。
- layout_label 的 skip 杠杆（set-skip）、box 编辑、IR 快照回滚尚未实现（M4）。
