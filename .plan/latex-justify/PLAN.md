> 批准后：本计划落盘到 `.plan/latex-justify/PLAN.md` + `briefs/<task-id>.md`（`git add -f` 入库）；
> 按"大项目 → 小任务"用 pi-coding-agent 执行、主 Agent 验收；每个大项目完成后在当前分支中文 commit。

## Context

分支已实现"在 MinerU bbox 内用 XeLaTeX 重排译文并贴回"（`babeldoc/format/pdf/document_il/backend/latex_bbox/`），
但验收产物里译文仍左对齐、右侧参差、英文词被拆开。目标：每个 MinerU bbox 内译文经 LaTeX 两端对齐；保留 BabelDOC 链接/样式；
用 MinerU 行内公式；不回退目录/书签/链接既有优化。

### 根因（代码 + 验收数据 + spike 证实）

| # | 根因 | 证据 |
|---|---|---|
| 1 | **选择性替换门禁挡掉 ~92% 段落**：纯文本段仅 `min_body_fill < 0.85` 才替换，单行段跳过 | `overlay.py:455-462`；DeepSeek 352 段仅 28 applied；截图摘要段 3oVax 在第 1 页，`pages_affected` 不含第 1 页 |
| 2 | **旧排版器无两端对齐**（逐字符贪心左对齐、无 glue），放不下时按字符切词 | `typesetting.py:1427-1609`、`:1097-1106` |
| 3 | **公式融合内容级错误（P0）**：B2Rrm 段融合成 `alleviati$L\times n_{win}$ge`。其 PdfFormula 的 `formula_layout_id`=7/8/9 正是 protector 追加的 MinerU 保护区；MinerU 把 "ng stora" 误判为 inline_equation（该页 `span_text_mismatch=24`），fusion 用 IoU 就近取了 OCR LaTeX，全文校验对未翻译段形同虚设 | `fusion.py:238-266`、`inline_math_protector.py:97`、alignment.json |
| 4 | **字体不一致**：产品默认输出拉丁 = Noto Serif Regular/Bold（sans 源则 Noto Sans），CJK = Source Han Serif CN；TeX 模板只设 CJK 且默认 SourceHanSans，拉丁回落 Latin Modern | pymupdf 统计 default mono 第 4~5 页；`renderer.py:29-46`、`capability.py:33-50` |
| 5 | `\parindent=0`、行距固定 1.5、无 hyphenation；且 `capture_layout_sources` 在译文回填之后运行，此时 composition 已无 `pdf_line`，源行几何丢失 | `renderer.py:40,53`；`overlay.py:48` |
| 6 | **可用段落 42%~65% 含 `{vN}`，绝大多数是 BabelDOC 启发式"公式"**（引文号 `[55]`、`•`、`_`、单个斜体变量 `𝑛`），无 MinerU LaTeX 源 → 全部 fallback | 三篇 `{vN}` 成分：2026-f1872 281 个中 261 纯文本 / 15 简单符号 / 4 MinerU；DeepSeek 240：75/88/77；LawBench 132 全纯文本；带曲线 0 |
| 7 | **单位陷阱**：模板 `paperwidth=…pt`（TeX pt）而 bbox/fit 用 PDF bp，455.67pt 页只有 453.97bp，贴片被拉伸 0.37%、fit 偏宽松 | spike |

### 用户决策（grill 三轮）

1. `--latex-bbox` 开启时所有 ≥2 行正文段（`text/list/figure_caption/table_caption/page_footnote/table_footnote`）默认走 LaTeX，仅失败/几何不安全回退；`title/toc_*/reference/page_number/author/figure/table_text/code` 不走。
2. `{vN}` 三级解析 + 一期就做裁片段：纯文本→文本；简单 Unicode 数学→LaTeX 转写；MinerU 有源且一致性校验通过→MinerU LaTeX；其余→裁源 PDF 区域 `\includegraphics` 嵌入。
3. 匹配基于 alignment 一一对应 + 原生字符一致性校验；`InlineMathProtector` 不动；未翻译段不走 LaTeX。
4. 字体与产品一致；复刻首行缩进；行距按源行数推导；允许英文断词。
5. 溢出：先安全下扩再 Bounded Shrink；旧排版器拆词不改。
6. 一期并发 = CPU 核数（18）；三期整文档批编译，DeepSeek 51 页额外 ≤ 30 s。
7. DONE：应用率 ≥ 95%（分母 eligible = 正文标签 ∧ 已翻译且译文≠原文 ∧ 源行数 ≥2 ∧ 非旋转页 ∧ 不压水印）；applied 段非末行 fill ≥ 0.98 的行占比 ≥ 99%；贴片文本层与译文纯文本零差异；`toolchain_gates` 全过；pytest 308 passed / 7 既有失败不变；每篇首页、公式页、末页 + 随机 2 页目视。
8. 计划文件 `git add -f` 入库；commit 在当前分支。
9. 约定：上游"源文含 `{v1}` 垃圾致 LLM 拒译"只记录；日常回放 = `extract`（MinerU 缓存）→ `apply` 现有 `translated.jsonl` → `reconstruct`；最终验收用 agy `gemini-3.8-flash-low` 重译一篇。

