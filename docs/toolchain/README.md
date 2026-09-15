# BabelDOC 工具链技术文档

> 本目录描述 `BabelDOC-agy-mvp` 分支上 **MinerU 深度融合** 之后的工具链：
> 从 PDF 到 mono/dual 成品的完整数据流、每层的不变量、每个工具的真实契约、
> 以及全部可运行门禁。
>
> 定位：这是**实现参考**，不是入门教程。每个论断都给出代码位置或可复现命令；
> 面向「出问题时该看哪个产物、该跑哪条命令」。入门请先读
> [`../../skills/document-translate/SKILL.md`](../../skills/document-translate/SKILL.md)
> 与 [`../agent-translate-pipeline.md`](../agent-translate-pipeline.md)。

---

## 1. 数据流总览

```text
输入 PDF
  │
  ├─[0] _prepare_pdf ────────── PDF 修复（null xref / filter / mediabox），保留 Link 注释
  │        babeldoc/tools/agent/workflow.py::_prepare_pdf
  │
  ├─[1] native parse ────────── page.pdf_character（扁平字符流：字符 + 坐标 + 字体，无段落）
  │        new_parser/native_parse.py（BabelDOC 原生字符层 = 唯一写回真源）
  │
  ├─[2] LayoutParser ────────── MinerU 布局 → page.page_layout（PageLayout 区域列表）
  │        midend/layout_parser.py；同一步落盘：
  │        ・agent/source/mineru/provider_ir.json   （MinerU 完整 block/line/span 树）
  │        ・<workdir>/<pdf名>/layout_coverage.json  （覆盖率门禁审计）
  │        ⚠ 覆盖率门禁在此判定：超阈值 → 抛 layout_coverage_gate，解析中止
  │
  ├─[3] InlineMathProtector ─── MinerU inline_equation span → 追加 formula 区域
  │        midend/inline_math_protector.py；落盘 agent/source/mineru/alignment.json
  │
  ├─[4] EnclosedMarkerFixer ─── 圈号修复（数字 + 矢量圆圈 → Unicode 圈号，删装饰曲线）
  │
  ├─[5] ParagraphFinder ─────── 聚类成行/成段；分配 layout_label 与确定性 debug_id（P02-003）
  │
  ├─[6] TocDetector ─────────── 目录页条目化：标题段(toc_entry) + 页码段(toc_entry_page)
  │        midend/toc_detector.py；落盘 agent/source/toc.json
  │
  ├─[7] StylesAndFormulas ───── 同样式 run 合并、公式对象（PdfFormula）识别
  │
  ├─[8] 快照 ────────────────── 书签 → agent/source/bookmarks.json
  │                            超链接 → agent/source/links.json（+ 字符对象进 state.pkl）
  │                            （两者都必须在 Typesetting 之前：坐标还是源坐标）
  │
  ├─[9] ILTranslator.pre ────── 每段 → canonical 源文（<style>/{vN}）
  │        + markdown_view → agent/document.md（整篇连续 Markdown + 行内锚点 + 段落标记）
  │                        agent/{anchors.json,sheet.jsonl,state.pkl}
  │
  ├─[10] LLM 整篇翻译 ──────── 一次调用翻译整篇 document.md → agent/translated.md
  │        （不是分块并发；术语与语气一次成型）
  │
  ├─[11] md-apply ──────────── 校验（id 对齐 / 锚点多重集 / 空 span / label 一致性）→ 写回 IR
  │        落盘 agent/translated.jsonl + agent/il_translated.applied.json
  │
  ├─[12] Typesetting ───────── 重排：源字符 box 原地改写 / 译文新建字符对象
  │        落盘 agent/layout_geometry.json
  │
  ├─[13] PDFCreater ────────── mono = 源 PDF 容器 + 重生成的页内容流；
  │        链接按源字符身份重映射 + URI 集合门禁；dual = 拼宽双语 + 书签搬运
  │
  └─[14] render ────────────── 代表性页 PNG（视觉审查）
```

对应代码：解析链在 `babeldoc/tools/agent/markdown_view.py::_run_parse`
与 `babeldoc/tools/agent/workflow.py::extract`（两条入口**同构**）。

---

## 2. 六阶段一览

| 阶段 | 入口 | 输入 | 输出（落盘） | 失败模式 |
|---|---|---|---|---|
| **解析** | `bdt parse` / `md-extract` | PDF + MinerU 布局（API 或回放） | `document.md` `anchors.json` `sheet.jsonl` `state.pkl` `provider_ir.json` `alignment.json` `toc.json` `bookmarks.json` `links.json` `layout_coverage.json` | `layout_coverage_gate`（硬）；`toc_low_confidence`（软）；`mineru_token_missing`；`layout_unsupported` |
| **翻译** | `bdt translate` | `document.md` | `translated.md`（+ `usage.json`） | `model_cli_missing`（无 `agy` 等 CLI 时改用 `--markdown <文件>` 导入） |
| **写回** | `bdt apply` | `translated.md` | `translated.jsonl` `il_translated.applied.json` `apply_report.json` | `anchor_multiset_mismatch`、`extra_ids`（硬，阻断） |
| **审查** | `bdt check`（内部 `review.backtranslate_check`） | 上述产物 | `review_verdict.json` | `verdict=needs_fix`（软，进重译循环） |
| **重建** | `bdt build` | IR + `state.pkl` | `output/*.mono.pdf` `*.dual.pdf` `reconstruct_report.json` | `link_uri_set_mismatch`（硬）；`link_unresolved`（软） |
| **渲染** | `bdt build --render 1,2`（内部 `layout.render_pages`） | mono/dual PDF | `render/*.png` | 页号越界静默跳过 |

