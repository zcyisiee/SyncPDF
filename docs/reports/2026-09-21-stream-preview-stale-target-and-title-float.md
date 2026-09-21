# 流式预览「只有零星几块翻译成功 + 标题上移」修复报告

- 日期：2026-09-21
- 范围：`bdt serve` 流式翻译预览（`ServeStreamPreview` → `BlockCompiler.compile_block_patch`）
- 现场：用户文档 `up-vns-20260921-022426`（12 页 / 翻译范围 169 块）
- 前情：`docs/reports/2026-09-21-stream-preview-compile.md` 宣称「编译质量优秀」，实测不成立；该文已加更正说明

## 一句话结论

两条独立缺陷，都只存在于**流式**路径，一次性全量编译都没有：译文读的是 5s 缓存快照，
导致绝大多数块编译时「查无译文」；流式路径又缺了全量路径的两道资格门禁，把单行标题
当正文编译，触发浮动扩框把标题整体上移。两者均已修复，资格判定现与全量路径**逐块一致**
（119 选中 / 50 否决，与该文档全量构建的 `latex_candidates` 完全相同）。

## 用户现象 → 根因对应

| 用户现象 | 根因 |
|---|---|
| 编译时疯狂显示报错，预览失败 | 根因 1（每个被饿死的块都发一条 `preview_failed`）+ 根因 2（不该编的块也报失败） |
| 只有零星几个 bbox 成功翻译 | 根因 1 |
| 标题莫名其妙向上移了一点 | 根因 2 |
| 编译速度尚可 | 上一轮的效率修复确实生效，与本轮无关 |

## 根因 1：译文读的是 5s 缓存快照，绝大多数块被饿死

`BlockCompiler._rows` 按 `did` 缓存段落行 5s（`_ROWS_CACHE_TTL_S`），`compile_block_patch`
从缓存行里取 `row.target`。但 `babeldoc_tools/translate.py` 的顺序是：

```python
commit_translation(...)      # 译文落 translation_blocks
preview.submit(pid, body, label)   # 同一瞬间提交编译
```

两件事之间没有任何间隔，**缓存快照里永远还没有正在编译的这一块** → `缺少译文或排版数据`。

证据（用户库 `~/.sp/app.db`）：

- 118 条失败的 `translated_at` 与 `failed_at` 精确到秒相同；
- 33 条成功彼此间隔 5–11s，正是 TTL 的节拍；翻译跨度 208s ÷ ~6s ≈ 33；
- 即每个 TTL 窗口里只有**第一个**到达的块能编译成功。

引入提交：`3122f5d2`。

**修复**：新增 `_target(did, pid)` 按 `(document_id, block_id)` 主键单行查询，
译文优先级改为 **草稿改写 > 库内权威行 > 行快照**。缓存从此只负责几何。
保留行快照兜底是为了不改变「库里没有该块」（迁移文档、上一轮产物）时的既有行为。

## 根因 2：流式路径缺资格门禁，单行标题被当正文编译并上移

全量路径 `LatexBboxOverlay._select_candidates` 有两道门禁，流式路径没有：

1. `layout_label` 必须属于 `_BODY_LABELS`（`title` 不在其中）；
2. **源文**行数 ≥ 2。

于是 `P01-006` / `P06-005` / `P08-011`（均为 `layout_label=title`、源文 1 行）被当正文编译：
译文在源字号档放不下 → 缩一档（实测 `13.4496 / 0.95 = 14.1575`，正好一步）→
`layout_refine.expansion_reason` 因 `scale < SHRINK_RATIO(0.995)` 判定需要扩框 →
`_float_if_shrunk` 向上吃掉上一行净空。`P01-006` 顶边由 630.03 抬到 656.59，**上移 26.6pt**
——就是用户看到的「标题向上移动了一点点」。

**修复**：把两道门禁移植进 `compile_block_patch`，在开 PDF、起 xelatex 之前判定。

### 关键细节：源文行数必须量**源** PDF

这一步返工过两次，记录下来避免重蹈：

