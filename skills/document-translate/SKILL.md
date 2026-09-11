---
name: document-translate
description: Agent 编排的高保真 PDF 文档翻译（BabelDOC 解析/重构 + subagent 翻译 + 结构化审查 + 排版微调）。适用于英文学术论文→中文等文档翻译任务，保留排版、公式与富文本样式；当需要翻译 PDF 文档、复核译文完整性、或按 review 结论微调排版并重排时使用。
---

# document-translate：agent 编排的 PDF 文档翻译

把 BabelDOC 管道拆成**可恢复的 DocumentJob**：解析/写回/重建/审查/lint 都是确定性工具，
翻译和审查通过可替换 Provider 注入，排版微调由**覆盖文件**驱动、可回滚。

发布包提供两个稳定 namespace：

```python
from babeldoc_core import DocumentJob, JobConfig
from babeldoc_tools import dispatch, list_tools, get_schema
```

`babeldoc_core` 保存 IR、公式原子、协议 gate 和 Job manifest；`babeldoc_tools` 提供
Python/JSON/CLI 三层工具适配。skill 目录只保存角色提示词、编排规则和验收说明。

```
job_create → parse_document → translate_document → validate_translation（gate）
   ├ blockers → retranslate_ids → apply_translation → 复核
   └ pass → reconstruct_pdf → render_pages → 并行审查（protocol/fidelity/layout）
            → layout_patch → reconstruct_pdf → render_pages → layout_lint（≤2 轮）→ export_report
```

> 目录：`agents/`（各角色的提示词）`tools/`（legacy 兼容工具）`reference/`（管线 / 契约 / 排查）
> `prompts/`（legacy 提示词，供旧 batch 流程兼容）

## 加载方式

- 由编排者显式指向本目录即可：`pi --skill skills/document-translate`；
- 或写进项目设置：`.pi/settings.json` 里 `{"skills": ["skills"]}`；
- 本 skill 目录不在 pi 默认发现路径内，**默认不改 harness 配置**，需要自动发现时用上面两种写法。

## 工具速查

```bash
# 发布 wheel 后：
babeldoc-tools list                      # 全部工具 + JSON Schema
babeldoc-tools schema layout_patch       # 单工具入参
babeldoc-tools call <tool> --args-json '{...}'
# 仓库 checkout 的 legacy shim 仍可用于旧实验：
skills/document-translate/tools/bin/bdt list
```

stdout 恒为 `{"ok": true, "tool": …, "data": {…}}` 或 `{"ok": false, "error": {…}}`；
退出码 0/1（1 = 工具失败，错误码见 `error.code`）。

| 组 | 工具 | 一句话 |
|---|---|---|
| parse | `parse_document` | PDF → 连续 Markdown（锚点）+ IR 状态 |
| translate | `translate_document` | 整篇翻译（默认 `agy` CLI），自动补译漏行 |
| translate | `retranslate_ids` | 按 id 重译（带 feedback）→ 合并 → 可选 apply |
| translate | `apply_translation` | 校验收写回 IR（确定性修复锚点/双标点/注释残留） |
| review | `review_document` | 结构 gate：`verdict=pass\|needs_fix` + blockers/warnings |
| review | `backtranslate_check` | 高风险段回译 + Levenshtein 相似度 |
| review | `dump_text_layer` | PDF 文本层导出（视觉结论必须回文本层复核） |
| layout | `reconstruct_pdf` | 应用排版覆盖重排 + dump 几何 |
| layout | `render_pages` | 页面 → PNG（视觉审查） |
| layout | `layout_set` / `layout_lint` / `layout_locate` | 写覆盖 / 查缺陷 / 定位 id |
| version | `snapshot` / `restore` / `list_snapshots` | 小文件快照与回滚 |
| report | `export_report`（兼容 `report`） | 产出 `agent/FINAL_REPORT.md` |

工具契约、字段与阈值：`reference/schemas.md`；出问题先查 `reference/troubleshooting.md`。

## 编排流程（主 Agent 视角）

1. **解析**：`parse_document --pdf <pdf> --workdir <wd> --layout mineru`
   检查返回的 `label_counts` / `skipped_label_counts` —— 作者区/参考文献/图内/表内
   应被跳过（保留原文渲染），图注/表注应进入 `sheet`。
   同时确认三项新指标（`python experiments/toolchain_gates.py <wd>`）：
   `layout_coverage`（未覆盖字符占比 ≤ 0.5%）、`toc_integrity`（
   `agent/source/toc.json` 的条目数 == `anchors.json` 的 `toc_entry` 行数 == 书签数）、
   `protected_tokens`（`alignment.json` 的 `inline_equation_matched`）。
2. **翻译**：`translate_document --workdir <wd> --model <m> --effort low`
   （提示词 `agents/translator.md`；缺 `agy` 时用 `--arg translated_md=<文件>` 导入译文）。
