# LaTeX bbox 排版：回归证据目录约定

本目录（`docs/layout-hypothesis/`）只保留**轻量结论与可复现脚本**：

- `FINAL.md` / `experiment-report.md` / `recon.md` — 实验结论与证据报告；
- `out/*.json` — 实验的定量证据（编译通过率、fill 率、坐标闭环等），
  大型 PDF/PNG 中间产物已删除，可用 `scripts/` 重新生成；
- `scripts/` — 可复现实验脚本（纯离线，不触碰产品代码）。

## 真实翻译验收产物（不进 Git）

三篇论文（`2026-f1872-paper.pdf`、`DeepSeek_V41_Tech_Report.pdf`、
`Fei 等 - 2023 - LawBench … .pdf`）的真实翻译验收在**临时目录**执行
（建议 `/tmp/babeldoc-latex-acceptance/<paper>/`），每个 workdir 固定保存：

| 文件 | 说明 |
|---|---|
| `manifest.json` | 论文、模型、配置、运行时间戳 |
| `agent/` 全套中间产物 | sheet / translated / layout_geometry / link_snapshot 等 |
| `reconstruct_report.json` | reconstruct 返回的统计（含链接指标） |
| `toolchain_gates.json` | `babeldoc_tools` 验收门禁输出 |
| `review-findings.md` | 协议 / fidelity / layout 审查结论 |

**最终只把以下轻量结果提交回仓库**（放 `out/acceptance/`，均为 JSON/Markdown）：

- 每篇论文的结果汇总（页数、段落、协议 violations、fallback、
  LaTeX applied/fallback、公式融合成功率、链接指标、耗时、PDF 体积）；
- 链接统计（URI 集合差异、remapped/fallback/unresolved、参考文献矩形）；
- 失败样本索引与截图清单（PNG 本体留在临时目录）。

大型 PDF、渲染 PNG、TeX 编译缓存**一律不提交**。
