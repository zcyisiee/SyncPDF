# Task: P5 — 回归防线补齐与三篇论文真实验收

## Objective

补齐计划要求的全部新单测（fusion alignment / source geometry / renderer
batch / tex template / bbox 追加项），跑三篇论文完整回放验收
（extract 缓存 → apply 现有译文 → reconstruct default/latex × mono/dual），
度量达标（应用率 ≥95%、非末行 fill ≥0.98 占比 ≥99%、text_diff=0、gates
全 pass），更新文档并提交结果 JSON 到 `docs/layout-hypothesis/out/acceptance/`。

## Context

分支 `feature/latex-bbox-layout`（P1–P4 已合入并逐项目验收 commit）。
通读现有 `tests/test_latex_bbox.py`、`tests/test_latex_bbox_links.py`、
P1–P4 各 brief 与其 decisions/acceptance 报告；验收工具
`experiments/toolchain_gates.py`、`experiments/acceptance_latex.py`、
`experiments/render_compare.py`（P0 产出）。

三篇回放 workdir（只读源）：`/tmp/babeldoc-latex-acceptance/{2026-f1872,deepseek-v4,lawbench}/workdir`
（含 `agent/state.pkl`、`agent/translated.jsonl`、MinerU 缓存）。
日常回放 = extract（MinerU 缓存）→ apply 现有 translated.jsonl → reconstruct。
DONE 标准（用户已批准）：
- 应用率 ≥ 95%（分母 eligible = 正文标签 ∧ 已翻译且译文≠原文 ∧ 源行数 ≥2 ∧
  非旋转页 ∧ 不压水印）；
- applied 段非末行 fill ≥ 0.98 的行占比 ≥ 99%；
- 贴片文本层与译文纯文本零差异；
- `toolchain_gates` 全过；pytest 基线（308+新增 passed / 7 既有失败不变）；
- 每篇首页、公式页、末页 + 随机 2 页目视（render_compare PNG 路径留给主
  Agent Read 目视，本任务只产 PNG）。
- 上游"源文含 `{v1}` 垃圾致 LLM 拒译"只记录，不处理。

## Deliverables

1. **P5-1 单测**：新增
   - `tests/test_latex_fusion_alignment.py`：拒绝 span 文本不一致 / 混杂
     layout_id / span 复用；一致时成功；
   - `tests/test_latex_source_geometry.py`：翻译前采集、缩进符号、pitch、
     旧 workdir 兜底（P3 已建则补全）；
   - `tests/test_latex_renderer_batch.py`：marker 归属、坏段隔离、状态机
     顺序、逐页 fit（P4 已建则补全）；
   - `tests/test_latex_tex_template.py`：字体、断词指令、bp 单位（P3 已建
     则补全）；
   - `tests/test_latex_bbox.py` 追加：未翻译跳过（若 P1 未覆盖）、已贴片
     字符不发射（P1）、下扩受布局区约束（P3）、prepare/stamp 顺序（P1）。
   清点计划清单逐项打勾，缺什么补什么；不为凑数重复造用例。
2. **P5-2 三篇回放**：每篇 extract（缓存）→ apply 现有译文 → reconstruct
   default/latex × mono/dual；跑 `toolchain_gates` + `acceptance_latex.py`；
   渲染首页/公式页/末页/随机 2 页 PNG 到 `/tmp/p5-final/<paper>/renders/`；
   全部指标写入结果 JSON，提交到 `docs/layout-hypothesis/out/acceptance/`
   （轻量 JSON/MD，PNG/PDF 不入库）。
3. **P5-3 全链路（可选，若环境可用）**：2026-f1872 用 agy
   `gemini-3.8-flash-low` 重跑 extract→batch_translate→apply→reconstruct
   （`python3 experiments/batch_translate.py <workdir> --model gemini-3.8-flash-low --effort low`），
   重复 P5-2 度量。agy/网络不可用时明确报告跳过原因，不阻塞其余交付。
4. **P5-4 文档**：更新 `docs/layout-hypothesis/ACCEPTANCE.md`（新指标与
   目录约定）、`docs/toolchain/*` 相关页面、`skills/document-translate/SKILL.md`
   验收清单；确保与实现一致。

## Constraints

- 只提交轻量结果（JSON/MD）；PNG/PDF/TeX 缓存不入库。
- 不修改三篇既有 workdir 的源数据（回放到 /tmp/p5-final/）。
- 若 `fragment` 编译失败率高导致应用率不达标：按计划回到 P2-5 调整（在
  报告中给出数据与建议，不要自行大改 P2 逻辑）。
- 不 commit；不引入新依赖；7 个既有失败测试不得变多。

## Validation

```bash
python3 -m pytest tests/ -q        # 308+新增 passed / 7 既有失败
python3 experiments/toolchain_gates.py <每篇回放 workdir>
python3 experiments/acceptance_latex.py <每篇回放 workdir> <mono_pdf>
```

## Report back

- 单测清单对照表（计划项 → 文件/用例名 → 状态）；
- 三篇 × (default/latex) × (mono/dual) 的 gates 与 acceptance 指标汇总表
  （应用率/fill 占比/text_diff/耗时/体积）；
- P5-3 执行情况（或跳过原因）；
- PNG 清单路径（主 Agent 目视用）；
- 文档更新 diff 摘要；偏离与理由。