### 规划期 spike 结论（/tmp/latex-spike）

- 单文档逐页 `\pdfpagewidth/\pdfpageheight` 有效，但 `geometry` 的 `\newgeometry` **不能改纸张尺寸**（第 2 页版心仍 300bp，右侧被裁）→ 批编译须逐页显式设 `\hsize/\vsize/\textwidth/\textheight/\columnwidth/\linewidth`。
- fontspec 路径加载 Noto/GoNoto + xeCJK Source Han + `babel[english]` 断词 + `\emergencystretch=1em` 共存编译通过，非末行 fill = 1.000。
- 裁片段：源 PDF `show_pdf_page(clip=源box)` → 小 PDF → `\raisebox{-0.2ex}{\includegraphics[height=…bp]{frag.pdf}}` 内嵌中文句子，两端对齐正常。

---

## 执行方式

```bash
# 子代理（AGENTS.md leaf worker，不再委派，不 commit）
pi -p --no-session --provider deepseek --model deepseek-flash --thinking high \
  @.plan/latex-justify/briefs/<task-id>.md \
  "Execute the attached brief directly as a leaf implementation worker. Do not delegate. Follow AGENTS.md. Validate your changes and report the result."
# 翻译测试代理
python3 experiments/batch_translate.py <workdir> --model gemini-3.8-flash-low --effort low
# 主 Agent 验收
python3 -m pytest tests/ -q
python3 -m babeldoc.tools.agent reconstruct <wd> --latex-bbox --dual --output-dir <out>
python3 experiments/toolchain_gates.py <wd>
python3 experiments/acceptance_latex.py <wd> <mono_pdf>     # P0-3 新增
```

brief 固定结构：`# Task / ## Objective / ## Context / ## Deliverables / ## Constraints / ## Validation / ## Report back`；并行任务用独立 worktree。

---

## 大项目与小任务

### P0 — 基线、诊断与验收工具（不改产品行为）

| 任务 | 目标 | 涉及 | 验收 |
|---|---|---|---|
| P0-1 harness 冒烟 + brief 模板 | pi(CNB) / agy 各跑一次只读任务；`briefs/_template.md` | — | 两 CLI 正常返回 |
| P0-2 逐段决策报告 | `latex_bbox_report.json` 增 `decisions[]`（`debug_id/page/label/reason/fuse_kinds/fill_before/fill_after/font_scale/lead/attempts`） | `overlay.py` `_select_candidates`/`write_report` | DeepSeek 回放可查 3oVax、B2Rrm 决策；单测 |
| P0-3 验收脚本 | `experiments/acceptance_latex.py`：eligible 集、应用率、applied 非末行 fill 分布（复用 `measure_line_fill`）、贴片文本层 vs 译文纯文本 diff、耗时、体积；JSON+MD | 新脚本 | 三篇现有产物基线（DeepSeek 28/211） |
| P0-4 视觉对比脚本 | `render_compare.py`：指定页 default/latex 并排 PNG | 新脚本 | 主 Agent Read 目视 |

**commit：** `增加 LaTeX bbox 逐段决策报告与验收度量脚本`

### P1 — 门禁重写与贴片机制（根因 1、7）