- ❌ 量 `compile_block_patch` 手上的 baseline 页。baseline 来自 `_outputs()`，第一顺位是
  `output/*.mono.pdf`——文档跑过一轮后**那已经是译文页**，量出来是译文行数。
  （全量路径能直接量页面，是因为它在 prepare 期跑，那时页上还是源文。）
- ❌ 用 `row.geometry['n_lines']`。`geometry` 是 apply 之后的**排版**几何（它的 `text`
  字段就是中文），`n_lines` 是译文排完的行数。实测比全量路径多否决 7 块。
- ❌ 用 `state.pkl` 的 `source_line_geometry[pid]['n_lines']`。虽是源文侧，但与全量路径
  的判定对不齐：该文档 13 条 `single-line` 判定里错 8 条。
- ✅ 用 `overlay.measure_line_fill` **现量源 PDF**（prepared 优先，回退 parse 记录的输入
  文件），结果按 `(did, pid)` 记忆，同一块重编不再开第二次 PDF。

### 「不替换」是落定，不是失败

新增 `NotReplaced` 异常与 `compile_blocks.status='not_replaced'`：

- 不用 `preview_failed`：这类块显示基线原文**就是正确结果**，报成失败会在界面刷出满屏假报错；
- 必须算「落定」（进 `_SETTLED_STATUSES`），否则 `_page_complete` 永远等不齐，整页发不出来；
- 事件 `block_not_replaced` 在前端显示为中性的「保留原文」（`tone: 'info'`）。

## 验证

### 资格判定与全量路径逐块一致

拿该文档全量构建归档里的 `latex_candidates` 事件做基准真值：

| | 全量构建（真值） | 流式门禁（修复后） |
|---|---|---|
| selected | 119 | **119** |
| label-not-eligible | 36 | **36** |
| single-line | 13 | 14 |
| box-too-small | 1（`P10-010`） | —（`P10-010` 归入 single-line） |

**否决的块集合完全相同**，唯一差异是 `P10-010` 的原因标签：全量路径先查 box-too-small，
流式先查 single-line；两边都不替换它，落地结果一致。

### 测试

`tests/test_serve_block_compile.py` 新增 6 例、`tests/test_serve_stream_preview.py` 新增 1 例。
其中 4 例在修复前的代码上**确实失败**（已实测反向验证），多行正文那例两边都通过（回归护栏）：

- `test_freshly_committed_translation_is_compiled_not_starved` — 根因 1
- `test_committed_translation_wins_over_stale_row_snapshot` — 根因 1
- `test_title_block_is_not_replaced_and_never_renders` — 根因 2
- `test_single_line_source_block_is_not_replaced` — 根因 2
- `test_multi_line_body_block_still_compiles` — 回归护栏
- `test_unmeasurable_source_lines_do_not_block_compile` — 无据不拦
- `test_not_replaced_block_settles_page_without_failure` — 不替换的块不阻塞成页、不报失败

顺带修掉一个本轮引入的真 bug：`_SETTLED_STATUSES` 从 2 项加到 3 项，
`_page_complete` 的 SQL 却仍写死 `IN (?,?)`，占位符数量不匹配（测试即时暴露，已改为按长度生成）。

### 基准脚本

`tmp/bench/replay_preview.py` 两处修正，使这一类 bug 今后压测可达：

1. 译文不再预写，改为**逐块「先落库、紧接着 submit」**，与 `translate.py` 同序；
2. 报告新增 `compile_status_counts` / `blocks_stamped_ok`——只看墙钟和 `preview_failed`
   看不出「块没编出来但页照常合成」。

## 预期效果（该文档，169 块翻译范围）

- `ok`：33 → ~119
- `not_replaced`：0 → 50（36 标题 + 14 单行，均显示基线原文，**不报错**）
- `preview_failed`：118 → 内容级的少数几块（见下）
- 标题不再被编译，因而不再触发浮动上移

## 遗留（本轮未修，有证据，属内容级问题）

`P03-016` / `P04-019` / `P05-013` / `P06-015` / `P09-020` 五块在**全量构建里同样失败**
（`text-mismatch` / `text-clipped-tail`），是译文内容与源文不匹配，不是流式路径的缺陷，
不在本轮范围内。
