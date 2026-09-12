# LaTeX bbox 排版——三篇论文真实翻译验收报告

**日期：** 2026-09-12
**分支：** `feature/latex-bbox-layout`
**翻译模型：** `agy` CLI · `gemini-3.8-flash-low`（计划锁定的 gemini-3.8-flash + low thinking；运行前已验证可用）
**流程：** `extract`（MinerU 缓存回放）→ `batch_translate`（gemini-3.8-flash-low，逐批协议校验）→ `apply` → `reconstruct`（默认 / `--latex-bbox` 各一次，均带 `--dual`）
**工件根目录：** `/tmp/babeldoc-latex-acceptance/<paper>/`（不入 Git；本目录只存轻量结论）

## 1. 结果总表

| 指标 | 2026-f1872 (20页) | DeepSeek V4.1 (51页) | LawBench (38页) |
|---|---|---|---|
| 翻译段落数（apply） | 196（violations 0） | 352（violations 0） | 125（violations 0） |
| LaTeX attempted / **applied** / failed | 5 / **5** / 0 | 29 / **28** / 1 | 1 / **1** / 0 |
| LaTeX reverted | false | false | false |
| mono 页数 / 目录书签（=源） | 20 / 53 ✓ | 51 / 54 ✓ | 38 / 18 ✓ |
| **mono URI 集合 = 源**（default & latex） | ✓ 38 条 | ✓ 40 条 | ✓ 16 条 |
| **dual URI 集合 = 源**（default & latex） | ✓ | ✓ | ✓ |
| 内部跳转目标页不匹配 | 0 / 361 | —（353 GOTO+2 NAMED） | 0 / 142 |
| 双层文本（latex−default 混排行差） | 0 | −1 | 0 |
| toolchain_gates（hard） | layout_coverage ✓ link_integrity ✓ | 同左 | 同左 |
| mono 体积 default→latex | 2.31→4.12 MB | 2.89→3.59 MB | 1.55→1.57 MB |
| reconstruct 耗时 default→latex | 5.2→12.8 s | 5.9→23.1 s | ~6→~8 s |

三篇论文均满足成功标准：`link_uri_set_match=true`（mono 与 dual、default 与 latex
共 12 份输出全部通过）、参考文献链接保留（URI 集合逐一相等；内部跳转目标页
零漂移）、无 P0/P1 layout finding（gates 全 pass）、译文协议通过（apply
violations=0，未知 id=0）、无双层文本。

## 2. 链接与参考文献证据

- 源 → mono：NAMED 引用跳转按 `_rebuild_link` 归一为 GOTO 直接目标；
  逐条比对源 NAMED 解析页码与输出 GOTO 页码：2026-f1872 361/361、
  LawBench 142/142 完全一致；URI 集合与源完全相同（含 NDSS/LLVM/arXiv 等外部链接）。
- mono → dual：并排模式左右两侧链接按 `_copy_page_links_to_dual` 缩放搬运
  （计数恰为 mono 的 2×）；交替模式经 `insert_file` + NAMED 重建 + TOC 源快照
  单次映射，书签条目数与源一致。
- LaTeX overlay 的链接生命周期：redaction 前快照 → 按原矩形重插（三篇共
  restored 3/8/0 条）→ 重开副本硬校验每页链接多重集 + URI 集合 →
  `uri_set_match=true`，无回滚。
- 期间发现并修复两个阻断性既有缺陷（见 commit 7993f56、db7eab0）：
  `ParagraphFinder.process` 不返回 document；pymupdf 1.27 `insert_link` 对
  URI 含 "/Link" 子串的 /NM 注入污染。

## 3. LaTeX 应用质量与回退统计

applied 段落经 XeLaTeX 在原 bbox 内重排（xeCJK + `PunctStyle=plain` +
`\XeTeXlinebreaklocale "zh"`，源字号首选 + 有界缩小），全部通过墨迹边界、
overfull-hbox 与无丢字校验。回退原因全部结构化（见 latex-stats.json）：

- `line-fill-ok`：现有渲染已达标，无需替换（选择性替换原则的主体）；
- `formula-fusion-failed`：`{vN}` 无一一对应的 MinerU `inline_equation` 源码
  （BabelDOC 启发式公式检测无 LaTeX 源），按计划保留 PdfFormula 矢量路径；
- `no-body-lines` / `box-too-small` / `box-expanded-after-typesetting`：几何与
  质量门禁安全跳过；
- DeepSeek 1 段 `text-clipped-tail`：有界缩小耗尽后安全回退（failed=1）。

**已知限制（如实记录）：**
1. 应用率仍低（1~28 段/篇）：公式段落依赖 MinerU 行内公式 span 覆盖，
   启发式公式（无 MinerU LaTeX 源）一律不猜测、直接回退；
2. stamp 嵌入字体使 mono 体积增加（2026-f1872 +78%，公式段落少、基准小）；
3. 英文/数字仍用 TeX 默认拉丁字体（LM），与产品字体映射存在视觉差异；
4. 未做 20+ 篇规模回归与人工大规模视觉评测（Phase 3 范畴）。

## 4. 性能

- 默认路径无回退：5.2~5.9 s / ~950 MB（20~51 页）。
- LaTeX 开启：+7~17 s，其中 TeX 编译占 3.1 s（6 次尝试）/ 24.5 s（49 次尝试，
  含有界缩小重试），并发度 2、每 bbox 独立临时目录、45 s 超时兜底。
- stamp 缓存命中 0（真实文档段落文本各不相同，属预期）；缓存对同一文本
  重复渲染（如 write 重试）有效。

## 5. 结论

- 代码测试：**308 passed / 7 既有 fixture 缺失失败**（与本特性无关）。
- 真实翻译：三篇论文 parse→translate→apply→reconstruct 全链路完成，
  协议 violations=0。
- 链接门禁：12 份输出（3 论文 × {default,latex} × {mono,dual}）URI 集合
  全部与源一致；参考文献跳转目标零漂移。
- 视觉抽样：每篇渲染首页/目录页/参考文献页/末页 PNG（`/tmp/.../renders/`），
  applied 段落无压字、无双层（混排行差 ≤0）。
- 仍未验证：大规模（>20 篇）回归、表格内/图内公式段落的 LaTeX 化、
  极端长 token（邮箱/超长参数）场景的主观视觉评分。
