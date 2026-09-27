# 两端对齐：断行评分与绘制必须用同一组 glue

## 结论

Knuth–Plass 选出的断点只在「绘制按评分所用的 (stretch, shrink) 执行」时才是最优。评分与绘制各有一套分配规则（评分按权重、绘制均分；声明 shrink=0 却在绘制时压缩），会让 DP 认为便宜的行在画面上变差；伸缩量声明过宽（空格 stretch 4w）则让 11pt 的空格 badness 只有约 20，DP 不会回避。

## 根因与证据

- 2405.15269v3 p3 左栏：引文行空格约 11pt（自然 3.3pt），「揭 示」字距 +0.84em，「Chenetal.」空格被压没。源码：`layout.rs` 空格 stretch 4w/shrink w、`place_row` 的 even_stretch 均分、负 ratio 同时压 CJK 字距。
- 修复（`syncpdf-typeset/src/layout.rs`）：`Row.glue` 保存每个 glue 的声明量，`place_row` 逐 glue 执行 `ratio×stretch|shrink`；空格 0.5w/w÷3，CJK 字距 0.1em/0；CJK↔拉丁合法断点也是同参数零宽 glue；全角标点相邻空格宽度与伸缩为 0（clreq，标点字面已含空白）。
- 结果（`tmp/linebreak/measure_gaps.py`，全文两端对齐非末行）：2405 最大空格 p95 0.753→0.659em、max 1.971→1.554em；CJK 字距增量 max 1.715→0.75em。5 篇回归（TRC/ALNS/VNS/2604/2602 cache-only 回放）回退段数与基线完全一致，同一行对比空格 p95/max 不变。
- 回归测试：`syncpdf-typeset/tests/typeset.rs` 中 `justify_executes_each_glue_as_declared_to_the_breaker`、`shrinking_row_compresses_spaces_only_never_cjk_tracking`、`breaker_prefers_hyphenation_over_a_very_loose_latin_line`、`mixed_script_spaces_stay_bounded_across_a_paragraph`、`space_beside_fullwidth_punctuation_draws_no_extra_blank`、`cjk_latin_boundary_takes_tracking_slack`。

## 以后如何做

- 调伸缩效果只改声明量（节点与 `Row.glue` 同一处产生），不要在绘制侧再加分配规则或常量。
- 「全角标点旁空格」按字符类别（CJK ∩ Unicode Punctuation）判定，不列具体标点。
- 测量工具：`min_space_gap_em` 会因零宽（被折叠）空格报出小负值（前一 CJK 字形墨迹略超 advance），不是重叠。

## 边界 / 待验证

- 可伸 glue 很少的行（纯 CJK 夹公式原子）slack 全由 0.1em 的字距承担，ratio 可到数倍（VNS p3 一行约 0.48em）；tolerance 10 仍允许。只有真实样本证明需要时才做 Step 4（过松质量指标 / refinement 处理 FitsButTooLoose），未实现。
