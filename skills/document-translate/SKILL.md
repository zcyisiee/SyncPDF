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

# 3. 从 IR 重排生成 PDF：mono 中文单语 + dual 中英拼宽对照（最终交付两者都要）
python -m babeldoc.tools.agent reconstruct <workdir> --output-dir <dir> --dual

# 4. 页渲染 PNG（视觉审查用）
python -m babeldoc.tools.agent render <pdf> --pages 1,2 [--dpi 120] [--out-dir <dir>]
```

产物路径：`<workdir>/agent/sheet.jsonl`（待译清单）、`<workdir>/agent/state.pkl`（IR 状态，勿手改）。
**subagent 提示词已落盘**：`skills/document-translate/prompts/translator.md`（翻译，batch_translate.py
自动加载）、`skills/document-translate/prompts/format-reviewer.md`（格式审查清单，审查阶段逐项执行）。

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
   每行 `{"id": ..., "target": ...}`，不增行不漏行。提示词落盘于 `prompts/translator.md`
   （含占位符协议、术语统一、JSONL 输出要求），直接执行：
   `python experiments/batch_translate.py <workdir> --model <model> --effort low --batch-size 40`
   （脚本自动加载该提示词，并做协议校验 + 违规打回重试 ≤2 + 回退）。
3. **apply**：校验通过 → 进入 4；失败 → 读取违规清单（缺/多哪个占位符、哪个 id），只把违规行
   发回重译，**重试 ≤2 次**；仍失败的段落回退 `target = source`（恒通过校验，原文保留）。
4. **reconstruct --dual** → **render**：生成中文 mono PDF + 中英对照 dual PDF（拼宽页，
   左原文右译文），渲染代表性页 PNG（首页、图注最密集页、表格页、末页）。
5. **格式审查 subagent**：按 `prompts/format-reviewer.md` 的清单逐项执行并记录 PASS/FAIL 证据。
   清单覆盖五组检查：A 跳过语义（作者区/参考文献/图表内部应保留原文）、B caption 已译且未截断、
   C 排版（标题字号/dual 左右对应/溢出重叠）、D 协议与标点（review_report.json 五项指标应全 0）、
   E 术语一致性。**铁律：视觉发现必须回到输出 PDF 文本层（pymupdf）复核**——视觉模型会把作者区
   混排、小号图注读错（实测误报率高），未复核的发现不得作为修复依据。
   结构化锚点：`python experiments/review_report.py <workdir> <mono pdf>` → `agent/review_report.json`。
6. **迭代**：段落级问题 → 改 `translated.jsonl` 对应行后重新 apply（可多次覆盖）；
   确定性缺陷（术语不统一、双标点残留）→ 直接程序化改写 target 再 apply，无需重译。
   修完后重复 4-5 验证。**文档级迭代 ≤2 轮**，超出则接受现状并在报告中说明。

## 预算与产出

- 成本护栏：目标 ≤3x 固定管道（当前无内建计费，凭 sheet 行数与重试次数估算）。
- 最终交付：中文 mono PDF + 中英对照 dual PDF、render PNG、审查报告
  （清单各项 PASS/FAIL + 已修复项对照 + 遗留项，推荐落盘 `<workdir>/FINAL_REPORT.md`）。

## 已知边界（MVP）

- `page` 字段为 0-based 页号。
- 段落粒度由 ParagraphFinder 决定；MinerU author 判定是首页标题与摘要之间的启发式，
  极端版式可能漏标——审查时用文本层复核作者区是否保持原文。
- 引用占位符展开内的 ASCII 逗号（`[17],`）会原样保留，中文句中偶现半角逗号，属可接受残留。
- layout_label 的 skip 杠杆（set-skip）、box 编辑、IR 快照回滚尚未实现（M4）。
