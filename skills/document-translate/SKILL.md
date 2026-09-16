---
name: document-translate
description: Agent 编排的高保真 PDF 文档翻译（BabelDOC 解析/重构 + subagent 翻译 + 结构化审查 + 排版微调）。适用于英文学术论文→中文等文档翻译任务，保留排版、公式与富文本样式；当需要翻译 PDF 文档、复核译文完整性、或按 review 结论微调排版并重排时使用。
---

# document-translate：agent 编排的 PDF 文档翻译

把 BabelDOC 管道拆成**可恢复的 8 个子命令**：解析、翻译、写回、重建、审查、
排版微调、报告、编排。每个阶段都是确定性工具，翻译和审查通过**可替换的子进程命令**
注入（stdin 读提示词、stdout 出结果），排版微调由**覆盖文件**驱动、可回滚。

**唯一入口是 `bdt`**（`pyproject.toml` 的 `[project.scripts]` 只注册它；等价于
`python -m babeldoc_tools`，任意 cwd 均可）。全仓只有一个 `babeldoc_tools` 包，
位于仓库根。

```
parse → translate（可迭代 --ids）→ apply → build → check → reviewer
      → [layout-set / translate --ids → build → check] → report
```

> 目录：`agents/`（各角色的提示词）`reference/`（管线 / 契约 / 排查）。
> 工具链实现细节见 [`docs/toolchain/`](../../docs/toolchain/)。

## 加载方式

- 由编排者显式指向本目录即可：`pi --skill skills/document-translate`；
- 或写进项目设置：`.pi/settings.json` 里 `{"skills": ["skills"]}`；
- 本 skill 目录不在 pi 默认发现路径内，**默认不改 harness 配置**，需要自动发现时用上面两种写法。

## 工具速查

```bash
# 在仓库根执行（包已在仓库根，任意 cwd 均可）：
uv run bdt --help               # 子命令清单
uv run bdt <subcommand> --help  # 单子命令参数
uv run bdt <subcommand> ...     # stdout 恒为单行 JSON
```

stdout 恒为 `{"ok": true, "data": {…}}` 或 `{"ok": false, "error": {…}}`；
日志/进度走 stderr；退出码 0/1（1 = 失败，错误码见 `error.code`），
2 = argparse 用法错误。

