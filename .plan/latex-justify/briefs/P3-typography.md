# Task: P3 — 版式保真：源几何、字体、断词、缩进、行距、安全扩框

## Objective

根因 4（字体不一致）、5（`\parindent=0`、行距固定 1.5、无 hyphenation、源行
几何在译文回填后丢失）：翻译前采集源行几何；模板字体与产品一致（Noto Serif/
Sans + Source Han Serif/Sans CN）；启用英文断词与容差；复刻首行缩进；行距按
源 baseline pitch 推导；垂直溢出先安全下扩再缩字。

## Context

分支 `feature/latex-bbox-layout`（P1/P2 已合入：full 模式门禁、bp 单位、
`decisions[]` 报告、四级融合分类 text→mineru→simple_math→fragment、
`(cid:N)` 拒绝、源 PDF 裁片段嵌入）。通读
`babeldoc/format/pdf/document_il/backend/latex_bbox/renderer.py`（模板 29–46、
`build_tex`、`_render_uncached`）、`overlay.py`、`capability.py:33-50`；
复用 `layout_geometry._annotate_space` 算法与 `link_snapshot.collect_page_chars`。
关联：`babeldoc/format/pdf/high_level.py`（约 1009 行后）、
`babeldoc/tools/agent/workflow.py`（extract 约 323 行 pickle 前）、
`babeldoc/format/pdf/document_il/backend/pdf_creater.py`。

P2 审查遗留（收养或跟踪）：
- text 类片段样式（粗/斜）未随 `escape_latex` 入 body（P2-2 部分落实）；
  P3-1 字体工作时若成本低可顺带（`FormulaClassification` 携带 style）；
- `HZpip`（7 mineru 片段）`fill_after=0.07`、`aG4M3` `0.22`：公式密集段
  盒内行填充仍差，正是 P3-4 行距推导与 P3-2 断词的靶子；
- 2026-f1872 的 7 段编译失败多为拉丁字形 `\uffff`（默认拉丁字体无
  ToUnicode），预期由 P3-1 `\setmainfont` 收口（这是 P3 验收重点之一：
  f1872 应用率应从 95.3% 回升）；
- 片段 PDF 存临时目录而非 workdir（P2 审查 P2-3）：P3 若重构 workdir
  路径可顺带落盘，非阻塞。

字体证据：产品默认输出拉丁 = Noto Serif Regular/Bold（sans 源则 Noto Sans），
CJK = Source Han Serif CN；TeX 模板当前只设 CJK 且默认 SourceHanSans，拉丁回落
Latin Modern。行距证据：第 4 页 "具体而言" 段缩进、17 行源段按 17 行推导。
spike 结论（/tmp/latex-spike）：fontspec 路径加载 + `babel[english]` 断词 +
`\emergencystretch=1em` 共存编译通过、非末行 fill=1.000。

单测基线以合入 P1/P2 后的实际数字为准（原 308 passed / 7 既有失败）。
回放 workdir（只读）：`/tmp/babeldoc-latex-acceptance/{2026-f1872,deepseek-v4,lawbench}/workdir`。

## Deliverables

1. **P3-0 源行几何前移采集**：新 `capture_source_line_geometry(docs)` 在
   ILTranslator 之前调用（`high_level.py` 约 1009 行后、
   `workflow.py` extract 约 323 行 pickle 前），记录每段
   `line_boxes/first_line_dx`（正缩进负悬挂）/`baseline_pitch/n_lines/
   ascent_top/space_below_pt`（复用 `layout_geometry._annotate_space` 算法 +
   page_layout figure/table/formula 区 + cropbox 底边）；旧 workdir（无该数据）
   用 `page_char_objects` 按源 box 聚类兜底。overlay 选择几何来源时新数据优先。
2. **P3-1 字体一致**：按段落主字体 serif 标志与 `primary_font_family`：
   `\setmainfont` = NotoSerif（Regular/Bold/Italic/BoldItalic）或 NotoSans，
   `\setCJKmainfont` = SourceHanSerifCN 或 SansCN；`capability.py` 探测这些
   字体文件。
3. **P3-2 断词与容差**：模板加 `\usepackage[english]{babel}`、
   `\hyphenpenalty=50 \tolerance=1500 \emergencystretch=1em`、
   `\lineskiplimit=-\maxdimen`、`\XeTeXlinebreakskip=0pt plus 0.3em`、`\url`
   可断；**不用 `\sloppy`**。
4. **P3-3 首行缩进**：`\parindent=first_line_dx`，悬挂用
   `\hangindent/\hangafter`；`\topskip`=ascent。
5. **P3-4 行距推导**：`lead = clamp(baseline_pitch, 1.15fs, 1.6fs)`；fit 失败
   先在 `[0.9,1.1]×lead` 调两步，再进字号缩小（×0.95 有界）。
6. **P3-5 安全下扩**：`vertical-overflow` 时 bbox 高度扩到
   `min(需要, h + space_below − 2bp)`，stamp rect 同步；扩后区域须无他段
   字符/图表（用 `page_char_objects`/布局区校验）；无净空走 shrink。
7. `tests/test_latex_bbox.py` 或新 `test_latex_source_geometry.py`：翻译前
   采集、缩进符号、pitch、旧 workdir 兜底；`test_latex_tex_template.py`
   雏形（字体、断词指令、bp 单位断言）。

## Constraints

- 默认关闭路径零行为变化；`decisions[]` 记录 `lead/attempts/font_scale`。
- 不改旧排版器；不碰 `/tmp` 既有 workdir；不 commit；不引入新依赖。
- 字体文件按路径加载（fontspec），不做系统字体名依赖；缺失时 capability
   warning 并回退，不抛异常。
- P3-5 扩框只允许进入无字符/无图表区域（无双层文本靠构造保证）。

## Validation

```bash
python3 -m pytest tests/ -q
# 三篇回放：对比 P2 后的 fill 分布 / shrink 次数 / 决策报告 lead 字段
python3 -m babeldoc.tools.agent reconstruct <workdir> --latex-bbox --dual --output-dir /tmp/p3-verify/<paper>
python3 experiments/acceptance_latex.py <workdir> <mono_pdf>
python3 experiments/toolchain_gates.py <workdir>
```

## Report back

- 文件清单与目的；验证输出摘要；
- 三篇 shrink 次数前后对比、非末行 fill 分布变化；
- 第 4 页 "具体而言" 段缩进与源一致的证据（render_compare PNG 路径）；
- 17 行源段的 lead 推导样例；下扩受布局区约束的单测结果；
- 偏离与理由。
