# Task: P4 — 性能：整文档轮次制批编译与缓存持久化

## Objective

把逐段 subprocess 编译改为整文档轮次制批编译（`BatchStampRenderer`），
使 DeepSeek 51 页文档 LaTeX 额外耗时 ≤ 30 s；缓存持久化到 workdir；
批超时与坏段隔离。

## Context

分支 `feature/latex-bbox-layout`（P1–P3 已合入：full 门禁、bp 单位、三级
融合、源几何、字体/断词/缩进/行距/下扩）。通读
`babeldoc/format/pdf/document_il/backend/latex_bbox/renderer.py`
（`BboxStampRenderer.render_many`、缓存 72–80 与 184–197 行、
`_measure_fit`）、`overlay.py`。

spike 结论（/tmp/latex-spike，必须遵守）：
- 单文档逐页 `\pdfpagewidth/\pdfpageheight` 有效，但 `geometry` 的
  `\newgeometry` **不能改纸张尺寸**（第 2 页版心仍 300bp）→ 批编译须逐页
  显式设 `\hsize/\vsize/\textwidth/\textheight/\columnwidth/\linewidth`；
  **不用 `\newgeometry`**。
- ~50 段/块 × CPU 并行（`latex_max_compile_workers`，P1 后默认
  `min(cpu,16)`，本机 18 核 → 16）。
- 一期并发 = CPU 核数（18）。

验收基线：DeepSeek 回放 200+ 段在 P3 后的 compile 秒数（decisions/
报告 `compile` 字段）为对比基准（P3 实测：DeepSeek 350s CPU / wall、
f1872 284s、lawbench 177s——单段编译是主要耗时，批编译目标 DeepSeek
额外耗时 ≤ 30s，即接近消除编译开销）。单测基线 385 passed / 7 既有失败。

P3 审查遗留（收养或跟踪）：
- **DONE fill 口径已裁定**（主代理）：P5 验收用**校正口径**
  （`lines_nonfinal_merged_*`，视觉行合并 + 首行缩进归一），基线口径
  （`lines_nonfinal_*`）并列上报供向后对比。理由：P3 起首行缩进复刻源
  排版与行内数学上下标会让基线口径系统性低估（deepseek 0.8875 vs
  校正 0.9951），这不是排版缺陷而是度量口径滞后。
- f1872 的 7 段 `text-mismatch`（xeCJK 字符类 + ToUnicode 码点映射，
  实测 5 种字体组合无解）已裁定安全回退，应用率 95.3% 为 P5 起始基线，
  若 P4 批编译不改善则接受（与 DONE ≥95% 的差距交 P5 台账）。
- HZpip 0.069 / aG4M3 0.221（公式密集段 fill）：批编译状态机的行距
  ±10% 与下扩重试可能部分改善；不改善则记录为已知限制。

## Deliverables

1. **P4-1 轮次制批编译**：新
   `babeldoc/format/pdf/document_il/backend/latex_bbox/renderer_batch.py`
   `BatchStampRenderer`：
   - 每轮把待定段拼一份 tex，每段一页，逐页显式
     `\pdfpagewidth/\pdfpageheight/\hsize/\vsize/\textwidth/\textheight/\columnwidth/\linewidth`；
   - 段前后 `\message{@@S n@@}/{@@E n@@}` 标记归属 Overfull/`!` 错误行；
   - `-interaction=nonstopmode` 不加 `-halt-on-error`；
   - 每段状态机：源字号+源行距 → 行距 ±10% → 下扩 → ×0.95；≤3 轮后
     剩余段回退单段渲染（`BboxStampRenderer`）；
   - `_measure_fit` 按页号取对应段测量；
   - `overlay.py` 切换到批渲染器（保留单段渲染器作为回退）。
2. **P4-2 缓存持久化**：key 加字体/模板版本；落盘
   `working_dir/latex_cache/`；二次 reconstruct cache_hits > 0。
3. **P4-3 超时与坏段隔离**：批超时 = 45 s + 0.2 s×段数；注入坏段只该段
   回退，其余段正常。
4. `tests/test_latex_bbox.py` 或新 `test_latex_renderer_batch.py`：marker
   归属、坏段隔离、状态机顺序、逐页 fit（xelatex 缺失 skipif 的部分用
   monkeypatch 模拟编译输出）。

## Constraints

- 批编译结果与单段模式逐段一致（同一回放 applied 集合不变）；
  无 `horizontal-overflow`。
- 默认关闭路径零行为变化；不碰 `/tmp` 既有 workdir；不 commit；
  不引入新依赖。
- marker 归属必须先做 20 段真实 spike 压测再定稿（报告粘贴 spike 结果）。

## Validation

```bash
python3 -m pytest tests/ -q
# DeepSeek 回放计时：额外耗时 ≤ 30 s（对比 P3 基线）
time python3 -m babeldoc.tools.agent reconstruct /tmp/babeldoc-latex-acceptance/deepseek-v4/workdir --latex-bbox --dual --output-dir /tmp/p4-verify/deepseek-out
python3 experiments/acceptance_latex.py /tmp/babeldoc-latex-acceptance/deepseek-v4/workdir <mono_pdf>
# 二次 reconstruct 验证 cache_hits > 0
```

## Report back

- 文件清单与目的；验证输出摘要；
- DeepSeek 计时对比表（P3 单段 vs P4 批编译；轮数/每轮段数/缓存命中）；
- 20 段 spike 的 marker 归属结果；坏段隔离单测结果；
- 与单段模式 applied 集合一致性证据；偏离与理由。
