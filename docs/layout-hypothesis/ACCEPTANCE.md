# LaTeX bbox 排版：回归证据目录约定

本目录（`docs/layout-hypothesis/`）只保留**轻量结论与可复现脚本**：

- `FINAL.md` / `experiment-report.md` / `recon.md` — 实验结论与证据报告；
- `out/*.json` — 实验的定量证据（编译通过率、fill 率、坐标闭环等），
  大型 PDF/PNG 中间产物已删除，可用 `scripts/` 重新生成；
- `out/acceptance/` — 三篇论文真实翻译验收的轻量结果（JSON/Markdown）；
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

## LaTeX bbox 排版验收（默认开启；`--no-latex-bbox` 对照组）

### 翻译协议（新论文验收从 md 协议开始）

新论文的全链路验收用 **md 整篇协议**（一次模型调用看到全文，术语/语气全局一致）：

```bash
# 1) 解析 → 连续 Markdown + 锚点 + state.pkl（含 LaTeX bbox 源行几何）
python3 -m babeldoc.tools.agent md-extract <paper>.pdf --workdir <wd> \
    --mineru-cache-key <sha256>   # 或 --mineru-json / MINERU_API_TOKEN
# 2) 单次整篇翻译 + 漏行补译 + md-apply 写回（到写回为止，reconstruct 分开跑）
python3 experiments/markdown_translate.py <wd> --model gemini-3.8-flash-low \
    --effort low --skip-reconstruct
# 3) 两次 reconstruct + 门禁 + 度量（同下面的回放命令）
```

旧 sheet 分批协议（`extract` + `experiments/batch_translate.py`）保留兼容，
不再用于新验收。md 协议的 `debug_id` 是确定性的（`P01-005`），同一 PDF 重复
`md-extract` 得到相同 id 集合，回放时可直接复用 `agent/translated.jsonl`。

### 回放方式（不要重新 extract）

legacy `extract` 每次解析会给段落分配**随机 `debug_id`**，因此「重新 extract →
复用旧 `translated.jsonl`」会因 id 不匹配而全部落空（`unknown_ids`）；
`md-extract` 的确定性 id 没有这个问题。日常回放与最终验收统一用：

```bash
cp -r /tmp/babeldoc-latex-acceptance/<paper>/workdir /tmp/p5-final/<paper>/workdir
# default 对照组（LaTeX bbox 默认开启，需显式关闭）
python3 -m babeldoc.tools.agent reconstruct /tmp/p5-final/<paper>/workdir --no-latex-bbox --dual \
    --output-dir /tmp/p5-final/<paper>/out-default
# latex 构建（默认即开启，--latex-bbox 可省）
python3 -m babeldoc.tools.agent reconstruct /tmp/p5-final/<paper>/workdir --latex-bbox --dual \
    --output-dir /tmp/p5-final/<paper>/out-latex
python3 experiments/toolchain_gates.py /tmp/p5-final/<paper>/workdir
python3 experiments/acceptance_latex.py /tmp/p5-final/<paper>/workdir <mono.pdf> \
    --dual-pdf <dual.pdf>
python3 experiments/render_compare.py <default.mono.pdf> <latex.mono.pdf> \
    --pages 1,<公式页>,<末页> -o /tmp/p5-final/<paper>/renders
```

`latex_bbox_report.json` 落在 `<workdir>/<pdf名>/`；`acceptance_latex.py` 默认
把度量写到 `<workdir>/acceptance/`。缓存落在 `<workdir>/<pdf名>/latex_cache/`
（key = 模板版本 + 字体签名 + 请求内容），二次回放近乎零编译。

### 指标定义（DONE 判定口径）

| 指标 | 口径 | 产物字段 |
|---|---|---|
| eligible（应用率分母） | 正文标签 ∧ 已翻译且译文≠原文 ∧ 源行数 ≥2 ∧ 非旋转页 ∧ 不压水印 | `acceptance_latex.json.eligible` |
| 应用率 | `applied / eligible`，要求 **≥ 95%** | `application_rate` |
| 非末行填充率 | applied 段贴片文本层非末行 fill ≥ 0.98 的行占比，要求 **≥ 99%**；判定用 `lines_nonfinal_merged_*`（视觉行合并 + 首行缩进归一），`lines_nonfinal_*`（未合并基线口径）并列上报 | `lines_nonfinal_merged_ge_threshold / lines_nonfinal_merged_total` |
| 文本层零差异 | 贴片文本层 vs 期望纯文本（译文去标记 + `text` 类片段原文），容差口径须 **100%**；严格字符级 `applied_paragraphs_strict_zero_diff_non_formula` 作为门槛并列上报 | `text_diff_zero_ratio_non_formula` |
| 门禁 | `toolchain_gates.py` 无 `fail`（md 协议下五项全可判定；本篇 2512.08296v3 无目录页，`toc_integrity` 合法降级 `not_available`。旧 sheet 流程 `extract → apply → reconstruct` 缺 `anchors.json`，`toc_integrity` / `protected_tokens` / `protocol` 降级 `not_available`，仅作历史基线） | `toolchain_gates.json` |
| 目视 | 每篇首页、公式页、末页 + 随机 2 页 `render_compare.py` 并排 PNG | `/tmp/p5-final/<paper>/renders/` |

### 已知限制（P5 台账）

- `2026-f1872` 有 7 段 `text-mismatch` 回退（xeCJK 字符类 + ToUnicode 码点映射，
  实测 5 种字体组合无解），应用率上限 95.33%，仍满足 ≥95%；
- `HZpip`（7 个 MinerU 片段）`fill_after≈0.07`、`aG4M3≈0.22`：公式密集段盒内行
  填充偏低（片段尺寸范围问题），属已知限制；
- 少量 applied 段 `fill_after` 为 `None`（fragment 密集段文本层无法量测）；
- `text` 类公式片段的粗/斜样式未随 `escape_latex` 入 body（源样式丢失）；
- 上游「源文含 `{v1}` 垃圾致 LLM 拒译」只记录不处理。
