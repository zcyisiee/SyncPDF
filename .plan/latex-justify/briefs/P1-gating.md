# Task: P1 — 资格门禁重写与免 redaction 贴片机制

## Objective

根因 1（~92% 段落被旧门禁挡掉）与根因 7（TeX pt vs PDF bp 单位混用）：
重写候选资格规则并新增 full/repair 模式开关；未翻译段跳过；模板与度量
统一 bp；并发默认 CPU 核数；已贴片段落的字符不再写入内容流（无双层文本
靠构造保证），redaction 降级为兜底。

## Context

分支 `feature/latex-bbox-layout`。通读
`babeldoc/format/pdf/document_il/backend/latex_bbox/overlay.py`（390–475 行
`_select_candidates`、`apply_latex_bbox_overlay`、`write_report`）、
`renderer.py`（模板 29–46 行、`_measure_fit` 102–144 行、149 行并发）、
`translation_config.py`（277–283 行 latex 参数）、`babeldoc/main.py`
（396–431 行）、`babeldoc/tools/agent/__main__.py`、
`babeldoc/format/pdf/document_il/backend/pdf_creater.py`（1799–1850、2025 行
内容流生成）。

基线证据：DeepSeek 回放 352 段仅 28 applied；纯文本段仅
`min_body_fill < 0.85` 才替换、单行段跳过（`overlay.py:455-462`）；
`paperwidth=…pt`（TeX pt）而 bbox/fit 用 PDF bp，455.67pt 页只有 453.97bp。

回放 workdir：`/tmp/babeldoc-latex-acceptance/{2026-f1872,deepseek-v4,lawbench}/workdir`
（只读）。P0 产出的 `decisions[]` 报告可用于验证。
单测基线 308 passed / 7 既有失败不变。

用户已批准的决策（照做，不要重新发明）：
- eligible = 正文标签（`text/list/figure_caption/table_caption/page_footnote/
  table_footnote`）∧ 源行数 ≥2；`title/toc_*/reference/page_number/author/
  figure/table_text/code` 永不走 LaTeX。默认模式 `full`：所有 eligible 段默认
  走 LaTeX，仅失败/几何不安全回退；`repair` 模式复现旧行为（保留既有测试可复现）。
- 未翻译段（译文与源文归一化相等）标记 `untranslated` 跳过，不走 LaTeX。
- 溢出处理：先安全下扩（P3-5 完整实现，本任务先留接口）再 Bounded Shrink。

## Deliverables

1. **P1-1 资格规则 + 模式开关**：`overlay.py` `_select_candidates` 重写；
   删除 `line-fill-ok` / `no-body-lines` 两个 reason（repair 模式保留旧行为
   词表）；`translation_config.py` 新增 `latex_bbox_mode: str = "full"`
   （`"full"|"repair"`）；`main.py` CLI 选项 `--latex-bbox-mode`；
   `__main__.py` reconstruct 子命令加 `--latex-bbox-mode` 传递。
2. **P1-2 未翻译跳过**：workflow 路径用 `state.pkl` 的 `inputs[debug_id].unicode`、
   high_level 路径在翻译前记录源文；归一化相等 → reason=`untranslated`。
   涉及 `overlay.py` capture、`babeldoc/tools/agent/workflow.py`（约 515 行）、
   `babeldoc/format/pdf/high_level.py`（约 1009 行附近）。
3. **P1-3 单位统一 bp**：`renderer.py` 模板 `paperwidth=%(w).4fbp`、
   `\fontsize` 按 bp 换算、`_measure_fit` 与 stamp rect 同单位；编译页
   rect 与 bbox 误差 ±0.01。
4. **P1-4 并发**：`latex_max_compile_workers` 默认 `min(os.cpu_count(), 16)`；
   200 段压测（可用合成 IL fixture）无 timeout/冲突。
5. **P1-5 不发射已贴片段落字符**：`apply_latex_bbox_overlay` 拆
   `prepare()`（选段+编译，在 `pdf_creater.write` 内容流循环前调用）与
   `stamp()`（贴片）；`pdf_creater.py` 内容流生成跳过 `stamped_ids` 的字符
   → 旧路径译文不入流；redaction 仅作兜底（先断言 stamp rect 内文本层为空，
   断言失败才 redact）；`box-expanded-after-typesetting` 门禁改为对扩后矩形
   做"无他段字符"校验。链接多重集/URI 集合仍恒等（复用 `_verify_links`）。
6. `tests/test_latex_bbox.py` 追加：full 模式资格（多行正文段入选、单行/标题/
   toc 排除）、repair 模式旧行为回归、untranslated 跳过、bp 单位
   （编译页 rect==bbox±0.01，xelatex 缺失时 skipif）、无 stamp 区双层文本测试、
   prepare/stamp 拆分调用顺序。

## Constraints

- P1-5 触及 `pdf_creater` 内容流生成，是本计划最大改动面：默认关闭路径
  （`enable_latex_bbox_layout=False`）必须零行为变化，用既有
  `test_link_remap` / `test_force_break` 基线断言护航。
- 不改旧排版器（`typesetting.py`）拆词行为。
- 不碰 `/tmp` 既有 workdir；不引入新依赖；不 commit。
- `decisions[]` 报告字段随门禁变化同步更新（reasons 新词表）。

## Validation

```bash
python3 -m pytest tests/ -q                    # 308+新增 / 7 既有失败
python3 -m pytest tests/test_latex_bbox.py tests/test_latex_bbox_links.py -q
python3 -m babeldoc.tools.agent reconstruct --help
# 回放验证（DeepSeek）：attempted 应 ≥ 200（eligible 约 211）
python3 -m babeldoc.tools.agent reconstruct /tmp/babeldoc-latex-acceptance/deepseek-v4/workdir \
  --latex-bbox --dual --output-dir /tmp/p1-verify/deepseek-out
python3 experiments/toolchain_gates.py /tmp/babeldoc-latex-acceptance/deepseek-v4/workdir
python3 experiments/acceptance_latex.py /tmp/babeldoc-latex-acceptance/deepseek-v4/workdir <mono_pdf>
```

（回放命令的具体参数以 `reconstruct --help` 实际签名与 workdir 布局为准，
先跑 `--help` 确认。）

## Report back

- 文件清单与目的；验证输出摘要；
- DeepSeek 回放前后 attempted/applied/fallback_reasons 对比表；
- B2Rrm 段 reason 变为 `untranslated` 的 decisions 摘录；
- P1-5 内容流跳过的实现要点与链接恒等性证据；
- 偏离与理由。