3. **写回 + 结构 gate**：
   `apply_translation` → `review_document`（可传 `--arg mono=<pdf> --arg dual=<pdf>` 做页数/目录/链接核对）
   - `blockers` 里的 id → `retranslate_ids --ids [...] --feedback "..."` → 回到本步（**≤2 轮**）；
   - `warnings` 里的高风险段 → `backtranslate_check`（相似度 < 0.55 判为重译对象）；
   - `verdict=pass` 才继续（warnings 允许携带）。
4. **重排 + 渲染**：`reconstruct_pdf --workdir <wd>`（默认出 mono+dual）→
   `render_pages`（首页、图表密集页、表格页、末页）。
5. **并行审查**（三个角色，提示词已落盘）：
   - `agents/reviewer-protocol.md`：结构/协议/跳过语义（数据源：`review_verdict.json`）；
   - `agents/reviewer-fidelity.md`：语义与漏译（回译）；
   - `agents/reviewer-layout.md`：版式（数据源：`layout_lint` + PNG，结论必须带 id/box）。
   findings 汇总成一份清单（每条含 `sev` / `evidence` / `fix`）。
6. **排版迭代**（≤2 轮）：`snapshot` → 用 `agents/layout-fixer.md` 决策出 patch →
   `layout_set` → `reconstruct_pdf` → `layout_lint` 复核。
   变差就 `restore`；每轮只改必要字段，**单轮 ≤8 段**。
7. **收尾**：`report --workdir <wd>` → `FINAL_REPORT.md`（token 用量 / apply 指标 /
   verdict / lint 前后对比 / 遗留项），交付 mono + dual + render PNG。

## 排版微调杠杆（`layout_set`）

| 症状 | 首选杠杆 |
|---|---|
| 溢出/与相邻段重叠 | `scale_cap` 下调（如 0.9） |
| 字号比原文明显偏小（自动缩放） | `box_scale` 1.05~1.3（放宽框） |
| 主动减字（标题偏大） | `font_scale` 0.9~0.95 |
| 需要在该段内部分行 | `force_break_after_text`（锚定子串） |
| 段落位置不对 | `box`（PDF 坐标，自动裁剪到页面） |

规则：**一次只改必要字段**；skip 段落（`overridable=false`）不可覆盖；
`layout_overrides.json` 不写 `state.pkl`，所以随时可回滚。

## 验收清单

- [ ] `review_document`：`verdict=pass`，`apply_ok=true`，`fallback=0`；
- [ ] 页数/目录条目与原文一致；`links_mono ≥ 原文`、`links_dual ≈ 2×`；
- [ ] `toolchain_gates.py`：五项门禁无 `fail`；`link_uri_set_match=true`、
      `unresolved` 已逐条确认（纯图形链接可接受）；
- [ ] 目录页：`toc.json` 条目数 == `anchors.json` 的 `toc_entry` 行数 == 书签数；
- [ ] 公式：`alignment.json` 的 `inline_equation_matched` 覆盖绝大多数 span
      （未对齐的应能解释为图内/代码块公式）；
- [ ] `placeholder_leftovers_* = 0`、`p1_title_shrink = 0`；
- [ ] `layout_lint`：`P0 = 0`；P1 全部有结论（已修 or 明确 accept）；
- [ ] `formula_splice`（P2）等已知解析层限制已列入遗留项，不得当作翻译缺陷重译；
- [ ] 渲染首页/图表密集页/表格页/末页，人工确认无压字、无豆腐块；
- [ ] `FINAL_REPORT.md` 落盘，遗留项逐条写明原因。

## 已知边界

- **MinerU 是唯一布局后端**（本地 ONNX `--layout native` 已移除）；无 token 时
  用 `--mineru-json` / `--mineru-cache-key` 回放缓存布局。
- **原生字符是唯一写回真源**：MinerU 的 `content` 只用于行内公式 token 决策与
  一致性审计（`alignment.json` 的 `span_text_mismatch`），不覆盖原生字符。
- 覆盖率门禁超阈值会**直接中止解析**（默认 0.5%）：宁可先确认漏排，也不静默交付。
- 链接映射三级回退（字符并集 → 段落投影 → 保持原矩形）；
  纯图形链接（无文字）会进 `link_unresolved`，保持原位是预期行为。
- 段落粒度由 ParagraphFinder 决定；`page` 字段为 0-based，`id` 为 `P<页>-<序号>`。
- 表格内部、图内文字保持原文（`table_text`/`figure` 跳过）——本版本不翻译表内内容。
- 目录条目化只处理「整段所有行都是目录条目」的段落；混合内容不动。
- 文本层可能出现 CJK 兼容区码位（复制/检索得到兼容字，视觉正常），属 P2 记录项。
- 排版自动缩放（Typesetting `optimal_scale`）是保守策略：译文长度变化大的段落仍可能
  字号偏小，需要人工覆盖或接受。
- 覆盖不做跨段落联动（改了 A 段高度可能压到 B 段）：靠 `layout_lint` 复核发现。

> 工具链实现细节（分层不变量、阶段契约、标签字典、门禁、排查手册）：
> [`docs/toolchain/`](../../docs/toolchain/)。