| 任务 | 目标 | 涉及 | 验收 |
|---|---|---|---|
| P1-1 资格规则 + 模式开关 | 候选 = 正文标签 ∧ 源行数 ≥2；删 `line-fill-ok`/`no-body-lines`；新增 `latex_bbox_mode: "full"|"repair"`（默认 full，repair 复现旧行为）+ CLI 选项 | `overlay.py:390-475`、`translation_config.py:277-283`、`main.py:396-431`、`__main__.py` | DeepSeek attempted ≥ 200；repair 模式测试 |
| P1-2 未翻译跳过 | workflow 路径用 `state.pkl` 的 `inputs[debug_id].unicode`、high_level 路径在翻译前记录源文；归一化相等 → `untranslated` | `overlay.py` capture、`workflow.py:515`、`high_level.py:1009` 附近 | B2Rrm reason=`untranslated` |
| P1-3 单位统一 bp | 模板 `paperwidth=%(w).4fbp`，`\fontsize` 按 bp 换算，`_measure_fit` 与 stamp rect 同单位 | `renderer.py:29-46,102-144` | 编译页 rect == bbox（±0.01） |
| P1-4 并发 | `latex_max_compile_workers` 默认 `min(os.cpu_count(),16)` | `renderer.py:149`、config | 200 段压测无 timeout/冲突 |
| P1-5 不发射已贴片段落字符 | `apply_latex_bbox_overlay` 拆 `prepare()`（选段+编译，在 write 内容流循环前）与 `stamp()`；`update_page_content_stream` 跳过 `stamped_ids` 的字符 → 旧路径译文不入流，redaction 仅作兜底（先断言 stamp rect 内文本层为空），`box-expanded-after-typesetting` 门禁改为对扩后矩形做"无他段字符"校验 | `pdf_creater.py:1799-1850,2025`、`overlay.py` | 无双层文本测试；链接多重集/URI 集合仍恒等 |

**commit：** `LaTeX bbox 改为正文段落默认重排：模式开关、未翻译跳过、bp 单位与免 redaction 贴片`

### P2 — 融合重构：三级 `{vN}` 解析 + 一致性校验（根因 3、6）

| 任务 | 目标 | 涉及 | 验收 |
|---|---|---|---|
| P2-1 片段分类器 | `classify_formula(pdf_formula, alignment) -> text/simple_math/mineru/fragment`：`text` = 原生字符仅 `[]()•·_-–—A-Za-z0-9,.;:+=<>/%*|空格` 且无 curve/form；`simple_math` = 可转写 Unicode 数学（U+1D400–1D7FF、希腊、上下标、`×÷±→↓≤≥≠∞`）；`mineru` 见 P2-4；其余 `fragment` | `fusion.py` 新函数 | 单测四类；三篇 IL 统计与根因表一致 |
| P2-2 文本去公式化 | `text` 类按原生字符 `escape_latex` + 样式（粗/斜）入 body | `fusion.py:fuse_paragraph` | LawBench 59 段全部融合成功 |
| P2-3 Unicode→LaTeX 转写 | 新 `unicode_math.py` 映射表（`𝑛`→`$n$`、`𝑛win`→`$n_{\mathrm{win}}$`、`×`→`\times`…）；未知字符降级 `fragment` | 新模块 | DeepSeek 88 个无源 math ≥ 90% 可转写 |
| P2-4 alignment 一一匹配 + 一致性校验 | protector 在 `protected_inline_math` 追加 `span_id/latex/text_consistent`（来自 `align_page` `_span_text_consistent`）；fusion：PdfFormula 全部字符 `formula_layout_id` 相同且指向一个保护区 → span_id → 要求 `matched ∧ text_consistent` 且 span 未被复用；原生字符为 ≥3 连续字母词片 → 判 `text`；MinerU LaTeX 去命令后字母数字须与原生字符相容（子序列 ≥ 0.6）否则 `fragment`；删除 IoU `lookup` | `fusion.py:186-266`、`inline_math_protector.py:97,204`（只加字段，不改保护行为） | B2Rrm 三个片段判 `text`；DeepSeek 77 个真公式仍匹配；`test_provider_alignment` 不变 |
| P2-5 裁片段嵌入 | `fragment`：从源 PDF（workflow 用 `state.pkl` inputs，high_level 用 `input_file`）按源 box 外扩 0.5bp 裁单页 PDF 存 workdir；`\raisebox{<y_offset>bp}{\includegraphics[height=<h>bp]{f.pdf}}`（偏移取 `PdfFormula.y_offset`）；模板加 `graphicx`；`REQUIRED_PACKAGES` 更新 | `fusion.py`、`renderer.py`、`capability.py:20` | 含曲线 fixture 通过；三篇 `fragment` 段 ≥ 90% 编译通过 |
| P2-6 零差异校验 | `expected_text` = 译文去标记 + `text` 片段原文 + `simple_math` 原文；`_measure_fit` 改全文归一化比较（`fragment` 区域除外） | `renderer.py:102-144` | 三篇 `text_diff=0` |

