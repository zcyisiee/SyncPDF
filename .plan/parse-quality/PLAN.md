# 解析质量根因修复计划（feature/latex-bbox-layout 分支延续）

> 计划落盘到 `.plan/parse-quality/PLAN.md` + `briefs/<task-id>.md`（`git add -f` 入库）；
> 按"大项目 → 小任务"委派 leaf subagent 执行、主 Agent 验收；每个大项目完成后在当前分支中文 commit。

## 背景：三问题根因（已全部实锤定位）

用 `测试.pdf`（2512.08296v3 第 7 页单页抽取版）+ `2512.08296v3.pdf` 全链路对照排查，产物在 `tmp/md-test/`、`tmp/md-test2/`、`tmp/md-2512/`、`tmp/md-2512-p7/`：

| # | 现象 | 根因 | 责任方 | 证据 |
|---|------|------|--------|------|
| A1 | document.md 正文逐字符空格（`S i n g l e`） | `PdfCharacter.advance` 存的是 **text-space** 值，`box` 是 **device-space** 值；PDF 用 `Tf 1` + 大 `Tm` 矩阵（a=11.12728）排版时两者单位差 11 倍 → `_has_word_gap`（`layout_helper.py:225`）的 `extra = next.box.x − (prev.box.x + advance)` 虚高 → 每对字符间都插空格 | **BabelDOC**（`il_creater_active.py:1341` `advance = char.adv`） | 同内容 2512 原版（Tm a=1.02，误差 2%）正常；测试.pdf（a=11.127）全坏。`typesetting.py:533` 把 `advance*scale` 与 `width*scale` 同乘，证明 IL 层约定 advance ∈ device space |
| A2 | 模型合法语序重排（"coordinates T rounds across S sub-agents" → "在 S 个子智能体间协调 T 轮"）被 `repair_target` reorder 模式强行回贴 → 最终 PDF "在 𝑇 个子智能体间协调 𝑆 轮"，语义颠倒 | reorder 假设"锚点顺序不可变"，但中英翻译语序调整是常态；修复只保证协议合法、牺牲语义 | **BabelDOC**（`markdown_view.py:repair_target`） | P01-017 实测：translated.jsonl `{v5}=T、{v6}=S`，修复后互换 |
| A3 | 正文 `(e.g.,` 被吞进公式占位符，最终译文出现裸 `(e.g.,` 括号不配对 | MinerU 把 `(e.g.` 误判为 `inline_equation` span（`'( \mathsf { e . g . }'`，左括号被吞、右括号留在正文）→ `InlineMathProtector` 按 span **bbox** 把区域内所有原生字符（含正文字体 Ty1/XCharter-Roman 的 `(e.g.,`）保护成 formula | **MinerU span 边界切错为主，BabelDOC "bbox 全保护"策略放大为次** | alignment.json `span_text_samples` 14 处 mismatch；字符层 dump 证实 `( e . g . ,` 全是正文字体 |

补充事实（影响方案设计）：
- MinerU 缓存（`~/.cache/babeldoc/mineru-layout.v1/<sha256>.json`）里 span 文本是干净的 → **逐字符空格与 MinerU 无关**。
- 同页内容两个 PDF 的 MinerU 结果 span 完全一致（backend=hybrid, effort=medium, v3.4.4）→ 服务端对相同像素内容返回稳定；差异只在 BabelDOC 原生字符层。
- BabelDOC 只用 MinerU 的 bbox（LaTeX 文本被丢弃），所以 A3 的关键是 **bbox 边界**而非 OCR 文本质量。
- MinerU API v4 当前 payload：`model_version=vlm`（服务端 3.4.4 已是 hybrid 引擎）、`enable_formula=True`、`enable_table=True`、**未传 language**。可调参数：`language`（OCR 语言，默认 ch）、`is_ocr`、`page_ranges`、`no_cache`。
- PaddleOCR-VL-1.6（0.9B VLM + PP-DocLayoutV3）OmniDocBench v1.6 达 96.3%，本地 Apple Silicon 可跑（CPU paddlepaddle 3.2.1 + `paddleocr[doc-parser]`）；输出 `parsing_res_list`（block 级 bbox/label/content/order）+ `layout_det_res.boxes`（label 含 formula）——但**没有 MinerU 的 line/span 级结构**，接入需要适配层。

## 用户决策与方向（本轮对话确认）

