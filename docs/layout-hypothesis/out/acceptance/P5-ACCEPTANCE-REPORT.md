# P5 验收报告：LaTeX bbox 两端对齐排版（P1–P4 合入后）

**日期：** 2026-09-13
**分支：** `feature/latex-bbox-layout`（P0 `3cc2b0c` → P1 `7e72e2e` → P2 `42d21b0` →
P3 `6d1a1f0`+`f98256e` → P4 `0a4772b`）
**回放工件：** `/tmp/p5-final/<paper>/`（PDF/PNG/缓存不入 Git；本目录只存轻量结果）
**本报告取代** `ACCEPTANCE-REPORT.md` / `acceptance-summary.json`（那份是 P0 升级前
基线：LaTeX applied 仅 5/28/1 段），二者保留作 before/after 对照。

## 1. 结果总表

| 指标 | 2026-f1872 (20页) | DeepSeek V4.1 (51页) | LawBench (38页) |
|---|---|---|---|
| LaTeX attempted / **applied** / failed | 159 / **152** / 7 | 220 / **220** / 0 | 98 / **98** / 0 |
| eligible（DONE 分母） | 150 | 208 | 93 |
| **应用率 applied/eligible** | **95.33%** ✓ | **100%** ✓ | **100%** ✓ |
| 非末行 fill ≥0.98 行占比（merged 校正口径） | **762/763 = 99.87%** ✓ | **819/823 = 99.51%** ✓ | **321/321 = 100%** ✓ |
| 同上（基线口径，并列上报） | 721/763 = 94.5% | 734/826 = 88.9% | 321/321 = 100% |
| 文本层零差异（容差口径） | **133/133 = 100%** ✓ | **160/160 = 100%** ✓ | **93/93 = 100%** ✓ |
| 同上（严格字符级） | 123/133 | 154/160 | 93/93 |
| `reverted` | false | false | false |
| 链接 before/after、restored、URI 集合 | 429/429、0、相等 ✓ | 410/410、0、相等 ✓ | 161/161、0、相等 ✓ |
| `toolchain_gates` 硬失败 | 0 ✓ | 0 ✓ | 0 ✓ |
| 公式单元分类（text / mineru / simple_math / fragment） | 269 / 0 / 15 / 1 | 78 / 74 / 94 / 19 | 143 / 0 / 0 / 0 |
| mono 体积 default→latex | 2.42→3.99 MB | 3.03→5.85 MB | 1.62→2.41 MB |

**DONE 判定：三篇全部达标**（应用率 ≥95%、fill ≥99%（merged 口径）、容差口径
文本层零差异 100%、gates 无硬失败、无回滚）。

> fill 口径说明：`lines_nonfinal_merged_*` 把同一视觉行合并后再算非末行填充率。
> P3 起首行缩进按源排版复刻、行内上下标（`$n_{\mathrm{win}}$`）会把基线口径的
> 逐行量测拆细，导致系统性低估（DeepSeek 88.9% vs 99.51%）；两种口径都在
> `acceptance_latex.json` 里并列上报，判定用 merged。裁定见 P4 brief。

## 2. 与历史基线对比（同一三篇、同一 workdir）

| 指标 | P0 基线（升级前） | P5（P1–P4 后） |
|---|---|---|
| LaTeX applied | 5 / 28 / 1 | 152 / 220 / 98 |
| 应用率（DeepSeek） | 10.6% | **100%** |
| 非末行 fill ≥0.98（DeepSeek，基线口径） | 0.928 | 0.889（merged 口径 0.9951） |
| `formula-fusion-failed` | 100 / 80 / 65 段 | **0 / 0 / 0** |
| reconstruct wall（latex，冷缓存） | 12.8 / 23.1 / ~8 s（仅贴 5/28/1 段） | 25.0 / 26.7 / 18.6 s（贴 152/220/98 段） |
| reconstruct wall（latex，热缓存） | — | **9.4 s**（LawBench，cache_hits 98/98） |

应用段数提升 30×（DeepSeek 28→220）；wall 放宽的绝对值被批量编译控制在 30 s
以内（§3），且二次回放近乎零编译。

## 3. 性能（P4 交付目标：DeepSeek 额外 ≤ 30 s）

| 论文 | default wall | latex wall（冷） | 额外耗时 | compile 汇总 |
|---|---|---|---|---|
| 2026-f1872 | 6.92 s | 24.98 s | **+18.1 s** | 114.7 s（多线程累计） |
| deepseek-v4 | 6.34 s | 26.72 s | **+20.4 s** | 66.7 s |
| lawbench | 5.65 s | 18.57 s | **+12.9 s** | 61.0 s |

LawBench 二次回放（缓存命中）：wall 9.37 s、`cache_hits=98/98`、`compile=0.0 s`。

## 4. 链接与无双层文本证据

- 三篇 latex 输出 `links.before_total == after_total`（429/410/161）、`restored=0`
  （免 redaction 构造成立：已贴片段落字符不进内容流）、`uri_set_match=true`；
  default 输出同样 `link_uri_set_match=true`（unresolved 1/2/1 条为纯图形或无字符
  链接，保持原位属预期）。
- 文本层零差异（容差口径 100%）等价于「贴片区等于期望纯文本」，即无残留旧译文、
  无重复层；严格口径（123/133、154/160、93/93）的差额全部来自断词连字符、
  CJK 兼容区码位与 `∆/Δ` 同形字，已逐段核对为字形差异而非丢字。