**commit：** `重构公式融合：三级占位符解析、alignment 一一匹配、原生字符一致性校验与源 PDF 片段嵌入`

### P3 — 版式保真：源几何、字体、断词、缩进、行距、安全扩框（根因 4、5）

| 任务 | 目标 | 涉及 | 验收 |
|---|---|---|---|
| P3-0 源行几何前移采集 | 新 `capture_source_line_geometry(docs)` 在 ILTranslator 前（`high_level.py:1009` 后、`workflow.extract:323` pickle 前）记录每段 `line_boxes/first_line_dx(正缩进负悬挂)/baseline_pitch/n_lines/ascent_top/space_below_pt`（复用 `layout_geometry._annotate_space` 算法 + page_layout figure/table/formula 区 + cropbox 底边）；旧 workdir 用 `page_char_objects` 按源 box 聚类兜底 | `overlay.py`、`high_level.py`、`workflow.py`、`link_snapshot.collect_page_chars` | 单测：新旧 workdir 都能得到 n_lines/pitch |
| P3-1 字体一致 | 按段落主字体 serif 标志与 `primary_font_family`：`\setmainfont` = NotoSerif（Regular/Bold/Italic/BoldItalic）或 NotoSans，`\setCJKmainfont` = SourceHanSerifCN 或 SansCN；capability 探测这些文件 | `renderer.py:29-46`、`capability.py:33-50` | 模板断言；渲染对比拉丁/中文字形与 default 一致 |
| P3-2 断词与容差 | `\usepackage[english]{babel}`、`\hyphenpenalty=50 \tolerance=1500 \emergencystretch=1em`、`\lineskiplimit=-\maxdimen`、`\XeTeXlinebreakskip=0pt plus 0.3em`、`\url` 可断；不用 `\sloppy` | `renderer.py` 模板 | shrink 次数下降；单测 |
| P3-3 首行缩进 | `\parindent=first_line_dx`，悬挂用 `\hangindent/\hangafter`；`\topskip`=ascent | `renderer.py:build_tex` | 第 4 页 "具体而言" 段缩进与源一致 |
| P3-4 行距推导 | `lead = clamp(baseline_pitch, 1.15fs, 1.6fs)`；fit 失败先在 `[0.9,1.1]×lead` 调两步，再进字号缩小 | `renderer.py:_render_uncached` | 17 行源段按 17 行推导；shrink 步数下降 |
| P3-5 安全下扩 | `vertical-overflow` 时 bbox 高度扩到 `min(需要, h + space_below − 2bp)`，stamp rect 同步；扩后区域须无他段字符/图表 | `overlay.py`、`renderer.py` | 有净空段不缩字；无净空段走 shrink；无双层 |

**commit：** `LaTeX bbox 版式保真：源几何采集、产品字体、断词、缩进、行距推导与安全扩框`

### P4 — 性能：整文档批编译

| 任务 | 目标 | 涉及 | 验收 |
|---|---|---|---|
| P4-1 轮次制批编译 | `BatchStampRenderer`：每轮把待定段拼一份 tex，每段一页，逐页显式 `\pdfpagewidth/\pdfpageheight/\hsize/\vsize/\textwidth/\textheight/\columnwidth/\linewidth`（**不用 `\newgeometry`**），段前后 `\message{@@S n@@}/{@@E n@@}` 归属 Overfull/`!`；`-interaction=nonstopmode` 不加 `-halt-on-error`；每段状态机：源字号+源行距 → 行距 ±10% → 下扩 → ×0.95；≤3 轮后剩余段回退单段渲染；~50 段/块 × CPU 并行；`_measure_fit` 按页号 | 新 `renderer_batch.py`、`overlay.py` | DeepSeek 额外 ≤ 30 s；与单段模式逐段一致；无 `horizontal-overflow` |
| P4-2 缓存持久化 | key 加字体/模板版本；落盘 `working_dir/latex_cache/` | `renderer.py:72-80,184-197` | 二次 reconstruct cache_hits > 0 |
| P4-3 超时与坏段隔离 | 批超时 = 45 s + 0.2 s×段数；注入坏段只该段回退 | 同上 | 单测 |