| 子命令 | 一句话 |
|---|---|
| `parse` | PDF → 连续 Markdown（锚点）+ IR 状态 |
| `translate` | 整篇翻译（`--translator <命令>`，stdin/stdout 协议），自动补译漏行；`--ids` 走重译合并 |
| `apply` | 校验译文 Markdown 写回 IR（确定性修复锚点/双标点/注释残留） |
| `build` | 应用排版覆盖重排生成 mono/dual PDF + dump 几何；`--render` 渲染页 |
| `check` | 三合一聚合：结构审查 + 排版 lint + 链接审计；`--strict` 时非 pass 退出码 1 |
| `layout-set` | 写排版覆盖（`--patch` / `--clear`） |
| `report` | 产出 `agent/FINAL_REPORT.md` |
| `debug` | 起/复用 `<workdir>` 的只读 debug 查看器（`--run-id` / `--stop` / `--source-pdf` / `--mono`）；各阶段加 `--debug` 采集，见 [README「Debug 工作台」](../../README.md#debug-工作台诊断归档--只读查看器) |
| `run` | 串联 parse→translate→apply→build→check→reviewer→report，可 `--from` 续跑 |

内部 Python 函数（已从公开 CLI 移除，供 reviewer agent 与脚本调用）：
`babeldoc_tools.layout.render_pages` / `layout.layout_lint` / `layout.layout_locate` /
`layout.dump_text_layer` / `babeldoc_tools.review.backtranslate_check`。

工具字段与阈值：`reference/schemas.md`；出问题先查 `reference/troubleshooting.md`。

## 编排流程（主 Agent 视角）

1. **解析**：`uv run bdt parse <pdf> --workdir <wd> --layout mineru`
   检查返回的 `label_counts` / `skipped_label_counts` —— 作者区/参考文献/图内/表内
   应被跳过（保留原文渲染），图注/表注应进入 `sheet`。
   同时确认覆盖率门禁：未命中 layout 区域的原生字符占比 ≤ 0.5%
   （`<wd>/<pdf名>/layout_coverage.json` 的 `global.uncovered_ratio`；超阈值解析直接中止）。
2. **翻译**：`uv run bdt translate --workdir <wd> --translator "scripts/agy-translator.sh"`
   （提示词 `agents/translator.md`；被调命令从 stdin 读提示词、把译文写到 stdout，
   模型与档位由它自己决定——`scripts/agy-translator.sh` 用 `AGY_MODEL`/`AGY_EFFORT`）。
   没有可调命令时用 `--markdown <文件>` 导入已有译文，或 `--prompt-only` 只取提示词
   （写 `agent/prompt.md`，由 Agent 自己翻译后走 `--markdown` 导入）。
3. **写回 + 结构 gate**：`uv run bdt apply --workdir <wd>` → `uv run bdt check --workdir <wd>`
   - `blockers` 里的 id → `uv run bdt translate --workdir <wd> --ids P01-003,P01-007 --feedback "..."` → 回到本步（**≤2 轮**）；
   - `warnings` 里的高风险段 → 回译校验（`review.backtranslate_check`，相似度 < 0.55 判为重译对象）；
   - `verdict=pass` 才继续（warnings 允许携带）。
4. **重排 + 渲染**：`uv run bdt build --workdir <wd> --dual`（`--dual` 出拼宽双语）→
   渲染首页、图表密集页、表格页、末页 PNG（`--render 1,5,12`）。
   LaTeX bbox 排版**默认开启**（正文段在 MinerU bbox 内用 XeLaTeX 两端对齐重排；
   缺 XeLaTeX/字体时自动回退）；传 `--no-latex-bbox` 关闭，关闭时输出与旧渲染路径
   逐字节一致。批编译与 stamp 缓存无开关恒开。
5. **审查（reviewer 角色）**：三份角色提示词——`agents/reviewer-protocol.md`
   （结构/协议/跳过语义）、`agents/reviewer-fidelity.md`（语义与漏译，回译）、
   `agents/reviewer-layout.md`（版式，数据源 `layout_lint.json` + PNG）。
   审查输入是 `agent/` 下的结构化产物（`check` 三块结论 + apply/几何/重建报告），
   清单见 `reviewer-protocol.md`。findings 汇总成一份清单（每条含 `id`/`kind`/
   `evidence`/`action`），输出契约见下节。
6. **排版迭代**（≤2 轮）：备份 `agent/layout_overrides.json` → 用 `agents/layout-fixer.md`
   决策出 patch → `uv run bdt layout-set --patch '{...}'` → `uv run bdt build` →
   `uv run bdt check` 复核。变差就还原备份；每轮只改必要字段，**单轮 ≤8 段**。
7. **收尾**：`uv run bdt report --workdir <wd>` → `FINAL_REPORT.md`（apply 指标 /
   verdict / lint 前后对比 / 遗留项），交付 mono + dual + render PNG。

### 一条命令闭环（`bdt run`）

`uv run bdt run <pdf> --workdir <wd> [--translator <cmd>] [--reviewer <cmd>] [--dual]`
串联 `parse → translate → apply → build → check → review → report`：

- 无 `--reviewer` 时停在 review 并以 `waiting_for_reviewer` 失败（exit 1）——质量门禁
  不把"没人审查"当成功；
- `check` 用 `--strict` 语义：verdict 非 pass 时仍跑完 reviewer 与 report，但整体 exit 1；
- reviewer 返回 `needs_fix` 时 findings 映射成 `actions` JSON（`bdt translate --ids` /
  `bdt layout-set`），Agent 执行后 `bdt run --from apply` 续跑；
- 每阶段完成标记与关键输入 sha256 记进 `agent/run_state.json`；上游被改动会报
  `stale_upstream` 并给出 `suggested_from`。

### reviewer 输出契约（U3/U4 最小 schema）

reviewer 只在 stdout 输出一个 JSON 对象：

```json
{"verdict": "pass" | "needs_fix",
 "findings": [
   {"id": "<段落 id 或 <页号>:<链接逻辑 id>>",
    "kind": "retranslate" | "layout",
    "sev": "P0|P1|P2",
    "page": 1,
    "evidence": "<工具输出片段/坐标/文本>",
    "action": "translate --ids <id> | layout-set <patch> | accept:<理由>"}]}
```

- `verdict` 只能是 `pass` / `needs_fix`；`findings` 必须存在（可为空数组）；
- 每条 finding 必须带 `id` / `kind`（`retranslate` 或 `layout`）/ `evidence` / `action`；
  缺字段会让 `run` 以 `reviewer_invalid_json` 失败。

## 排版微调杠杆（`bdt layout-set`）

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

- [ ] `bdt check`：`verdict=pass`，`apply_ok=true`，`fallback=0`；
- [ ] 页数/目录条目与原文一致；`links_mono ≥ 原文`、`links_dual ≈ 2×`；
- [ ] `link_audit.json`：`uri_set_match=true`、`unresolved` 已逐条确认（纯图形链接可接受）；
- [ ] LaTeX bbox 构建（默认开启；`--no-latex-bbox` 关闭）：
      - [ ] 应用率 `applied/eligible ≥ 95%`（eligible = 正文标签 ∧ 已翻译 ∧
            源行数 ≥2 ∧ 非旋转页 ∧ 不压水印；`experiments/acceptance_latex.py`）；
      - [ ] applied 段非末行 fill ≥ 0.98 的行占比 ≥ 99%（`lines_nonfinal_merged_*`，
            基线口径并列上报）；
      - [ ] 贴片文本层 vs 期望纯文本零差异（容差口径 100%，严格计数并列上报）；
      - [ ] `latex_bbox_report.json` 的 `reverted=false`、`links.uri_set_match=true`、
            `decisions[]` 的 fallback 原因可解释（`compile:` / `text-mismatch` /
            `fragment-source-missing` 等只影响该段）；
      - [ ] `experiments/render_compare.py` 并排目视首页/公式页/末页/随机 2 页：
            两端对齐、无压字、无重复文本层；
      - [ ] 显式关闭（`--no-latex-bbox`）时输出与旧默认构建逐字节一致。
- [ ] 目录页：`toc.json` 条目数 == `anchors.json` 的 `toc_entry` 行数 == 书签数；
- [ ] 公式：`alignment.json` 的 `inline_equation_matched` 覆盖绝大多数 span
      （未对齐的应能解释为图内/代码块公式）；
- [ ] `placeholder_leftovers_* = 0`、`p1_title_shrink = 0`；
- [ ] `layout_lint`：`P0 = 0`；P1 全部有结论（已修 or 明确 accept）；
- [ ] `formula_splice`（P2）等已知解析层限制已列入遗留项，不得当作翻译缺陷重译；
- [ ] 渲染首页/图表密集页/表格页/末页，人工确认无压字、无豆腐块；
- [ ] `FINAL_REPORT.md` 落盘，遗留项逐条写明原因。

## 已知边界

- **布局后端**：`mineru`（默认，云端 API）或 `paddle`（本地 PP-DocLayoutV3/PaddleOCR-VL）；
  无 token 时用 `--mineru-json` / `--mineru-cache-key` 回放缓存布局。
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