1. **A2：删除 reorder**。删除后失去的保障 = "锚点顺序错乱但多重集一致"的段落不再被修复——但实测这类"错乱"多数是模型合法语序调整，reorder 反而制造语义错误。删除后依靠：(a) `protocol.check_placeholders` 仍校验**多重集**（丢锚点/幻觉锚点照旧拦截）；(b) `parse_translate_output` 按 regex 逐个匹配占位符，顺序无关，天然支持任意顺序回填。proportional 模式（锚点有增删时的等比投放）保留——它只在多重集不一致时触发，是协议兜底。
2. **A3：字体护栏有回归风险，需谨慎设计**。风险：有些 PDF 公式区域字符确实用正文字体（如斜体变量 `L`、`O(k)` 用正文 Italic），一刀切"正文字体不吞"会把真公式拆散。方向：用已有的 `span_text_consistent`（`provider_alignment.py:438`，宽松字母集比对 ≥0.6）做**入口修剪**——span 文本与区域内原生字符字母集不一致时收缩 bbox，只保护一致部分；一致时保持现状。这是保守增强，默认行为对正常 span 无影响。
3. **升级解析引擎**：(a) MinerU API 传 `language` 参数实验；(b) 引入 PaddleOCR-VL 作为**可选第二布局后端**，本地部署（用户电脑跑得动），走 `--layout` 开关，与 MinerU 并存对比。

## 大项目结构

```
A1 advance 单位修复（独立、无争议、先行）
A2 锚点协议：删 reorder（独立、小）
A3 MinerU bbox 护栏（依赖 A1 后重测的干净基线）
B  MinerU 参数实验（language 等，独立可并行）
C  PaddleOCR-VL 备选后端（大，最后做，B 的结论会输入 C 的必要性判断）
```

依赖关系：A1 → A3（A3 的验收要在 A1 修复后的干净文本上做）；A2 独立；B 独立（随时可跑）；C 依赖 B 的 MinerU 参数结论（若 language=en 显著减少 A3 类误判，C 的优先级降低）。

---

## 执行方式

```bash
# 子代理（AGENTS.md leaf worker，不再委派，不 commit）
pi -p --no-session \
  @.plan/parse-quality/briefs/<task-id>.md \
  "Execute the attached brief directly as a leaf implementation worker. Do not delegate. Follow AGENTS.md. Validate your changes and report the result."
# 主 Agent 验收
python3 -m pytest tests/ -q
python3 -m babeldoc.tools.agent md-extract "测试.pdf" --workdir tmp/<dir>   # 回放 MinerU 缓存，无需 token
# 翻译全链路（需要 agy/网络）
python3 experiments/markdown_translate.py tmp/<dir> --output-dir tmp/<dir>/output
```

brief 固定结构（`briefs/_template.md`）：`# Task / ## Objective / ## Context / ## Deliverables / ## Constraints / ## Validation / ## Report back`。并行任务用独立 worktree。

测试 PDF 基线约定（所有任务共用）：
- `测试.pdf` = 坏例（Tf 1 型，A1 触发）；`2512.08296v3.pdf` = 好例（正常字号型）。
- MinerU layout 已缓存（sha256 keyed），md-extract 直接回放，不消耗 API 配额。
- 单测基线：`pytest tests/ -q` 当前 308 passed / 7 failed（既有环境失败，勿修勿增）。
- 中间产物全部落 `tmp/`（已 gitignore）。

---

## A1 — advance 单位修复（根治逐字符空格）

| 任务 | 目标 | 涉及 | 验收 |
|---|---|---|---|
| A1-1 单测先行 | `tests/test_char_advance.py`：构造 Tm a≠1 的 AWLTChar 场景（或直接用 `测试.pdf` 的 state 断言）——`_has_word_gap` 在修复前假阳性、修复后正确 | 新测试文件 | 修复前 FAIL，修复后 PASS |
| A1-2 il_creater_active 修复 | `project_native_char`：`advance = char.adv * sx`，`sx = hypot(matrix[0], matrix[1])`；vertical 字符（`font.is_vertical()`）用 `sy = hypot(matrix[2], matrix[3])`；`char.adv` 为 None/0 时保持 None（下游已有 fallback） | `il_creater_active.py:1296-1341` | 测试.pdf 重跑 md-extract，document.md 无 `S i n g l e` 型逐字符空格；2512 p7 输出与修复前逐字节一致（diff 为空） |
| A1-3 旧 frontend 同修 | `il_creater.py:1026` `advance = char.adv` 同样问题（虽当前主链路不经过，保持一致） | `il_creater.py` | grep 无残留裸 `advance = char.adv` |
| A1-4 回归三连 | 2512 p7 + 2512 p1,2 + 2026-f1872（若缓存可用）跑 md-extract，输出与基线 diff；`pytest tests/ -q` 基线不变 | 全部 | diff 仅在预期处；测试数不变 |