> 硬/软门禁的完整清单与触发方式：见 [`gates.md`](gates.md)。

---

## 3. 离线回放（不消耗 MinerU API）

MinerU layout.json 按 **PDF 内容 sha256** 缓存在 `~/.cache/babeldoc/mineru-layout.v1/<hash>.json`。
回放有两种等价写法：

```bash
# ① 直接给缓存文件路径
python -m babeldoc.tools.agent md-extract DeepSeek_V41_Tech_Report.pdf \
  --workdir tmp/docs-smoke \
  --mineru-json ~/.cache/babeldoc/mineru-layout.v1/ba68e2e40408125ae6d2f63a9a241b61c73910691c74ec1a2a7023c851eac08d.json

# ② 只给内容哈希（--mineru-cache-key），缓存未命中时明确报错
python -m babeldoc.tools.agent md-extract DeepSeek_V41_Tech_Report.pdf \
  --workdir tmp/docs-smoke \
  --mineru-cache-key ba68e2e40408125ae6d2f63a9a241b61c73910691c74ec1a2a7023c851eac08d

# ③ 工具层等价写法（bdt）
bdt parse DeepSeek_V41_Tech_Report.pdf --workdir tmp/docs-smoke \
  --mineru-cache-key ba68e2e40408125ae6d2f63a9a241b61c73910691c74ec1a2a7023c851eac08d
```

本仓库样本与缓存哈希对照（仓库根目录）：

| 样本 | 页 | 链接 | 书签 | sha256（缓存 key） |
|---|---|---|---|---|
| `DeepSeek_V41_Tech_Report.pdf`（主验收样本） | 51 | 410 | 54 | `ba68e2e40408125ae6d2f63a9a241b61c73910691c74ec1a2a7023c851eac08d` |
| `2312.04432v2.pdf` | 16 | 323 | 27 | `519f7090f41e66e81d0e5493ae79aba536755297747b2548cc8de637c04bd01f` |
| `ccs2026b-paper3764.pdf` | 21 | 319 | 28 | `ebdca8e7e579450770eafdf98243f197b8e60fc115fe07ab15870a31812a326b` |
| `2026-f1872-paper.pdf` | 20 | 429 | 53 | `f15a0e00eed725df8ed53c5da37250bb2a09ac1dd4c7710013d6b0b8d5ac062e` |
| `Fei 等 - 2023 - LawBench ….pdf` | 38 | 161 | 18 | `4a4192fb88b8b09f0d3da2ac9ef71f07502fdaf344a83062ff82f1bff74e1284` |

---

## 4. 验收一条命令

所有门禁聚合为一份机读 JSON：

```bash
python experiments/toolchain_gates.py <workdir> [--pdf <源pdf>] [--json]
```

对刚解析完（未重建）的 workdir，链接/协议门禁会降级为 `not_reconstructed` /
`not_available`（不算失败）；对完整走完
`md-extract → md-apply → reconstruct` 的 workdir，五项门禁全部可判定。
退出码：任一硬门禁失败 → `1` + `"ok": false`。

实测（DeepSeek 样本，完整走完 `md-extract → md-apply → reconstruct` 的 workdir）：

```text
layout_coverage  pass  未覆盖 59/135415 字符，占比 0.0436%（阈值 0.5000%）
link_integrity   pass  URI 集合：源 40 条 / mono 40 条；缺失 0，新增 0
toc_integrity    pass  目录条目 54（anchors 54，书签 54），顺序不一致 0
protected_tokens pass  翻译输入含 265 个 {vN} 占位符（103 段）；inline_equation 103/126 已对齐
protocol         pass  translated.jsonl 352 条 / anchors 352 条；缺失 0，多余 0
```

---

## 5. 本目录导航

| 文件 | 内容 |
|---|---|
| [`architecture.md`](architecture.md) | 分层架构、各层职责边界与不变量、关键对象生命周期 |
| [`pipeline-stages.md`](pipeline-stages.md) | 每阶段输入/输出 artifact、关键字段、不变量、失败模式 |
| [`tool-api.md`](tool-api.md) | 每个工具的入参/出参/副作用/典型调用/失败码 |
| [`label-dictionary.md`](label-dictionary.md) | layout label 全表：来源、语义、是否翻译、保护原因、开关 |
| [`gates.md`](gates.md) | 所有门禁：阈值、触发条件、产物路径、修复建议、人为触发命令 |
| [`troubleshooting.md`](troubleshooting.md) | 按症状排查：目录、链接、漏译、公式、字号、圈号 |

相关既有文档：

- [`../agent-translate-pipeline.md`](../agent-translate-pipeline.md)：管线逐步详解（含历史背景）
- [`../../skills/document-translate/SKILL.md`](../../skills/document-translate/SKILL.md)：agent 编排流程与工具速查
- [`../../skills/document-translate/reference/pipeline.md`](../../skills/document-translate/reference/pipeline.md)：skill 内自包含副本
- [`../../skills/document-translate/reference/schemas.md`](../../skills/document-translate/reference/schemas.md)：产物 schema 摘要
- `.plan/minerU深度融合.md`（本轮改造的实施计划；该文件为本地计划稿，不在版本库里）