**commit：** `LaTeX bbox 整文档轮次制批编译与缓存持久化`

### P5 — 回归防线与真实验收

| 任务 | 目标 | 验收 |
|---|---|---|
| P5-1 单测 | 新增 `test_latex_fusion_alignment.py`（拒绝 span 文本不一致 / 混杂 layout_id / span 复用；一致时成功）、`test_latex_source_geometry.py`（翻译前采集、缩进符号、pitch、旧 workdir 兜底）、`test_latex_renderer_batch.py`（marker 归属、坏段隔离、状态机顺序、逐页 fit）、`test_latex_tex_template.py`（字体、断词指令、bp 单位）；`test_latex_bbox.py` 追加未翻译跳过、已贴片字符不发射、下扩受布局区约束、prepare/stamp 顺序 | `pytest tests/ -q` = 308+新增 passed / 7 既有失败 |
| P5-2 三篇回放 | extract(缓存)→apply 现有译文→reconstruct default/latex × mono/dual；`toolchain_gates` + `acceptance_latex.py`；渲染首页/公式页/末页/随机 2 页，主 Agent 目视 | 应用率 ≥ 95%、fill ≥ 0.98 占比 ≥ 99%、text_diff=0、gates 全 pass、URI 集合 = 源、TOC = 源 |
| P5-3 全链路 | 2026-f1872 用 agy `gemini-3.8-flash-low` 重跑 extract→batch_translate→apply→reconstruct | 同 P5-2 |
| P5-4 文档 | 更新 `docs/layout-hypothesis/ACCEPTANCE.md`、`docs/toolchain/*`、`skills/document-translate/SKILL.md` 验收清单；结果 JSON 提交到 `docs/layout-hypothesis/out/acceptance/` | 文档与实现一致 |

**commit：** `完成 LaTeX 两端对齐排版三篇论文回归与全链路验收`

---

## 关键设计要点（brief 引用）

1. **无双层文本靠构造保证**：已贴片段落的字符不进内容流（P1-5）；redaction 只在断言失败时兜底；扩框只允许进入无字符/无图表区域。
2. **片段来源是源 PDF**，不是 mono；基线偏移取 `PdfFormula.y_offset`。
3. **分类顺序** `text → mineru → simple_math → fragment`，任何不确定即降级，不猜测。
4. **两模式共存**：`repair` 保留旧行为供既有测试/文档复现；默认 `full`。
5. **所有决策留痕**到 `decisions[]`，验收脚本只读报告与 PDF。

## 复用实现

`overlay.measure_line_fill/_stamp_pages/_verify_links`、`fusion.escape_latex/parse_segments/iter_composition_units/_style_flags`、`provider_alignment.to_il_box/_span_text_consistent`、`inline_math_protector.load_provider_document`、`layout_geometry._annotate_space`、`link_snapshot.collect_page_chars`、`capability.probe_latex_capability`、`link_remap.safe_insert_link`、`experiments/toolchain_gates.py`。

## 验证（端到端）

1. `python3 -m pytest tests/ -q` 基线不变 + 新增全过。
2. 三篇回放 default/latex × mono/dual → gates 全 pass，`acceptance_latex.py` 达标，结果提交 `out/acceptance/`。
3. 目视：第 1 页摘要（3oVax）左右对齐、无拆词伪影；第 4 页 B2Rrm 不再被融合成错误公式且判 `untranslated`；公式页 `$n_{win}$` 基线对齐。
4. agy 重译 2026-f1872 后重复 2、3。

## 风险

- P1-5 触及 `pdf_creater` 内容流生成，是本计划最大改动面；默认关闭路径零变化，用既有 `test_link_remap`/`test_force_break` 基线断言护航。
- 批编译单个 TeX 错误的 marker 归属需真实段落压测（P4-1 先做 20 段 spike）。
- 裁片段竖向对齐 ±1bp 误差，以目视验收。
- 应用率估算依赖 P2 分类；若 `fragment` 编译失败率高，回到 P2-5 调整后再进 P5。