**commit：** `修复 PdfCharacter.advance 单位不一致：text-space 换算 device-space，根治 Tf 1 型 PDF 逐字符空格`

### A1 已验证的技术细节（brief 引用）
- 修复公式：`advance_device = char.adv * hypot(m[0], m[1])`。测试.pdf 实测：S 的 `box` 宽 6.32 = 0.568 × 11.127（=|Tm a|），修复后 advance=6.32 与 box 对齐，`extra = next.x − (x + 6.32) ≈ 0` → 不再假插空格。
- 2512（a=1.02）修复后 advance 从 6.196 → 6.32（+2%），仍在 `_has_word_gap` 阈值 0.6 以下 → 词间空格判定不变，输出不变。
- `typesetting.py:533/596` 把 advance 与 width 同乘 scale、`:844` advance=char_width，证明 IL 约定 advance 与 box 同空间——修 frontend 是对齐约定，不是改约定。

---

## A2 — 锚点协议：删除 reorder 修复（小任务，独立）

| 任务 | 目标 | 涉及 | 验收 |
|---|---|---|---|
| A2-1 删 reorder 分支 | `repair_target` 删除"多重集一致但顺序不同 → reorder 回贴"模式；**保留 proportional**（多重集不一致时等比投放，协议兜底）。`apply_markdown` 中 `src_seq != tgt_seq` 触发修复的分支改为：多重集一致且顺序不同 → **接受模型顺序**（只修 `_fix_empty_spans`），记入 `warnings`（`anchor_reordered: id X`）供观察；多重集不一致 → proportional | `markdown_view.py:230-282` | 单测：reorder 场景译文原样通过；丢锚点场景仍被修复/拦截 |
| A2-2 语义颠倒回归用例 | 测试：P01-017 型输入（F5/F6 交换）→ apply 后 target 保持模型顺序，最终 PDF 文本含"在 S 个子智能体间协调 T 轮"语义 | `tests/test_markdown_format.py` 追加 | 断言 F5 仍对应轮数位置 |

**commit：** `锚点协议尊重模型语序重排：删除 reorder 强制回贴，保留多重集校验与 proportional 兜底`

### A2 已验证的技术细节（brief 引用）
- 下游兼容（删除 reorder 不引入严重问题）的证据链：
  1. `parse_translate_output`（`il_translator.py:777`）用 `re.finditer(combined_pattern)` **按出现顺序**逐个匹配占位符 → 顺序无关，任意顺序的 `{vN}`/`<style>` 都能正确回填到对应 composition。
  2. `workflow.apply` → `protocol.check_placeholders` 校验的是**多重集**（Counter 差集），顺序无关。
  3. 唯一依赖顺序的是 `repair_target` 自己和 `apply_markdown:912` 的 `anchor_sequence(body) != src_seq` 判等——本任务就是改这一处。
- 风险边界：模型若把锚点搬运到完全无关的句子（非语序调整），删除 reorder 后不再自动修复 → 用 `warnings` 里的 `anchor_reordered` 观察真实发生率，二期再决定是否需要语义级校验。
- `_fix_empty_spans`（`[[SN]][[/SN]]X → [[SN]]X[[/SN]]`）逻辑保留，与顺序无关。

---

## A3 — MinerU bbox 修剪护栏（依赖 A1 的干净基线）

| 任务 | 目标 | 涉及 | 验收 |
|---|---|---|---|
| A3-1 基线重测 | A1/A2 合入后重跑 `测试.pdf` 全链路，量化剩余问题（预期只剩 `(e.g.,` 类 A3；逐字符空格与语义颠倒消失）；dump alignment.json 的 `span_text_samples` 全量清单 | tmp 产物 | 基线报告（问题段落清单） |
| A3-2 span 一致性修剪 | `inline_math_protector.protect_page`：对每个 inline_equation region，先对齐区域内原生字符（复用 `align_page` 的 char_bbox 匹配），计算 `span_text_consistent(span.content, native_text)`；**不一致时收缩 bbox**：从左/右逐字符收缩边界直到一致或区域宽 < MIN_REGION_SIZE（放弃保护，退回正文）；一致时保持现状 | `inline_math_protector.py:100-160`、复用 `provider_alignment.span_text_consistent` | `(e.g.` span：收缩后 `(e` 或整段退回正文（该字符正文字体）；正常公式 span（`|A|>1` 等）bbox 不变；alignment mismatch 数下降 |
| A3-3 修剪审计 | `protected_inline_math` 条目加 `trimmed: {old_box, new_box, reason}` 字段；`alignment.json` 汇总 trimmed 数 | 同上 | 审计可查 |
| A3-4 回归 | 2512 p1,2/p7 + 测试.pdf 全链路；重点：公式段（`|A|=1`、`O(k)`）仍被保护（PDF 公式图形不丢）、`(e.g.,` 回归正文、括号配对 | 全链路 + 目视渲染 PNG | `(e.g.,` 修复且公式零回归 |

