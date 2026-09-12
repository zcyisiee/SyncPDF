# Task: P2 — 融合重构：三级 {vN} 解析 + 一致性校验 + 裁片段嵌入

## Objective

根因 3（融合内容级错误：`alleviati$L\times n_{win}$ge`）与根因 6（42%–65%
段落含 BabelDOC 启发式"公式"占位符而无 MinerU LaTeX 源）：建立
`text → mineru → simple_math → fragment` 四级分类（任何不确定即降级，
不猜测）；alignment 一一匹配 + 原生字符一致性校验替换 IoU 就近取；新增
Unicode→LaTeX 转写模块；`fragment` 级从源 PDF 裁区域嵌入。

## Context

分支 `feature/latex-bbox-layout`。通读
`babeldoc/format/pdf/document_il/backend/latex_bbox/fusion.py`
（`fuse_paragraph` 186–266 行、`escape_latex`/`parse_segments`/
`iter_composition_units`/`_style_flags`）、`inline_math_protector.py`
（97、204 行）、`renderer.py`、`capability.py:20`；以及
`provider_alignment.to_il_box` / `_span_text_consistent`（复用）。

证据（来自 alignment.json 与三篇统计）：
- B2Rrm 段：PdfFormula 的 `formula_layout_id`=7/8/9 是 protector 追加的
  MinerU 保护区；MinerU 把 "ng stora" 误判为 inline_equation（该页
  `span_text_mismatch=24`），fusion 用 IoU 就近取了 OCR LaTeX。
- `{vN}` 成分统计：2026-f1872 281 个中 261 纯文本 / 15 简单符号 / 4 MinerU；
  DeepSeek 240：75/88/77；LawBench 132 全纯文本；带曲线 0。
- 回放 workdir（只读）：`/tmp/babeldoc-latex-acceptance/{2026-f1872,deepseek-v4,lawbench}/workdir`，
  各 `<paper>/agent/source/mineru/alignment.json` 可查 span_id/一致性。
- 单测基线 308 passed / 7 既有失败不变；`test_provider_alignment` 不得变。

用户已批准的决策（照做）：
- 匹配基于 alignment 一一对应 + 原生字符一致性校验；`InlineMathProtector`
  保护行为不动（只加字段）；未翻译段不走 LaTeX（P1 已做）。
- 分类顺序 `text → mineru → simple_math → fragment`；任何不确定即降级。
- 片段来源是**源 PDF**（workflow 用 `state.pkl` inputs，high_level 用
  `input_file`），不是 mono；基线偏移取 `PdfFormula.y_offset`。

## Deliverables

1. **P2-1 片段分类器**：`fusion.py` 新函数
   `classify_formula(pdf_formula, alignment) -> "text"|"simple_math"|"mineru"|"fragment"`：
   - `text` = 原生字符仅 `[]()•·_-–—A-Za-z0-9,.;:+=<>/%*|` 与空格，且无 curve/form；
   - `simple_math` = 可转写 Unicode 数学（U+1D400–1D7FF、希腊、上下标、
     `×÷±→↓≤≥≠∞`）；
   - `mineru` = 见 P2-4 一致性校验通过；
   - 其余 `fragment`。三篇 IL 统计应与根因表一致（写入报告）。
2. **P2-2 文本去公式化**：`text` 类按原生字符 `escape_latex` + 样式
   （粗/斜）入 body；LawBench 59 段全部融合成功。
3. **P2-3 Unicode→LaTeX 转写**：新模块
   `babeldoc/format/pdf/document_il/backend/latex_bbox/unicode_math.py`：
   映射表（`𝑛`→`$n$`、`𝑛win`→`$n_{\mathrm{win}}$`、`×`→`\times` …）；
   未知字符降级 `fragment`。DeepSeek 88 个无源 math ≥ 90% 可转写。
4. **P2-4 alignment 一一匹配 + 一致性校验**：protector 在
   `protected_inline_math` 追加 `span_id/latex/text_consistent` 字段（来自
   `align_page` 的 `_span_text_consistent`；只加字段，不改保护行为）；
   fusion：PdfFormula 全部字符 `formula_layout_id` 相同且指向一个保护区 →
   span_id → 要求 `matched ∧ text_consistent` 且 span 未被复用；原生字符为
   ≥3 连续字母词片 → 判 `text`；MinerU LaTeX 去命令后字母数字须与原生字符
   相容（子序列 ≥ 0.6）否则 `fragment`；**删除 IoU `lookup`**。
5. **P2-5 裁片段嵌入**：`fragment`：从源 PDF 按源 box 外扩 0.5bp 裁单页 PDF
   存 workdir（pymupdf `show_pdf_page(clip=…)`）；模板
   `\raisebox{<y_offset>bp}{\includegraphics[height=<h>bp]{f.pdf}}`；模板加
   `graphicx`；`capability.py` `REQUIRED_PACKAGES` 更新。
6. **P2-6 零差异校验**：`expected_text` = 译文去标记 + `text` 片段原文 +
   `simple_math` 原文；`renderer.py` `_measure_fit` 改全文归一化比较
   （`fragment` 区域除外）；三篇 `text_diff=0`（用 P0 的
   `acceptance_latex.py` 度量）。
7. `tests/test_latex_bbox.py`（或新 `test_latex_fusion_alignment.py` 雏形）：
   四类分类单测；拒绝 span 文本不一致 / 混杂 layout_id / span 复用；
   一致时成功；裁片段 fixture（含曲线的最小 PDF）编译通过。

## Constraints

- `test_provider_alignment` 与 protector 保护行为不变。
- `InlineMathProtector` 只加字段。
- 不碰 `/tmp` 既有 workdir；不 commit；不引入新依赖。
- 分类顺序硬约束：`text → mineru → simple_math → fragment`。

## Validation

```bash
python3 -m pytest tests/ -q
python3 -m pytest tests/test_latex_bbox.py tests/test_latex_bbox_links.py tests/test_provider_alignment.py -q
# 三篇回放对比（输出到 /tmp/p2-verify/，不碰原 workdir）
python3 -m babeldoc.tools.agent reconstruct <workdir> --latex-bbox --dual --output-dir /tmp/p2-verify/<paper>
python3 experiments/acceptance_latex.py <workdir> <mono_pdf>
```

## Report back

- 文件清单与目的；验证输出摘要；
- 三篇 `{vN}` 分类统计表（对照根因表：261/15/4、75/88/77、132/0/0）；
- B2Rrm 三个片段判 `text` 的 decisions 摘录；DeepSeek 77 个真公式仍匹配的
  证据；text_diff=0 证据；
- 偏离与理由。