## 5. 门禁

| 门禁 | 三篇状态 |
|---|---|
| `layout_coverage`（硬） | pass |
| `link_integrity`（硬） | pass |
| `toc_integrity` / `protected_tokens` / `protocol` | `not_available` |

`not_available` 的原因见 `docs/toolchain/README.md`：这三项需要 `anchors.json`
（`parse` / `md-extract` 阶段产物），而本次三篇走的是 `extract → apply →
reconstruct` 流程。`docs/layout-hypothesis/out/acceptance/*-gates.json`（P0 基线）
是同样状态，`ok=true`、`hard_failures=[]` —— 非本次回归。

## 6. 单测

```text
python3 -m pytest tests/ -q   →  7 failed, 409 passed
```

7 个失败全部是 `tests/test_mineru_doclayout_adapter.py` / `tests/test_provider_ir.py`
缺 fixture `mineru/layout_v275_s41586_excerpt.json` 的**既有环境失败**（P0 基线
308 passed / 7 failed 时相同），与本特性无关且未增加。

## 7. P5-3 全链路（agy 重译 2026-f1872）

流程：复制 workdir → `batch_translate.py --model gemini-3.8-flash-low --effort low`
（重译现有 `sheet.jsonl`，196 行 / 5 批）→ `apply` → `reconstruct --latex-bbox --dual`
→ gates + acceptance。**跳过重新 extract**：`extract` 会给段落分配随机
`debug_id`，使旧译文无法 `apply`（见 P5 brief Context）。

| 阶段 | 结果 |
|---|---|
| batch_translate（agy gemini-3.8-flash-low / effort low） | 196 行、5 批、1 次重试、**fallback 0** |
| apply | `ok=true`、violations 0、applied 196、unknown_ids 0 |
| reconstruct --latex-bbox | attempted 159 / **applied 153** / failed 6；wall 26.19 s |
| 链接 | 429/429、restored 0、URI 集合相等 ✓ |
| gates | `ok=true`、hard_failures 0 |
| eligible / applied / **应用率** | 153 / 147 / **96.08%** ✓ |
| 非末行 fill ≥0.98（merged） | **767/767 = 100%** ✓ |
| 文本层零差异（容差 / 严格） | **137/137 = 100%** / 127 |

与复用旧译文的同篇回放（§1：152 applied、95.33%）相比，重译译文 applied 153、
应用率 96.08% —— 全链路（翻译 → 写回 → 重排）在新鲜译文上同样达标。

证据：`p5-2026-f1872-agy-{gates,reconstruct-latex,acceptance}.json`；
目视材料 `/tmp/p5-final/2026-f1872-agy/renders/page-{001,005,008,009,020}.png`。

## 8. 遗留台账（不阻塞验收）

1. `2026-f1872` 7 段 `text-mismatch` 回退（xeCJK 字符类 + ToUnicode 码点映射，
   5 种字体组合实测无解）→ 应用率上限 95.33%，仍达标；
2. `HZpip`（7 个 MinerU 片段）`fill_after≈0.07`、`aG4M3≈0.22`：公式密集段盒内
   行填充偏低（片段高度范围问题），已列入后续优化；
3. 少量 applied 段 `fill_after=None`（fragment 密集段文本层无法量测，13–14/220）；
4. `text` 类公式片段的粗/斜样式未随 `escape_latex` 入 body（源样式丢失）；
5. 上游「源文含 `{v1}` 垃圾致 LLM 拒译」只记录不处理。

## 9. 目视材料（PNG，不入库）

`/tmp/p5-final/<paper>/renders/page-NNN.png`（default | latex 并排）：

| 论文 | 页 | 选页理由 |
|---|---|---|
| 2026-f1872 | 1, 5, 8, 9, 20 | 首页 / 随机 / 随机 / 公式页 / 末页 |
| deepseek-v4 | 1, 9, 16, 25, 51 | 首页 / 公式页 / 随机 / 随机 / 末页 |
| lawbench | 1, 19, 26, 28, 38 | 首页 / 公式页 / 随机 / 随机 / 末页 |
| 2026-f1872-agy（P5-3） | 1, 5, 8, 9, 20 | 同上，重译译文 |

## 10. 复现

```bash
cp -r /tmp/babeldoc-latex-acceptance/<paper>/workdir /tmp/p5-final/<paper>/workdir
python3 -m babeldoc.tools.agent reconstruct /tmp/p5-final/<paper>/workdir --dual --output-dir /tmp/p5-final/<paper>/out-default
python3 -m babeldoc.tools.agent reconstruct /tmp/p5-final/<paper>/workdir --latex-bbox --dual --output-dir /tmp/p5-final/<paper>/out-latex
python3 experiments/toolchain_gates.py /tmp/p5-final/<paper>/workdir
python3 experiments/acceptance_latex.py /tmp/p5-final/<paper>/workdir <mono.pdf> --dual-pdf <dual.pdf>
```

原始证据：`p5-summary.json`、`p5-<paper>-{gates,reconstruct-default,reconstruct-latex}.json`
与 `p5-2026-f1872-agy-*.json`（JSON 被仓库的 `*.json` 忽略规则覆盖，入库需
`git add -f`）。

三篇回放的 `apply` 在 workdir 产出时已完成（目录内已有
`agent/{translated.jsonl,il_translated.applied.json}`），因此 §1 的回放从
`reconstruct` 开始；§7 的 agy 全链路则从翻译开始完整重跑了一遍。