**commit：** `InlineMathProtector 按原生字符一致性修剪 MinerU 公式 span 边界，防止吞正文`

### A3 已验证的技术细节（brief 引用）
- `(e.g.` 案例数据：MinerU span content=`'( \mathsf { e . g . }'`，区域内原生字符=`( e . g . ,`（全 Ty1 正文字体）。`span_text_consistent` 字母集比对：span letters `{e,g}` vs native `{e,g}` → **恰好一致**！所以简单调用 `span_text_consistent` 分不出这个案例（右括号在 span 外）。
  → 修剪判据需升级：**不平衡括号检测**——span content 的括号不平衡（`(` 无 `)`）且收缩边界能恢复平衡时，收缩到平衡点；或 span 首字符是 `(`/`[` 且 native 同位置字符属于正文字体（font_id 非公式字体）时，从 bbox 左界剔除正文字体字符直到遇到公式字体。
  实测该页公式字体集合：`XCharterMathMI/RM、txmiaX、txsys、SFTT1095`；正文 `Ty1`。判据用"区域内字符 font_id ∈ 该页公式字体集合"最直接（数据在 `page_font_map`/`_page_font_maps`）。
- 正常公式 `(|A|=1` 的原生字符 `(|𝐿|=1` 是 XCharterMathMI → 不被修剪。
- **回归风险点**（用户关注）：有些 PDF 斜体变量用正文 Italic 字体（2512 的 `ω` 是 XCharterMathMI，但别的论文可能用 Italic）→ 纯字体判据会误伤。所以双判据 AND：字体不同 **且**（括号不平衡 或 字母集不一致）才修剪；单变量 Italic 公式（字体不同但字母集一致且括号平衡）保持保护。A3-4 回归重点验证此边界。

---

## B — MinerU API 参数实验（独立，随时可跑）

| 任务 | 目标 | 涉及 | 验收 |
|---|---|---|---|
| B-1 language 参数 A/B | `MinerUDocLayoutModel` 增加 `language` 透传（构造参数已有，`workflow.py`/`markdown_view.py` 调用点加 CLI 选项 `--mineru-language`）；对 `测试.pdf` 分别跑 `language='en'` vs 默认 ch，对比 `span_text_samples` mismatch 数与 `(e.g.` 类边界错误 | `workflow.py:224`、`markdown_view.py:413`、`__main__.py`、`mineru_doclayout.py` | A/B 报告（mismatch 计数、边界案例清单）落 tmp |
| B-2 缓存 key 纳入参数 | 当前缓存 key 只有 PDF sha256 → 同 PDF 不同参数会命中旧缓存。key 改为 `sha256 + model_version + language`（`_layout_cache_path`） | `mineru_doclayout.py:394` | 同 PDF 两参数两份缓存；回放路径不受影响 |

**commit：** `MinerU 布局支持 language 参数与参数化缓存 key，英文论文 A/B 实验`

说明：B-1 是**实验任务**（主 Agent 自己跑，不委派——需要网络与 API 配额），产出数据决策"language=en 是否纳入默认值"。MinerU API v4 的 `model_version` 我们已用 `vlm`（服务端 3.4.4 = hybrid 引擎，文本 PDF 场景号称 ~95%），`pipeline`（~86%，CPU 稳定）作为后续对照选项保留在 CLI，不在本计划强制实验。

---

## C — PaddleOCR-VL 备选布局后端（大项目，最后做）

> 触发条件：B-1 结论为 language=en 仍不能显著降低 A3 类边界错误，或用户想本地化摆脱 MinerU API 配额/网络依赖。

