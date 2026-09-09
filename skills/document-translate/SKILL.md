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
#    --layout mineru：MinerU 云端布局识别（推荐，作者区/参考文献/图表内部可自动跳过）
#    --mineru-token：默认读环境变量 MINERU_API_TOKEN；同一 PDF 的布局结果按内容哈希缓存
#    --skip-labels：在默认跳过集之外追加跳过的 label（逗号分隔）
python -m babeldoc.tools.agent extract <pdf> --workdir <dir> --layout mineru \
    [--pages 1,2] [--lang-in en --lang-out zh] [--mineru-token <tok>] [--skip-labels author,code]

# 2. 校验译文（id 对齐 + 占位符完整）并写回 IR；失败输出违规清单，退出码 1
#    apply 自动归一化占位符双标点（{vN} 展开尾部已带标点而译文又紧跟 ，/、/。）
python -m babeldoc.tools.agent apply <workdir> <translated.jsonl>

# 3. 从 IR 重排生成 PDF（mono 单语；--dual 加拼宽双语对照）
python -m babeldoc.tools.agent reconstruct <workdir> [--output-dir <dir>] [--dual]

# 4. 页渲染 PNG（视觉审查用）
python -m babeldoc.tools.agent render <pdf> --pages 1,2 [--dpi 120] [--out-dir <dir>]
```

产物路径：`<workdir>/agent/sheet.jsonl`（待译清单）、`<workdir>/agent/state.pkl`（IR 状态，勿手改）。

### 布局模式与跳过语义

- **native**（默认）：本地 DocLayoutModel。所有 text 段都会进 sheet。
- **mineru**（推荐）：作者区（author）、参考文献（reference）、图片内部（figure）、表格内部
  （table_text）、代码（code）、页眉页脚页码等自动跳过不译，**保留原文渲染**；
  图注/表注（figure_caption / table_caption / code_caption）正常翻译。
  用 `--skip-labels` 追加、或查看 extract 返回的 `skipped_label_counts` 确认跳过了什么。

## 占位符协议（翻译时必须严格遵守）

- `{v1}` `{v2}` … 是公式占位符：**原样保留**，不得改写、增删、翻译、调换顺序。
  展开内容可能自带标点（如 `[17],`），因此**占位符后不要再加 ，/、/。**（apply 也会程序化去重）。
- `<style id='1'>…</style>` 是富文本标记：**标签原样保留**，只翻译标签内外的自然语言文本。
- 人名、邮箱、URL、引用标记（如 [1]）原样保留。
- 译文 target 中占位符的集合与出现次数必须与 source 完全一致——apply 会程序化校验，不符即拒绝。

## 编排流程

1. **extract**：`--layout mineru` 解析（大文档先用 `--pages` 限制页数试点）。检查返回的
   `layout_label_counts` 与 `skipped_label_counts`，确认跳过语义符合预期。
2. **翻译 subagent**：把 `sheet.jsonl` 分批（建议每批 ≤40 行）译出，输出 `translated.jsonl`，
   每行 `{"id": ..., "target": ...}`，不增行不漏行。提示词模板：
   - 角色：专业 lang_in→lang_out 译者；
   - 附占位符协议全文（上述，含"占位符后不加标点"）；
   - 术语要求：同一术语全篇统一（如 Poisoned Model Rate 统一译"毒化模型率"）；
   - 引用占位符后不要额外加逗号/顿号；
   - 输出要求：仅输出 JSONL，无解释、无 markdown 代码块。
   参考实现：`experiments/batch_translate.py <workdir> --model <model> --batch-size 40`。
3. **apply**：校验通过 → 进入 4；失败 → 读取违规清单（缺/多哪个占位符、哪个 id），只把违规行
   发回重译，**重试 ≤2 次**；仍失败的段落回退 `target = source`（恒通过校验，原文保留）。
4. **reconstruct** → **render**：生成目标语言 PDF 并渲染 PNG。
5. **审查（双轨，report 脚本 + 视觉抽查）**：
   - 结构化：`python experiments/review_report.py <workdir> <output_pdf>` →
     `agent/review_report.json`（P0 占位符残留/回退、P1 漏译/标题字号塌陷、P2 双标点三类）。
   - 视觉：抽代表性页（首页作者区、图注密集页、末页参考文献）render PNG 审查。
   - **视觉结论必须用输出 PDF 文本层复核**（pymupdf 抽文本比对），视觉模型会把作者区/小号图注
     读错（实测误报率不低）。
6. **迭代**：段落级问题 → 改 `translated.jsonl` 对应行后重新 apply（可多次覆盖）；
   确定性缺陷（术语不统一、双标点残留）→ 直接程序化改写 target 再 apply，无需重译。
   文档级排版问题 → 调整后 reconstruct。**文档级迭代 ≤2 轮**，超出则接受现状并在报告中说明。

## 预算与产出

- 成本护栏：目标 ≤3x 固定管道（当前无内建计费，凭 sheet 行数与重试次数估算）。
- 最终交付：mono PDF（+dual 如需）、render PNG、审查报告（每页问题清单 + 已修复项 + 遗留项）。

## 已知边界（MVP）

- `page` 字段为 0-based 页号。
- 段落粒度由 ParagraphFinder 决定；MinerU author 判定是首页标题与摘要之间的启发式，
  极端版式可能漏标——审查时用文本层复核作者区是否保持原文。
- 引用占位符展开内的 ASCII 逗号（`[17],`）会原样保留，中文句中偶现半角逗号，属可接受残留。
- layout_label 的 skip 杠杆（set-skip）、box 编辑、IR 快照回滚尚未实现（M4）。
