# Task: P0 — 逐段决策报告 + 验收度量脚本 + 视觉对比脚本

## Objective

在不改变产品行为的前提下，为 LaTeX bbox 排版补齐三类观测工具：
(1) `latex_bbox_report.json` 的逐段 `decisions[]` 决策留痕；
(2) 独立验收脚本 `experiments/acceptance_latex.py`（eligible 集、应用率、fill 分布、贴片文本层 diff）；
(3) 视觉对比脚本 `experiments/render_compare.py`（default/latex 并排 PNG）。
三者都是后续 P1–P5 验收的度量基础设施。

## Context

分支 `feature/latex-bbox-layout`。核心模块在
`babeldoc/format/pdf/document_il/backend/latex_bbox/`（capability/fusion/overlay/renderer，
共 ~1500 行，先通读 `overlay.py` 与 `renderer.py`）。

三篇论文回放 workdir 已存在（只读，勿改）：
`/tmp/babeldoc-latex-acceptance/{2026-f1872,deepseek-v4,lawbench}/workdir`，
其中 `<paper>/latex_bbox_report.json` 是当前报告样例；基线数据：
DeepSeek 352 段仅 28 applied（2026-f1872 5/195、lawbench 1/125）。

关键现状：
- `overlay.py` `_select_candidates`（约 390–475 行）做选择性替换门禁；
  `write_report` 写 `working_dir/latex_bbox_report.json`。
- `overlay.measure_line_fill` 已存在（P0-3 复用）；报告路径约定见
  `docs/layout-hypothesis/ACCEPTANCE.md`。
- 单测基线：`python3 -m pytest tests/ -q` = 308 passed / 7 failed，
  7 个失败是 `test_mineru_doclayout_adapter.py` / `test_provider_ir.py`
  缺 fixture 的既有环境失败——不要修、不要变多。
- 页码约定：`page.page_number` 是 0-based PDF 页索引。

## Deliverables

1. `decisions[]`：`overlay.py` 的 `write_report` 增加逐段决策数组，每个元素
   至少含 `debug_id/page/label/reason/fuse_kinds/fill_before/fill_after/font_scale/
   lead/attempts`（未到该阶段的字段为 null）。reason 使用现有 fallback_reasons
   词表 + `applied`。确保 DeepSeek 回放可查 3oVax（摘要段）与 B2Rrm 的决策；
   报告体积注意分段控制（只记录 body 类候选段，不记水印/页码）。
2. `experiments/acceptance_latex.py`：CLI `python3 experiments/acceptance_latex.py
   <workdir> <mono_pdf>`，输出 JSON+MD 到 workdir：
   - eligible 集（正文标签 ∧ 已翻译且译文≠原文 ∧ 源行数 ≥2 ∧ 非旋转页 ∧ 不压水印）；
   - 应用率 = applied / eligible；
   - applied 段非末行 fill 分布（复用 `measure_line_fill`，从贴片文本层提取行）；
   - 贴片文本层 vs 译文纯文本 diff（每段：`text_diff` 字符级计数）；
   - 耗时、PDF 体积。对三篇现有产物跑通并产出基线 JSON（DeepSeek 基线
     applied=28/eligible≈211）。
3. `experiments/render_compare.py`：CLI 指定页码 + 两个 PDF（default/latex 产物），
   并排渲染 PNG 保存到 workdir `renders/`（不提交 PNG）。
4. `tests/test_latex_bbox.py` 追加 decisions 报告结构的单测（最小 IL fixture，
   覆盖 applied 与典型 fallback reason 两类决策记录）。

## Constraints

- P0 不改任何产品行为路径（`decisions[]` 只是报告字段）。
- 不碰 `/tmp` 下既有 workdir 内容（读可以，验收脚本输出写到 workdir 的
   `acceptance/` 子目录或 `docs/layout-hypothesis/out/acceptance/` 由主 Agent 决定，
   脚本本身只写传入的输出目录参数）。
- 不引入新第三方依赖（pymupdf 已有）。
- AGENTS.md：leaf worker，不再委派，不 commit。

## Validation

```bash
python3 -m pytest tests/test_latex_bbox.py -q
python3 -m pytest tests/ -q          # 308+新增 passed / 7 既有失败
python3 experiments/acceptance_latex.py /tmp/babeldoc-latex-acceptance/deepseek-v4/workdir /tmp/babeldoc-latex-acceptance/deepseek-v4/out-latex/*.pdf
python3 experiments/render_compare.py --help
```

## Report back

- 修改/新增文件清单与目的；
- 验证输出摘要 + 三篇基线数字（eligible/applied/fill 分布/text_diff）；
- decisions[] 对 3oVax、B2Rrm 的实际样例（从 DeepSeek 回放报告摘录）；
- 偏离与理由。