| 任务 | 目标 | 涉及 | 验收 |
|---|---|---|---|
| C-1 环境与 spike | 本地安装 `paddlepaddle==3.2.1`（CPU）+ `paddleocr[doc-parser]`；对 `测试.pdf` 跑 `PaddleOCR-VL-1.6` doc_parser，dump 完整 JSON 结构（parsing_res_list + layout_det_res），确认：公式 block 的 bbox 精度、`(e.g.` 案例的边界表现、文本 block_content 质量 | 无产品代码改动，产物落 tmp | spike 报告：`(e.g.` 是否被正确切为正文；公式 bbox 与 MinerU 对照 |
| C-2 适配层 | `babeldoc/docvision/paddle_doclayout.py`：`PaddleDocLayoutModel(DocLayoutModel)`，输出 YoloResult（layout 区域）+ ProviderDocument（block→span 降维：text block → 1 个 text span；formula block → 1 个 formula span + content 里的 LaTeX）。复用 `MinerUDocLayoutModel` 的接口契约（stride/handle_document/provider_document 属性） | 新文件 + `DocLayoutModel` 注册 | 单测：适配层 JSON→ProviderDocument 转换 |
| C-3 CLI 接入 | `--layout paddle` 选项（现只有 mineru）；`_run_parse` 分支 | `markdown_view.py:391`、`workflow.py`、`__main__.py` | `md-extract --layout paddle` 跑通测试.pdf |
| C-4 对比与决策 | 同一 PDF 双后端全链路：布局覆盖率、`(e.g.` 类错误数、公式保护正确性、耗时（CPU 推理速度） | tmp 产物 + 报告 | 决策报告：默认后端是否切换/何时用哪个 |

**commit：** `引入 PaddleOCR-VL 本地布局后端：适配层、CLI 接入与双后端对比`

### C 前期调研结论（brief 引用）
- PaddleOCR-VL-1.6 = PP-DocLayoutV3（布局+阅读顺序）+ 0.9B VLM（识别），OmniDocBench v1.6 96.3%；Apple Silicon 本地推理支持（M4 已验证），CPU 亦可跑（0.9B 模型 + paddlepaddle 3.2.1 CPU 版）。
- 输出：`parsing_res_list[] = {block_bbox, block_label, block_content, block_id, block_order}`（block 级）；`layout_det_res.boxes[] = {cls_id, label, score, coordinate}`（block 级，label 含 `formula`）；Markdown/JSON/Word 保存。
- **结构性差异**：没有 MinerU 的 line/span 两级结构；公式在 block_content 里是 `$...$` LaTeX。适配层把 formula block 映射为单 span（bbox = block bbox）——精度上 block bbox 可能比 MinerU 的 span bbox 更粗，C-1 spike 需重点验证 A3 类边界（这正是 PaddleOCR-VL 的优势假设：VLM 直接看图像识别公式区域，不像 MinerU 的公式检测模型容易把括号吞进去）。
- Python API：`pipeline = create_pipeline(pipeline="PaddleOCR-VL-1.6"); output = pipeline.predict(input="x.pdf")`；`res.save_to_json()`。
- 若 CPU 推理太慢（>30s/页），备选：mlx-vlm-server（Apple Silicon GPU 加速，`vl_rec_backend="mlx-vlm-server"`）。

---

## 风险与回退

- A1 是**行为变更**：2512 类正常 PDF 的 advance 值也会变（+2%）。`_has_word_gap` 阈值 `max(0.1*font_size, 0.6)` 对 ±2% 不敏感，预期输出不变；但 `typesetting.py:533` 重建字符、LaTeX bbox 源几何等消费 advance 的路径都要在 A1-4 回归中覆盖（尤其既有三篇论文 LaTeX 回放）。
- A2 删除 reorder 后，若观察到 `anchor_reordered` warning 比例高且伴随错位（非语序调整类），再考虑语义级方案（如公式锚点带内容提示 `{v5:T}`），那是二期。
- A3 双判据仍有误伤可能（公式字体集合随论文变体）→ 修剪只收缩不扩张，最坏退化为"该 span 不保护"（回到正文被当公式翻译的老问题），不产生新类型错误。
- C 的 PaddleOCR-VL block 级 bbox 若过粗，公式保护退化 → C-1 spike 先验证，不达标则 C 降级为"实验特性"不入默认链路。

## 总体验证（全部完成后）

1. `pytest tests/ -q` 基线 308 passed / 7 failed 不变 + 新增测试全过。
2. `测试.pdf` 全链路（md-extract → translate → apply → reconstruct → render）：document.md 无逐字符空格；`(e.g.,` 为正文；P01-017 语义正确（S 个智能体 / T 轮）；渲染 PNG 目视正常。
3. `2512.08296v3.pdf`（好例）全链路输出与修复前 diff 为空或仅在 `(e.g.` 修复处。
4. 三篇 LaTeX 论文回放（若有缓存）不回归。
