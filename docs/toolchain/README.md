# BabelDOC 工具链技术文档

> 本目录描述 **MinerU 深度融合** 之后的工具链：从 PDF 到 mono/dual 成品的完整数据流、
> 每层的不变量、每个子命令的真实契约、门禁与排查方式。
>
> 定位：这是**实现参考**，不是入门教程。每个论断都给出代码位置或可复现命令；
> 面向「出问题时该看哪个产物、该跑哪条命令」。入门请先读
> [`skills/document-translate/SKILL.md`](https://github.com/zcyisiee/ieeTranslater/blob/main/skills/document-translate/SKILL.md)。

本目录只有三个文件：

| 文件 | 内容 |
|---|---|
| 本文件 | 唯一入口 `bdt`、数据流与分层不变量、门禁速查、label 字典 |
| [`pipeline-stages.md`](pipeline-stages.md) | 每阶段输入/输出 artifact、关键字段、失败模式 |
| [`troubleshooting.md`](troubleshooting.md) | 按症状排查：目录、链接、漏译、公式、字号、圈号 |

---

## 1. 唯一入口 `bdt`

工具只有一个入口：`bdt`（= `python -m babeldoc_tools`，argparse 子命令）。
`pyproject.toml` 的 `[project.scripts]` 只注册它；仓库里只有一个 `babeldoc_tools`
包，位于仓库根，任意 cwd 下命中的都是这一份代码。

约定：stdout 恒为单行 `{"ok": true, "data": {…}}` 或
`{"ok": false, "error": {"code": …, "message": …}}`；日志/进度走 stderr；
退出码 0/1（2 = argparse 用法错误）。`registry.invoke(fn, **kwargs)` 是唯一的信封包装点。

| 子命令 | 实现函数 | 一句话 |
|---|---|---|
| `parse` | `parse.parse_document` | PDF → 连续 Markdown（锚点）+ IR 状态 |
| `translate` | `translate.translate_document`（`--ids` → `retranslate_blocks`） | 整篇翻译（`--translator <命令>`），或 `--markdown` 导入 / `--prompt-only` 取提示词 |
| `apply` | `translate.apply_translation` | 校验译文写回 IR |
| `build` | `layout.build_pdf` | 应用排版覆盖重排生成 mono/dual PDF + dump 几何；`--render` 渲染页 |
| `check` | `review.check_document` | 三合一聚合：结构审查 + 排版 lint + 链接审计 |
| `layout-set` | `layout.layout_set` | 写/清排版覆盖 |
| `report` | `report.report` | 产出 `FINAL_REPORT.md` |
| `run` | `run.run_pipeline` | 串联上述阶段，可 `--from` 续跑 |

内部 Python 函数（已从公开 CLI 移除，供 reviewer agent/脚本调用）：
`layout.layout_lint`、`layout.layout_locate`、`layout.render_pages`、
`layout.dump_text_layer`、`review.backtranslate_check`。

> 查子命令参数：`bdt <subcommand> --help`（U2 起不再有 `list` / `schema` 元命令）。

翻译与审查的 provider 机制只有一种：把提示词写进被调命令的 stdin、从它的 stdout 读结果，
退出码 0 表成功（`common._run_subprocess`）。命令经 `shlex.split` 拆成 argv、**不经 shell**；
模型、档位、JSON 解包等细节属于被调命令自己的事（仓库 `scripts/` 下有两个 wrapper）。

### 编排：`bdt run`

`bdt run <pdf> --workdir <WD> [--translator <cmd>] [--reviewer <cmd>] [--dual]` 按
`parse → translate → apply → build → check → review → report` 顺序执行。要点：

- 每个要执行的阶段先校验所需 artifact（translate 前 `agent/document.md`、apply 前
  `agent/translated.md`、build 前 `agent/apply_report.json`、check 前 build 的 PDF），
  缺失时以 `missing_artifact` 报错（exit 1）并指明该跑哪一步；
- `agent/run_state.json` 记录每阶段完成标记与关键输入 sha256（`document.md` /
  `translated.md` / `layout_overrides.json` / PDF）；
- `--from {parse,translate,apply,build,check,review,report}` 续跑时，若被跳过阶段的输入
  哈希与记录不符，报 `stale_upstream` 并给出 `suggested_from`（最早受影响阶段），不静默
  沿用旧产物；
- 无 `--reviewer` 时停在 review 并以 `waiting_for_reviewer` 失败（exit 1）；reviewer
  返回 `needs_fix` 时 findings 映射成 `actions` JSON 并以 `reviewer_needs_fix` 失败；
- `check` 用 `--strict` 语义：verdict 非 pass 时仍跑完 reviewer 与 report，但整体 exit 1。

---

## 2. 数据流与分层

```text
输入 PDF
  │
  ├─[0] _prepare_pdf ────────── PDF 修复（null xref / filter / mediabox），保留 Link 注释
  ├─[1] native parse ────────── page.pdf_character（扁平字符流，唯一写回真源）
  ├─[2] LayoutParser ────────── MinerU 布局 → page.page_layout；落盘 provider_ir.json
  │                              + <workdir>/<pdf名>/layout_coverage.json（覆盖率门禁）
  ├─[3] InlineMathProtector ─── inline_equation span → formula 区域；落盘 alignment.json
  ├─[4] EnclosedMarkerFixer ─── 圈号修复
  ├─[5] ParagraphFinder ─────── 聚类成行/成段；分配 layout_label 与 debug_id（P02-003）
  ├─[6] TocDetector ─────────── 目录条目化；落盘 source/toc.json
  ├─[7] StylesAndFormulas ───── 同样式 run 合并、公式对象（PdfFormula）识别
  ├─[8] 快照 ────────────────── 书签 → source/bookmarks.json；超链接 → source/links.json
  ├─[9] ILTranslator.pre ────── canonical 源文（<style>/{vN}）+ document.md/anchors.json
  ├─[10] bdt translate ──────── 一次调用翻译整篇 document.md → translated.md
  ├─[11] bdt apply ──────────── 校验 + 写回 IR → translated.jsonl / apply_report.json
  ├─[12] Typesetting ────────── 重排 → layout_geometry.json
  ├─[13] PDFCreater ─────────── mono = 源容器 + 重生成内容流；链接重映射 + URI 集合门禁
  └─[14] bdt build --render ─── 代表性页 PNG（视觉审查）
```

**分层不变量**（改动任何一层时先确认这些仍成立）：

```text
L7 交付层    mono/dual PDF + render PNG + FINAL_REPORT.md     PDFCreater + 链接重映射
L6 排版层    Typesetting：源字符 box 原地改写 / 译文新建字符     → layout_geometry.json
L5 翻译协议  段落 id + 样式锚点 [[S1]] + 公式锚点 [[F3]]        ILTranslator + markdown_view
L4 段落 IR   PdfParagraph / PdfParagraphComposition            ParagraphFinder 等
L3 原生字符  page.pdf_character = 唯一写回真源                   native_parse.py
L2 provider  ProviderDocument（MinerU block/line/span 树）       provider_ir.py
L1 布局层    page.page_layout：PageLayout(box, class_name, conf) MinerUDocLayoutModel
L0 PDF 对象  pymupdf：内容流、注释（Link/TOC）、xobject、字体
```

| 不变量 | 内容 |
|---|---|
| I0.1 | `_prepare_pdf` 之后页数与源 PDF 一致（回放校验依赖） |
| I0.3 | 书签绑定「页号 + 目标点」，页序变化时必须重映射 |
| I1.1 | `mineru` / `paddle` 是布局后端；无布局模型时 `TranslationConfig` 直接报错，不静默回退 |
| I1.3 | 覆盖率门禁：未命中任何 `page_layout` 的原生字符占比 ≤ `--layout-coverage-threshold`（默认 0.005） |
| I2.1 | provider IR 只读参考：MinerU `content` 不覆盖原生字符（只用于公式 token 决策与一致性审计） |
| I3.1 | 原生字符是唯一写回真源；公式/跳过段落原样 passthrough |
| I3.2 | 覆盖率统计必须在 `ParagraphFinder` 之前（之后只剩被跳过字符） |
| I3.3 | 字符对象身份跨 `pickle` 保持（链接重映射依赖） |
| I4.1 | `debug_id` 格式 `P{页:02d}-{序:03d}`，跨运行可复现 |
| I4.3 | `char.formula_layout_id` 由 `ParagraphFinder` 无条件重写；「让某段变公式」只能追加 formula 区域 |
| I4.4 | 目录条目化只替换「整段所有行都是目录条目」的段落 |
| I5.1 | 翻译单元是**整篇**（`document.md` 一次调用） |
| I5.2 | 锚点多重集必须与源文一致；顺序不强制（中英语序调整合法），顺序不同只记 `anchor_reordered` |
| I5.3 | 漏行回退原文（`fallback_ids`），不阻断重建 |
| I6.1 | 源字符 box 原地改写；译文新建字符对象 |
| I6.2 | 无排版覆盖（`layout_overrides.json` 不存在）时行为与改造前一致 |
| I7.1 | mono = 从源 PDF 打开 + 重写每页内容流 |
| I7.3 | URI 集合门禁：mono 输出的 URI 集合必须等于源快照集合（删页模式跳过） |

> 解析链两条入口（`markdown_view._run_parse` 与 `workflow.extract`）的同构性由
> `tests/test_provider_alignment.py` 钉住：改动解析链时两条都要改。

---

## 3. 门禁速查

判定分两类：**硬门禁**失败即中止当前阶段并报错（宁可失败，不可静默交付）；
**软门禁**只记警告并继续。聚合查看：

```bash
python experiments/toolchain_gates.py <workdir> [--pdf <源pdf>] [--json]
```

| 门禁 | 硬/软 | 阈值 | 产物 | 阻断点 |
|---|---|---|---|---|
| `layout_coverage_gate` | 硬 | 未覆盖占比 ≤ 0.5% | `<workdir>/<pdf名>/layout_coverage.json` | `LayoutParser.process` |
| `link_uri_set_mismatch` | 硬 | 集合相等 | `reconstruct_report.json`（`link_uri_set_match`） | `PDFCreater.write` |
| protocol（anchors/占位符） | 硬 | 0 违规 | `apply_report.json` | `apply_markdown` |
| `link_unresolved` | 软 | 记数（建议 0） | `reconstruct_report.json`（`link_unresolved[]`） | — |
| `toc_low_confidence` | 软 | 置信度 ≥ 0.6 | `agent/source/toc.json` | — |
| `layout_lint` | 软 | 记发现 | `agent/layout_lint.json` | — |
| `acceptance_latex`（LaTeX bbox 默认开启时） | 软 | 应用率 ≥95%、fill ≥99%、text_diff=0 | `<workdir>/<pdf名>/latex_bbox_report.json` + `<workdir>/acceptance/` | — |

**覆盖率门禁**（`layout_coverage_gate`）：超阈值说明「有一部分原生文字不在任何布局区域
内」——这些文字既不会被翻译，也不在保护区域内。超阈值时**必须人工确认**是真漏排还是
扫描件/异形版式的正常现象。常见修复：调大 `--layout-coverage-threshold`、清缓存重跑
MinerU、用 `--pages` 缩小范围定位。

**协议门禁**（`apply_markdown` + `workflow.apply`）：`extra_ids`（伪造段落 id）与
`anchor_multiset_mismatch`（锚点多重集不一致，`proportional` 修复后仍不一致）**阻断**；
`missing_ids`（回退原文）、`empty_translation`、`label_mismatch`、`empty_style_span`、
`anchor_reordered` 只警告。CLI 层面 `violations` 非空 → `"ok": false`，退出码 1。

**LaTeX bbox 验收**（软，默认开启时）：应用率 `applied/eligible ≥ 95%`、applied 段
非末行 fill ≥ 0.98 的行占比 ≥ 99%、贴片文本层零差异。跑
`python3 experiments/acceptance_latex.py <workdir> <mono.pdf>`。

---

## 4. layout label 字典

label 由两级映射产生：`MinerU block.type → layout_label → 翻译决策`；增补来源有
`TocDetector`（`toc_entry` / `toc_entry_page`）、`InlineMathProtector`（`formula` 区域）、
`LayoutParser`（词表外区域名）、启发式（`author`）。

「是否翻译」有两个入口且必须一致：`tools/agent/translation_selection.py::PROTECTED_LABELS`
（+ `CAPTION_LABELS` 白名单）与 `TranslationConfig.MINERU_DEFAULT_SKIP_TRANSLATE_LAYOUT_LABELS`
经 `MINERU_SKIP_TRANSLATE_ALIAS_MAP` 展开。

| MinerU block type | layout_label | 翻译 | 说明 |
|---|---|---|---|
| `text` | `text` | ✓ | 正文 |
| `title` | `title` | ✓ | 标题（论文主标题与章节标题不细分） |
| `interline_equation`, `equation` | `formula` | ✗ | 行间公式（整块图形，保护） |
| `ref_text` | `reference` | ✗ | 参考文献条目 |
| `table_caption` | `table_caption` | ✓ | 表注 |
| `table_body` | `table_text` | ✗ | 表格内部文字 |
| `table_footnote` | `table_footnote` | ✗ | 表脚注 |
| `image_caption`, `chart_caption` | `figure_caption` | ✓ | 图注/图题 |
| `image_footnote` | `figure_text` | ✗ | 图内说明文字 |
| `image_body`, `chart_body`, `chart` | `figure` | ✗ | 图/图表本体 |
| `header` / `footer` / `page_number` / `page_footnote` / `aside_text` | 同名 | ✗ | 页眉/页脚/页码/脚注/边栏 |
| `code`, `algorithm`, `code_body` | `code` | ✗ | 代码/算法/伪码块 |
| `code_caption` | `code_caption` | ✓ | 代码题注 |
| `list` + `sub_type=ref_text` | `reference` | ✗ | 容器 |
| `list` | `list_item` | ✓ | 列表项 |
| 未知类型 | `text`（有 lines）/ `abandon`（无） | — | 同时记入 `provider_ir.unknown_types[]` |

增补 label：

| label | 来源 | 翻译 | 说明 |
|---|---|---|---|
| `toc_entry` | `TocDetector` | ✓ | 目录条目的标题部分；独立 id |
| `toc_entry_page` | `TocDetector` | ✗ | 点引导线 + 印刷页码；原字符 passthrough |
| `formula`（区域） | `InlineMathProtector` | — | 由 MinerU `inline_equation` span 追加 |
| `author` | 启发式（首页 + 标题与摘要之间） | ✗ | MinerU 不标作者区 |
| `paragraph_title` / `doc_title` | 词表 | ✓ | MinerU 路径下通常不出现 |

**保护原因分类**：坐标/图形资产（`figure`/`formula`/`table_text`/`toc_entry_page`）、
结构化短文本（`reference`/`page_number`/`code`）、非正文元数据（`author`/`header`/
`footer`/`page_footnote`/`aside_text`）、丢弃（`abandon`）。

**已移除项**：`fallback_line`（字符聚类兜底，由覆盖率门禁替代）、`provides_complete_layout`、
本地 ONNX 后端（`--layout native`）、`DocLayoutModel.load_onnx`、rpc 布局网关模块
（只被已删除的 executor/旧 CLI 引用，可 git 历史回溯）。

**实测标签分布**（DeepSeek 样本）：翻译行 352 = `text` 220 | `title` 59 |
`toc_entry` 54 | `figure_caption` 14 | `table_caption` 5；跳过行 234 =
`reference` 100 | `toc_entry_page` 54 | `page_number` 50 | `figure` 20 | `table_text` 5 等。

---

## 5. 离线回放（不消耗 MinerU API）

MinerU layout.json 按 **PDF 内容 sha256** 缓存在
`~/.cache/babeldoc/mineru-layout.v1/<hash>.json`。回放两种等价写法：

```bash
# ① 直接给缓存文件路径
uv run bdt parse paper.pdf --workdir tmp/docs-smoke --mineru-json <缓存 layout.json>

# ② 只给内容哈希（--mineru-cache-key），缓存未命中时明确报错
uv run bdt parse paper.pdf --workdir tmp/docs-smoke --mineru-cache-key <sha256>
```

---

## 6. 验收一条命令

```bash
python experiments/toolchain_gates.py <workdir> [--pdf <源pdf>] [--json]
```

对刚解析完（未重建）的 workdir，链接/协议门禁会降级为 `not_reconstructed` /
`not_available`（不算失败）；对完整走完 parse → apply → build 的 workdir，门禁全部可判定。
退出码：任一硬门禁失败 → `1` + `"ok": false`。
