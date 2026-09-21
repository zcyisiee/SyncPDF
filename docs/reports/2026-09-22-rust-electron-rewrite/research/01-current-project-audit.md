# 现状审计：Python → Rust 后端 + Electron 前端重写调研

日期：2026-09-22。证据路径均相对仓库根（本 worktree）。核对方式：通读 `AGENTS.md`、`ARCHITECTURE.md`（核对日期 2026-09-19）、`docs/reference/pipeline.md`、`docs/reference/http-api.md`、`docs/design/online-translation.md`、`docs/issues/index.md`、`README.md` 后深入源码逐条验证。**文档与代码基本一致，本文以代码为准。**

一句话总览：这是一条「原生字符 IR + MinerU/Paddle 布局 → 锚点 Markdown 翻译协议 → XeLaTeX bbox 贴片排版 → 链接对象身份重映射」的单机管线，外面套 FastAPI + SQLite + 内容寻址资产的本地服务，再套 React 三栏工作台。

---

## 0. 仓库地图（与重写相关的部分）

| 目录 | 职责 | 备注 |
|---|---|---|
| `babeldoc/format/pdf/new_parser/` | 自研 PDF 内容流解释器（原生字符抽取） | 上游 BabelDOC 遗产，Python 版最重的部分 |
| `babeldoc/format/pdf/document_il/` | DocIL 中间表示（frontend/midend/backend） | **注意：在 `format/pdf/` 下，不是仓库顶层** |
| `babeldoc/format/pdf/document_il/backend/latex_bbox/` | XeLaTeX 贴片渲染（renderer/overlay/fusion/cache） | 本 fork 的核心增量 |
| `babeldoc/docvision/` | 布局提供方：MinerU 云 API / 本地 Paddle + provider IR | |
| `babeldoc/tools/agent/` | Markdown 协议、写回、重建、审计（内部库，无 CLI） | |
| `babeldoc_tools/` | `bdt` CLI：阶段编排、serve、stream preview | |
| `babeldoc_tools/serve/` | FastAPI 服务：路由、runner、草稿、SQLite、资产、局部编译 | |
| `web/src/` | React 工作台 | |
| `skills/document-translate/agents/` | 模型提示词（markdown 文件即协议） | |
| `tests/` | 76 个 Python 行为测试 | |

---

## 1. 解析层

### 1.1 new_parser：原生字符从哪来

`babeldoc/format/pdf/new_parser/` 是自研的 PDF 内容流解释器（tokenizer → interpreter → glyphs），把每个页面的内容流执行成 `PdfCharacter`（每个原生字符带 bbox/visual_bbox/style/advance）。入口 `parse_prepared_pdf_with_new_parser_to_legacy_ir`（`native_parse.py:21`）在解析管线中被调（`markdown_view.py:442`）。它同时保留 `baseOperations`（原始内容流串，`il_version_1.xsd:45`）与 pdfXobject/curve/form 等矢量对象，重建时原样重发内容流。**要点：重建 PDF = 在源 PDF 上重发内容流 + 替换段落字符，不是从零排版。**

### 1.2 解析管线（`markdown_view._run_parse`，`markdown_view.py:337-598`）

| 步骤 | 实现 | 输出 |
|---|---|---|
| 1 预处理 | `workflow._prepare_pdf`（裁剪页面、规范化 CropBox） | `temp_pdf_path` |
| 2 原生解析 | new_parser → legacy IR `docs` | 每页 `pdf_character` 全量 |
| 3 布局 | `LayoutParser(config)`（`markdown_view.py:454`），后端 = MinerU 或 Paddle | `page.page_layout`（区域框 + class_name）+ 覆盖率门禁 |
| 4 行内公式保护 | `InlineMathProtector`（`markdown_view.py:469`，无条件运行） | `inline_equation` span 覆盖字符 → formula 区域 |
| 5 段落 | `ParagraphFinder` + `EnclosedMarkerFixer` + `TocDetector`（:495-503） | `pdf_paragraph` |
| 6 样式/公式 | `StylesAndFormulas`（:504） | style id、`{vN}` 公式对象 |
| 7 确定性 id | `_deterministic_ids`（`markdown_view.py:265-273`）：`P<page+1两位>-<页内序号三位>`，如 `P01-001` | 段落身份 |
| 8 源行几何 | `capture_source_line_geometry`（:517-525，无条件，供 LaTeX bbox 用） | `source_line_geometry` |
| 9 链接/书签快照 | `_snapshot_links` / `_snapshot_bookmarks`（:299-334） | `agent/source/links.json`、`bookmarks.json` + state 内对象映射 |
| 10 选择翻译范围 | `select_page_paragraphs`（`translation_selection.py`）；参考文献/作者/页脚/图表内部默认不译 | `inputs`、`rows` |

### 1.3 DocIL 核心数据结构（`il_version_1.xsd` + `il_version_1.py`）

XML 序列化 schema；Python 侧是 `@dataclass`（`il_version_1.py`，1371 行）。坐标系：**PDF 坐标，左下原点 y 向上**（`provider_ir.py:14-16` 明确对比）。没有独立的 line/span/char 三层段落结构——段落 composition 是异构的。

| 元素 | 字段（XSD 行号） |
|---|---|
| `document` | `totalPages`（xsd:8） |
| `page` | `pageNumber`、`Unit`；子元素 mediabox/cropbox/pdfXobject*/pageLayout*/pdfRectangle*/pdfFont*/pdfParagraph*/pdfFigure*/pdfCharacter*/pdfCurve*/pdfForm*/baseOperations（xsd:11-30） |
| `box` | `x,y,x2,y2` float（xsd:46-53）——**统一四元组，无宽高字段** |
| `pdfParagraph`（= block） | `xobjId, unicode(译文承载), scale, optimal_scale, vertical, FirstLineIndent, debug_id(段落身份 P01-001), layout_label, layout_id, renderOrder`；子：box、pdfStyle、`pdfParagraphComposition*`（xsd:148-166） |
| `pdfParagraphComposition` | choice：`pdfLine` \| `pdfFormula` \| `pdfSameStyleCharacters` \| `pdfCharacter` \| `pdfSameStyleUnicodeCharacters`（xsd:167-177）——段落正文与公式混排 |
| `pdfLine` | `renderOrder`；子：box + `pdfCharacter*`（xsd:178-186） |
| `pdfStyle` | `font_id, font_size` + `graphicState.passthroughPerCharInstruction`（xsd:139-147）——**样式内联在每个字符上，非引用表** |
| `pdfCharacter` | `vertical, scale, pdfCharacterId, char_unicode*, advance, xobjId, debug_info, formula_layout_id, renderOrder, subRenderOrder`；子：pdfStyle、box、visual_bbox（xsd:98-116） |
| `pdfFormula`（= 公式锚点实体） | `x_offset*, y_offset*, x_advance, lineId, is_corner_mark`；子：box + `pdfCharacter*` + `pdfCurve*` + `pdfForm*`（xsd:205-219） |
| `pdfSameStyleCharacters`（= 富文本 span） | box + pdfStyle + `pdfCharacter*`（xsd:187-195）；翻译协议里的 `<style id='N'>` 对应它 |
| `pageLayout`（布局区域） | `id, conf, class_name` + box（xsd:124-133） |
| `pdfFont` | `name, fontId, xrefId, encodingLength, fontSubtype, type3FontMatrix/BBox/EmHeight, bold, italic, monospace, serif, ascent, descent` + 每字符 bbox 表（xsd:57-86） |
| `pdfFigure / pdfRectangle / pdfCurve / pdfForm` | 图形/表格对象与矢量路径（xsd:220-341），重建时原样保留 |

**没有 `link` 元素**：链接是独立子系统（`link_snapshot.py` 快照 + `state.pkl` 内 pickle 的对象引用），不在 DocIL 里。

### 1.4 MinerU 云 API 后端（`docvision/mineru_doclayout.py`）

| 项 | 值 | 证据 |
|---|---|---|
| base_url | `https://mineru.net`，model_version `vlm` | :60-62 |
| 提交 | `POST /api/v4/file-urls/batch`（files[] + enable_formula/enable_table + language）→ batch_id + 上传 URL；`PUT` 直传 PDF 分片 | :351-406 |
| 轮询 | `GET /api/v4/extract-results/batch/{batch_id}`，5s 间隔、900s 超时，state∈{done,failed} | :407-462, :63-65 |
| 分片 | >10 页切成 chunk_pages=10 的分片一个 batch 提交（≤50 chunks），结果 zip 按 `layout.json`（或 `*_middle.json`）解包再按页偏移合并 | :66-68, :645-663, :497-563 |
| 缓存 | `~/.cache/babeldoc/mineru-layout.v1/<pdf sha256>.json`，命中不打 API；源侧与译文侧识别共享同一缓存（不重复计费） | :664-690, :745-767 |
| 输出→YoloResult | `para_blocks`+`discarded_blocks` 深度优先取叶子 block → bbox + 类别；type→layout_label 映射表（text/title/formula/reference/table_*/figure_*/code/…，27 种已知） | :106-190, `provider_ir.py:38-72` |
| 特例 | 首页作者区识别（title 与 abstract 之间的 text 块 → `author` label） | :191-218 |
| 坐标 | MinerU 给显示视图坐标；`_unrotate_bbox` 用 `page.derotation_matrix` 转回未旋转 MediaBox **左上原点**坐标（原始缓存不改写）；provider IR 的 block/line/span 同步 unrotate | :286-298, :710-733 |
| 回放 | `BABELDOC_MINERU_LAYOUT_JSON` 环境变量或 `--mineru-json`/`--mineru-cache-key` 离线回放，页数不匹配报错 | :778-806, `markdown_view.py:276-296` |

### 1.5 本地 Paddle 后端（`docvision/paddle_doclayout.py` 等）

| 项 | 值 | 证据 |
|---|---|---|
| 模型 | PP-DocLayoutV3（布局检测 ONNX）+ PaddleOCR-VL（VLM 文本/公式识别）；ONNX Runtime + CoreML EP（Apple GPU，~0.1s/页），运行期失败自动降 CPU | `paddle_layout_regions.py:9-57` |
| 输入 | 每页按固定 DPI 栅格化（`with_fixed_dpi`，normalize_rotation），检测框从像素换算回**未旋转 media points**（`GEOMETRY_VERSION="unrotated-media-points-v2"`） | `paddle_doclayout.py:20, 287-313` |
| 标签表 | 25 个 PADDLE_LABELS → PADDLE_TO_LAYOUT 语义映射 → PADDLE_ROLES（protected/translate 二分：算法/图/表/公式/页眉脚/参考文献=protected） | `layout_labels.py` 全文 |
| 重试 | 页内原生字符有未被覆盖者 → 降阈值重试一次，仅当「覆盖严格更多且不丢任何已覆盖」才采纳 | `paddle_doclayout.py:95-127, 340-380` |
| 缓存 | 每页 JSON（cache_namespace + 源哈希 + 页号 + 页图哈希），带 runtime 契约校验（RUNTIME_CONTRACT） | :318-340 |
| provider IR | `build_provider_page` 把 VLM 解析结果保守对齐到原生字符（formula 只展平可理解的 LaTeX，分数/矩阵绝不猜） | `paddle_provider.py:20-58` |

### 1.6 provider IR 与坐标统一

**Provider IR**（`docvision/provider_ir.py`）：MinerU layout.json 的规范化镜像，完整保留 block/line/span 层级。

| 实体 | 字段 | 证据 |
|---|---|---|
| `ProviderDocument` | provider 名、revision、pages | :615- |
| `ProviderPage` | page_index、blocks（正文在前 discarded 在后）、reading_order | :253- |
| `ProviderBlock` | block_id(`p{页}-b{n}`)、type、sub_type、bbox、level、index、angle、lines、children（嵌套容器）、parent_block_id、merge_prev、source | :184-250 |
| `ProviderLine` | line_id(`…-l{m}`)、bbox、spans | :160- |
| `ProviderSpan` | span_id(`…-s{k}`)、bbox、kind（27 block 类型 + text/inline_equation/interline_equation/image/table/chart 6 span kind）、content、score、page_index、metadata | :120-158, :38-85 |

跨页段落修复：MinerU 会把跨页段落合并进前一页块、续页留 `lines_deleted:true` 空块；`repair_merged_cross_page_blocks` 把溢出行搬回物理页（:343-450）。

**坐标统一**（`document_il/utils/provider_alignment.py`）：

- provider bbox = 左上原点 y 向下；IL/PDF 坐标 = 左下原点 y 向上。换算点只有 `to_il_box`：`y' = H − y`（:64-75）。
- 原生字符 ↔ provider span 对齐（审计 + 行内公式保护）：① 字符中心在 span 内（容差 1.0pt）；② 字符面积落入 span ≥ 0.6；③ 回退 line bbox 同规则。多命中判 ambiguous，取覆盖率更高、面积更小者（:14-24, :43-45）。产物 `alignment.json` 记匹配率与 mismatch。
- 共同约定：**布局框与 provider IR 统一到「未旋转页面坐标（左上原点）」；进 IL 才翻 y 轴**（`pipeline.md:21-22`、`ARCHITECTURE.md:39`）。

### 1.7 解析产物清单

| 产物 | 内容 | 写入点 |
|---|---|---|
| `agent/document.md` | 整篇连续 Markdown（带 `<!-- id=… label=… -->` 段落标记 + `[[S1]]`/`[[F3]]` 锚点） | `markdown_view.extract_markdown`（:745） |
| `agent/anchors.json` | id → 源文/锚点/label 明细 | :745 |
| `agent/sheet.jsonl` | id/source（canonical 形式）清单 | :12-19 |
| `agent/state.pkl` | **pickle 的可恢复状态**：`doc`(整个 DocIL)、`inputs`(id→翻译输入)、`temp_pdf_path`、`pdf_path`、`lang_in/out`、`link_snapshot`、`page_char_objects`（链接覆盖的 PdfCharacter **对象引用**，靠 pickle 保身份）、`source_line_geometry`、`rows`、`mediabox_data` | `markdown_view.py:809-824`、`workflow.py:194-197` |
| `agent/source/links.json`、`source/bookmarks.json` | 源链接/书签快照（页、矩形、动作、目标、覆盖字符下标） | `link_snapshot.py:306,461,503` |
| `agent/source/mineru/provider_ir.json` | provider IR（unrotate 后） | `mineru_doclayout.py:303-341` |
| `<workdir>/<pdf名>/layout_coverage.json` | 覆盖率门禁审计（阈值 0.5%，纯空白不计分子分母，另记 `ignored_whitespace_chars`） | `pipeline.md:23` |
| （build 后）`agent/layout_geometry.json` | Typesetting 后 dump：每段 `src_box`/`layout_box`/`rendered_box`（pdf_native，y 向上） | `layout_geometry.py:23,150-236` |
| （build 后）`agent/target/provider/provider_ir.json` + `target_recognition.json` | **对译文 PDF 重新识别**的独立 IR（MinerU 原生坐标，不换算）+ 清单（status ok/skipped/failed、pdf_sha256） | `mineru_doclayout.py:749-767`、`pipeline.md:29-40` |

---

## 2. 翻译协议层

### 2.1 锚点 Markdown 协议（`babeldoc/tools/agent/markdown_view.py`）

| 概念 | 语法 | 规则 |
|---|---|---|
| 段落标记 | `<!-- id=P01-001 label=text -->`（`ID_MARK_RE`，:45） | id/label 原样保留；回填靠 id 定位、label 决定排版角色；模型改写 label 只记 warning（:988-1001） |
| 样式锚点 | `[[S1]]…[[/S1]]`（容忍 `[[ S 1 ]]`/大小写，`ANCHOR_RE` :40） | ↔ canonical `<style id='1'>…</style>`；开闭成对、id 不改不增删 |
| 公式锚点 | `[[F3]]` | ↔ canonical `{v3}`；位置原样，不翻译不移动 |
| 链接锚点 | LaTeX 侧 `bdoclink://l<N>`（`\href` 挂在角标公式上） | 渲染层概念，见 §3.6 |
| canonical 占位符全集 | `{vN}`、`<style id='N'>`、`</style>`、`<bN>`、`</bN>` | `protocol.py:21-27`，正则多重集比对 |

**写回校验（`apply_markdown`，`markdown_view.py:956-1080`）**：

1. 解析译文 Markdown → `{id: (body, echoed_label)}`；**额外 id（不在 inputs）直接拒绝**（:969, :1052）。
2. 漏段落 → 回退原文，记 `fallback_ids`（:979-982）；正文空 → 回退原文，记 `empty_ids`（:1002-1007）。恒通过校验、可观察。
3. 锚点**多重集**（Counter）比对：一致但顺序不同 → 只记 `anchor_reordered` warning（**顺序合法**，模型按中文语序重排锚点是正确翻译）；多重集不一致或有空 span → `repair_target` 确定性修复（:229-259）：
   - `accepted` 模式：多重集本就一致，只修 `[[S1]][[/S1]]X` → `[[S1]]X[[/S1]]` 空标签（:215-227）；
   - `proportional` 模式：按源文各文本段长度占比把源锚点序列等比投放到译文（保证协议合法，牺牲锚点精度）。
4. 修复后仍多重集不符 → violation，**阻止写回**（:1030-1034, :1052-1063）。
5. 通过 → 写 `agent/translated.jsonl`（canonical）→ `workflow.apply`（`workflow.py:191-274`）：`protocol.check_placeholders` 再验一遍占位符多重集 → `ILTranslator.post_translate_paragraph` 写回 IR（占位符标点规范化 `_normalize_placeholder_punctuation`）→ 重写 `state.pkl` + `il_translated.applied.json`。

`protocol.py` 另有批量 JSON 协议（`## Here is the input:` + `[{id,input,output}]`，:61-150），用于离线回放与 LLM-only 历史路径，**当前主管线不用**。

### 2.2 提示词与分工（`skills/document-translate/agents/`）

| 文件 | 角色 | 要点 |
|---|---|---|
| `translator.md` | 整篇翻译 | 锚点协议 5 条军规（样式/公式/段落标记/引用编号/圈号）；只译收到的正文块（作者/文献/页脚已在解析侧排除）；Markdown 结构一一对应；`{glossary}` 词表占位行整行替换 |
| `translator-repair.md` | 按 id 补译/重译（`translate --ids`） | 只含待修段落 + `{feedback}` 审查反馈 |
| `reviewer-protocol.md` | 结构/协议审查 | 输入是 `bdt check` verdict + layout_lint + link_audit 的**工具输出**；输出 verdict/findings（id/kind/sev/page/evidence/action） |
| `reviewer-fidelity.md` | 语义保真（含回译） | 译文段→回译英文→Python 侧算 Levenshtein 相似度 |
| `reviewer-layout.md` | 排版视觉审查 | render PNG + geometry；结论必须回落到文本层/几何数据 |
| `layout-fixer.md` | findings → `bdt layout-set --patch` | 用最少杠杆消除缺陷 |
| `main-agent.md` | 8 子命令编排器 | 阶段状态 + 命令注入 + 验收决策 |

分工模式：**确定性检查在代码（check/protocol/audit），LLM 只做代码判不了的（语义保真、视觉审查），修复动作由上层 Agent 执行后 `--from apply` 续跑**（`run.py:53-57`，每类修复最多 2 轮，超限 `needs_human_review`）。

### 2.3 模型提供方与调用方式（两条并行通道）

| 通道 | 机制 | 证据 |
|---|---|---|
| ① 外部 CLI harness | `--translator/--reviewer` 给命令；`common.run_translator` 用 **argv 列表**（不经 shell）起子进程，**提示词走 stdin、结果走 stdout**。内置 4 个 profile：`pi-deepseek-flash/pro`（调 `pi` CLI，NDJSON 事件流）、`agy-gemini-3.8-flash/3.1-pro`（调 `agy` CLI，stream-json stdin） | `harnesses.py:24-71`、`pipeline.md:50` |
| ② OpenAI 兼容 HTTP | serve 的 models 端点：用户存 base_url/model/api_key（`​.bdt-serve/model-credentials/models.json`，0600 权限，https 或 loopback）；`call_model` 直接 `POST {base_url}/chat/completions`（httpx，`trust_env=False`、拒绝重定向、API key 回显脱敏） | `serve/models.py:34-50, 62-103, 151-188` |

job 侧：客户端只传 profile id；serve 把 profile 解析成 `--translator <命令>` 进 argv，**命令与密钥不回传客户端、不落 job 记录**（`runner.py:129-187`，argv 只记 sha256 指纹 :189-191）。模型调用本身**不流式进 DB**——翻译块落库靠 CLI 侧的流式事件（`translation_stream.py` / `process_stream.py` 解 harness 输出增量）。

---

## 3. 排版/输出层

### 3.1 上游原生路径：仍在、仍可用、是回退基线

| 组件 | 状态 |
|---|---|
| `midend/typesetting.py`（1917 行） | 活跃：扩容优先排版（译文放不下先扩框再缩字；障碍=段落/非空白孤立字符/pdf_figure/page_layout 的 figure/table/formula；纯空白不算障碍，防止页眉空格把段落扩到页顶）。EXPAND_VERTICAL_GAP=2.0 等参数 :33-38。**仍是每遍 build 的第一步**（latex_bbox 在其输出之上贴片） |
| `backend/pdf_creater.py`（2214 行） | 活跃：内容流重发、字符跳过、mono/dual 输出、`latex_bbox_stats` 挂点 :642-644 |
| 双语 PDF | `create_side_by_side_dual_pdf`（拼宽左右，:1240-1337）与 `create_alternating_pages_dual_pdf`（交替页，:1339-1390）；链接搬运 `_copy_page_links_to_dual`（:1146-1238）+ TOC 复制 |

**latex_bbox 对它的替换程度**：默认开（`--no-latex-bbox` 关）；能力缺失/编译失败/融合失败/几何不安全**全部回退原生渲染**且计入结构化统计（`overlay.py:1-35`）。关闭时输出与旧渲染逐字节一致（`README.md:224`）。即：**原生路径 = 兜底 + 非“正文本体”段落的主路径**（title 等标签不进贴片）。

### 3.2 latex_bbox 目录做什么

| 文件 | 职责 |
|---|---|
| `capability.py` | 探测 xelatex/kpsewhich（缺宏包检测）与字体目录；缓存探测结果 |
| `renderer.py` | `BboxStampRenderer`：XeLaTeX 编译贴片。**有界阶梯**：源字号+源行距 → 行距 ×1.1/×0.9 → 字号 ×0.95^k（≤12 步、下限 4pt，`_MIN_FONT_SIZE` :87，阶梯构造 :918-933）。fit 判定水平容差 2.5pt / 垂直 0.5pt。**批编译快路** `build_ladder_tex`（:654）：首选档单编一次，未过则剩余候选压进**一次** xelatex（一档一页、`\vsize` 大常量、`@@S/@@E` 标记），选档结果与逐档顺序编译完全一致；前提不成立（编译失败/标记数或页数不符）退回顺序（`_try_ladder_batch` :738）。TEMPLATE_VERSION=`latex-bbox-2026-09-p6`（:80），进缓存键 |
| `renderer_batch.py` | `BatchStampRenderer`：多请求并发编译编排 |
| `stamp_cache.py` | 贴片编译缓存（内容寻址：模板版本+字体签名+请求内容）；serve 模式跨文档共享 `<store_base>/cache/stamps`（经 `BDT_LATEX_STAMP_CACHE` 传给 job 子进程） |
| `font_families.py` | 段落级中文字体族注册表：`source-han-serif`（思源宋）/`source-han-sans`（思源黑）/`lxgw-wenkai`（霞鹜文楷）/`klee-one`/`maru-buri`（:59-79）；拉丁按 serif 配 Noto Serif/Sans；缺字体族自动回落默认族，不报错 |
| `fusion.py` | Typesetting 前把 `paragraph.unicode` 转成安全 LaTeX body：prose 转义、`<style>`→`\textbf{}`/`\textit{}`、`{vN}` 按五级分类（**cornermark → text → mineru → simple_math → fragment**，:10-39）：MinerU 级要求精确盒同一性+文本一致+未复用三道闸门；fragment 裁源 PDF 片段嵌入（`\bdocfrag`），**绝不猜公式** |
| `source_geometry.py` | 源行几何采集（首行 ascent 等，撑起 `\topskip` 精修与擦除范围） |
| `overlay.py`（2012 行） | `LatexBboxOverlay`：生命周期 prepare（选段+编译贴片，内容流生成**前**）→ 内容流跳过已贴片段落字符（无双层文本靠构造保证）→ stamp（贴片落 bbox）。redaction 只在贴片矩形内仍有文本层时物理 `apply_redactions`（禁止白矩形假擦除）；redaction 删链接 → 快照重插 → `link_remap` 重定位 → 每页链接集合+URI 集合校验不过则**整体回滚**。模式 full/repair（repair=复现旧门禁只修溢出）。`latex_bbox_box_overrides`（编译后扩框）只换贴片矩形不动 Typesetting 输入框（:1-35） |

### 3.3 浮动/加宽/跨页迁移（`layout_refine.py` + `block_compile.py`）

局部编译时贴片被缩字/溢出 → 自动浮动，三级顺序：

1. **同栏下/上扩**（`plan_expansion` :123，向下优先、同页先给所有目标向下再向上，障碍含刚扩过的框）；build 全量路径的编译后扩框用同一模块 + pymupdf 墨迹兜底（`page_ink_rects` :349，实测区域检测漏过小标题）。
2. **跨栏横向扩**（`plan_widen_expansion` :226，widen-right/left）。
3. **跨页整框迁移**（`plan_next_page_float` :576）——**缺省关闭**（`BDT_NEXT_PAGE_FLOAT` 才开，:93-100），且须过 `next_page_float_eligible` 四道门禁（:606-637）：缩字 ≤0.75、落点在页面上半部、**原位不留空白**（`_home_stays_occupied` `block_compile.py:1040`：本段原位必须有别的段落框或已定贴片覆盖）、同页确无净空。理由：搬走正文段会在原位留空白，代价大于「字缩小了」。

并发账本：`FloatReservations`（`block_compile.py:122-174`）把已选中落点按页登记进数据库，后到的块当障碍避让——同页两块不会抢占同一净空。版面检测按页缓存（`PageLayoutCache` :78），ONNX Runtime 线程夹到 4。成功发 `compile_float` 事件（kind=expand/widen-right/widen-left/next-page），失败保留原贴片。

### 3.4 流式预览（两套并存！）

| 实现 | 机制 | 证据 |
|---|---|---|
| `babeldoc_tools/stream_preview.py`（CLI 侧 `StreamPreview`） | 隔离副本 workdir + 单编译器串行合并块 → `bdt apply`+`build` → 整本 PDF 落 `preview/` | :19-142 |
| `babeldoc_tools/serve/stream_preview.py`（`ServeStreamPreview`，**主用**） | 块完成即提交并行线程池（1..MAX_PREVIEW_WORKERS=min(16,核数-2)，`limits.py:23`）；**每页一把锁**（`PageLocks` :90）：xelatex 渲染不持锁、同页并行，只有浮动规划/占位与 patch 读-改-写提交持页锁串行；页全部块完成发布页资产事件；跨页浮动贴片落到已发布页 → 该页重合成一次；结束后 `compose_full_preview` 合成整本进 `local_previews` | :102-246 |

### 3.5 局部编译（`serve/block_compile.py::BlockCompiler`，:606）

- 资格门禁与全量同口径（`_ineligible_reason` :786）：`layout_label` ∈ 正文本体标签（title 不在内）且**源文行数 ≥2**（量源 PDF）；不合格 → `NotReplaced`（:323）→ 状态 `not_replaced` + 中性事件 `block_not_replaced`（reason=label-not-eligible/single-line）——**不是失败，原文就是正确结果**。
- 译文现查 `translation_blocks`（`(document_id,block_id)` 主键），不读段落行缓存快照（缓存里永远还没有正在编译的那块）。
- 实例级缓存：LaTeX 能力探测一次、state.pkl 按 workdir+mtime 缓存、ILTranslator 按语言对缓存、解析快照 5s TTL。
- 发布前检查 job 未取消且 revision 未过期（防旧结果覆盖新编辑）。

### 3.6 链接保真（四级定位 + 审计）

**定位**（`backend/link_remap.py:12-35`）——按优先级：

| 级 | 方法 | 机制 |
|---|---|---|
| 1 | `stamp` | LaTeX 印章内 `bdoclink://l<N>` 标记注记的**真实墨迹矩形**（角标公式在 fusion 阶段挂 `\href`） |
| 2 | `char_union` | 链接覆盖的源 `PdfCharacter` 对象仍存活在 composition（公式/passthrough/未译段）→ 当前 box 并集。**存活判定 = 进程内 `id()` 集合**（`build_alive_char_ids` :114；PdfCharacter 带 slots 不能打标记，pickle 保跨阶段身份） |
| 3 | `anchor` | 源链接覆盖文字（引文号等）在所属译文段落内做**带数字边界校验**的局部匹配（`3`≠`13`）；重复标签按源矩形段内相对位置就近分配 |
| 4 | `paragraph`/`proportional` | 段落 box / 比例投影，显式进 `fallbacks` 诊断 |
| — | `unresolved` | 保持原矩形不删链接 |

**写回**：直接 `xref_set_key` 更新现有注解 `/Rect`，不删不重建——任意 `/A`/`/Dest`（URI/GoTo/GoToR/Launch/命名目的地）、`/Border` 全保留；跨行拆成 N 条注记共享同一动作；清过期 QuadPoints；mono/dual 都复制内部目标（跨页目标按目标页尺寸映射）。`EXACT_METHODS={stamp,char_union,anchor}`（:58），其余进 fallbacks。

**审计**（`tools/agent/link_audit.py:168`）：逐条比较源/译文 PDF——数量与动作保留、内部目标页与坐标、引文/图/表/公式/脚注编号覆盖正确文字、外部 URI（只查地址不访问网站）；区分 `missing / wrong_label / wrong_role / source_invalid / external_unchecked`（`README.md:22-31`）。

### 3.7 字体来源

- LaTeX 贴片：XeLaTeX + fontspec/xeCJK；中文字体按 serif 选 Source Han Serif/Sans CN，注册表另 5 族；字体目录探测（含用户缓存目录），缺族回落；能力探测 kpsewhich 查宏包。
- 原生路径：`fontmap.py` FontMapper + 内嵌字体子集化。
- `GET /fonts` 端点 = 注册表 ∩ 能力探测（`routers/fonts.py:28-45`），LaTeX 不可用时全部 `available=false` 但不报错。

---

## 4. 服务层（`babeldoc_tools/serve/`）

### 4.1 路由清单（前缀 `/api/v1`，`app.py:294-307` 挂载）

| 方法 路径 | 用途 | 实现 |
|---|---|---|
| GET `/health` | 服务可用性与文档数 | `app.py:277` |
| GET/POST `/documents` | 文档列表 / 上传（multipart `file`，%PDF- 头、200MiB 上限、sha256 去重；上传不自动解析） | `routers/documents.py:80,105`、`uploads.py` |
| GET/DELETE `/documents/{did}` | 详情（含 quality/preview_asset/export_revision）/ 删除（workdir 目录树+DB 行，资产保留；活动 job 拒绝 409 `document_busy`；workdir 模式 400） | `documents.py:122,153` |
| GET `D/stage-state` | 七阶段状态 | `documents.py:186` |
| GET `D/paragraphs` | 段落清单（每段 style 摘要 font_size/bold/italic/serif/font_name + geometry） | `documents.py:202` |
| GET `D/geometry?kind=parse\|layout\|target` | 三档几何（`coord_system` 只标注不转换：parse/target=`pdf_topleft`，layout=`pdf_native`） | `documents.py:226`、`views.py:645-774`、`schemas.py:36-38` |
| GET `D/check` | 质量结论 | `documents.py:265` |
| GET `D/events`、`D/events/stream` | 诊断事件分页 / SSE（默认诊断流 + `?persistent=true` 持久流，共用路径**游标不混用**） | `routers/events.py:377,408` |
| POST `D/jobs`、GET `D/jobs` | 全量任务提交（action=run/check/compile；profile id、preview_workers）/ 列表 | `routers/jobs.py:130,354` |
| GET `/jobs/{jid}`、POST `/jobs/{jid}/cancel` | 任务状态 / 取消 | `jobs.py:317,331` |
| GET/PATCH/DELETE `D/draft` | 草稿读 / 写（base_revision 乐观并发，成功 revision+1）/ 清空（也 +1） | `routers/draft.py:62,78,108` |
| POST `D/blocks/{block_id}/compile` | 单段编译（带 base_revision） | `routers/jobs.py:291` |
| POST `D/blocks/compile` | 批量编译（1..200 块，一个 job，渲染并行/同页提交串行） | `jobs.py:213` |
| POST `D/export`、GET `D/exports/latest` | 导出当前 revision / 下载最近导出（默认要求成功且匹配当前 revision，`allow_previous=true` 放宽） | `jobs.py:213` 附近、`block_compile.py:1133` |
| POST `P/retranslate`、GET `P/candidates`、POST `P/candidates/{cid}/adopt\|reject` | 重译候选：隔离副本生成、采用才写草稿 | `routers/candidates.py` |
| GET `D/artifacts`、GET/HEAD `D/artifacts/{name}` | workdir 白名单产物下载（Range 支持） | `routers/artifacts.py:116,142` |
| GET `D/preview-pages` | 当前页资产清单（ETag/304） | `artifacts.py:48` |
| GET `D/assets/{digest}` | 内容寻址资产下载（归属校验） | `artifacts.py:82` |
| GET `D/versions`、`D/versions/{revision}/pdf` | 旧全量编译版本归档 | `routers/versions.py:46,73` |
| GET/PUT `/profiles` | 外部 CLI 提供方配置 | `routers/profiles.py:81,99` |
| GET/PUT `/models`、GET/DELETE `/models/{id}`、POST `/models/{id}/test` | OpenAI 兼容模型配置与连通测试 | `routers/models.py:21-43` |
| GET/PUT/DELETE `/glossary` | 词表（服务端复用 CSV 编解码） | `routers/glossary.py:51-85` |
| GET `/fonts` | 中文字体族清单 | `routers/fonts.py:60` |

### 4.2 SQLite 表结构（`database.py:19-40`，SCHEMA_VERSION=2，WAL，17 表）

| 表 | 关键列 |
|---|---|
| `papers` | id、title、authors（详情路由懒回填，只填空幂等 :96-111） |
| `documents` | id、paper_id、pdf_sha256(UNIQUE)、byte_size、status、revision |
| `drafts` | document_id PK、revision、payload(JSON) |
| `block_edits` | (document_id,block_id) PK、target、bbox、manual、revision（草稿写入时整删重插 :136-148） |
| `translation_blocks` | (document_id,block_id) PK、job_id、revision、target —— 流式译文落库点 |
| `blocks` | 段落快照（page、source、original_bbox、layout） |
| `compile_blocks` | (document_id,block_id)、input_hash、patch_asset、status（none/ok/preview_failed/**not_replaced**）、target_size |
| `pages` | (document_id,page)、input_hash、page_asset、dirty |
| `jobs` | id、document_id、action、status、from_stage、revision、error、progress |
| `job_events` | 自增 id、(job_id,seq) UNIQUE、block_id、page、type、data —— 持久 SSE 数据源 |
| `job_snapshots` | id、payload(JSON 全量快照) |
| `exports` | document_id、asset_sha256、revision、status |
| `assets` | sha256 PK、relative_path、kind —— 资产索引（文件在文件系统） |
| `parse_results`、`local_previews`、`local_pages` | 解析快照/完整预览/页 payload 引用 |
| `schema_migrations` | 版本 |

删除文档：单事务逐表删行（`DOCUMENT_TABLES` :312-325），资产文件与 `assets` 行保留（内容寻址可能共享，回收交 `--cleanup`）。`migrate.py:15-30` 定义从旧 workdir 登记哪些快照文件（state.pkl、anchors.json、provider_ir.json、layout_overrides.json 等）。

### 4.3 JobRunner 子进程协议（`runner.py`）

- argv **只由服务端构造**（`build_job_argv` :129-187）：`sys.executable -m babeldoc_tools run … --translator <profile 命令> --from … --pages --dual --glossaries`；profile id 解析后命令不回传客户端。
- `spawn_job`（:279-308）：`start_new_session=True`（自成进程组，取消时 `os.killpg` SIGTERM→5s→SIGKILL 连孙进程一网打尽）；**serve 把自己源码树根前置进子进程 PYTHONPATH**（父子同代码，import 不依赖 cwd）；stdin=DEVNULL、stdout/stderr 管道由 `PipeCapture` 读干留尾。
- 输出契约：stdout **最后一行 JSON 信封** `{"ok":…}`；无信封失败时 `error_message` 附脱敏 stderr 末三行（400 字符）。信封落盘前 `sanitize_envelope`（:197）替换密钥。
- 服务重启把残留任务收敛为 `interrupted`，不自动恢复。

### 4.4 SSE 事件类型

两种流（`routers/events.py:408`+）：默认诊断流读 `debug/runs/<run_id>/events.jsonl`（`id: <run_id>:<seq>`，15s ping）；`?persistent=true` 读 `job_events`（数字 `id` 游标，跨任务/重启可续）。默认流另有虚拟 `job_update`（纯通知不落盘，重连必须查 `/jobs/{jid}`；`events.py:85`）。

持久事件类型（`job_events.type`）：

| 类型 | 含义 | 写入点 |
|---|---|---|
| `translation_block_completed` | 一个译文块验证落库（原子：块+progress+事件一个事务） | `database.py:216-269` |
| `preview_ready` | 页资产发布 / 整本预览合成（page 字段区分） | `block_compile.py:1648,1825`、`serve/stream_preview.py` |
| `preview_failed` | 块预览失败 / 流式预览整体失败 | `block_compile.py`、`stream_preview.py:181,266` |
| `block_not_replaced` | 按设计不替换（label-not-eligible / single-line），中性 | `block_compile.py:716-719` |
| `compile_float` | 浮动成功（kind=expand/widen-right/widen-left/next-page，含落点页与框） | `block_compile.py:1437-1440` |
| `block_compiled` | 批量编译每块完成 | `block_compile.py:1718-1721` |
| `target_layout` | 全量编译后译文侧识别产物产出（layout_status/reason/provider/page_count/pdf） | `runner.py:1156-1157` |
| `job_queued/started/finished/failed/canceled/interrupted` | 任务生命周期（append-only） | `jobs.py:558-584`、前端 `usePersistentEvents.ts:5-6` |

### 4.5 草稿数据模型与 revision 语义（`draft.py:1-80`）

`{"revision": n, "updated_at", "paragraphs": {"P05-002": {"target"?, "layout"?}}}`；`layout` 合法键由 `layout_overrides.py:35-45` 唯一定义（数值 4 键 scale_cap/font_scale/line_skip/box_scale、box、强制换行 2 键、bold/italic/serif 三态布尔、font_family）。三条铁律：**revision 单调不回退**（删除也 +1）、**base_revision 乐观并发**（不符 → 409 `revision_conflict` 带 current_revision）、**校验复用 layout_overrides**（不复制魔法数字）。文件 `<workdir>/.bdt-serve/draft.json`（原子替换）与 SQLite 并存，**有 DB 记录优先读 DB**。保存**不自动编译**。局部编译/导出带 base_revision，发布前查任务状态与 revision 防过期覆盖。

### 4.6 资产存储（`asset_store.py:19-70`）

内容寻址：`<store_base>/assets/<sha256 前 2 位>/<sha256>.<扩展名>`；`put` 流式哈希、tmp+rename 原子写、DB 记索引；`resolve` 只经 DB 查 relative_path 且校验不越 root。数据库不存 PDF 二进制。

---

## 5. 前端（`web/src/`）

### 5.1 结构一览

| 层 | 文件 | 职责 |
|---|---|---|
| 入口 | `app/App.tsx`、`main.tsx` | 三栏骨架（左 PaperNav 常驻 / 中按路由 / 右 InspectorPanel 仅工作台）；react-query（retry:false，错误卡显式重试） |
| 路由 | `lib/routing.ts` | hash 路由：library/glossary/settings/workbench/{did}/{progress\|archive}；旧视图 id 兼容回落 |
| screens | `WorkbenchScreen`（预览区+右栏）、`GlossaryScreen`、`SettingsScreen`、`PlaceholderScreen` | |
| shell | `PaperNav`（论文卡/搜索/上传/列底 `DocumentActionBar` 任务控制+编译全文）、`InspectorPanel`（段落/事件流/归档三 tab）、`Gutter`（栏宽拖拽） | |
| preview | `PreviewArea`/`PreviewToolbar`（文档名+阶段徽标、翻页、缩放、bbox 四档）、`ContinuousPdfPane`/`PdfCanvas`/`TransitioningPdfPage`（连续滚动+页切换动画）、`BboxLayer`/`BboxLegend`、`CompileBar`、`ExportButton`/`DownloadButton` | |
| edit | `ParagraphEditor`（译文+字号/字体族/三态样式）、`BboxEditor`（layout 框拖拽）、`BatchPanel`（shift 多选批量编译）、`CandidatePanel`（重译候选） | |
| events | `EventStreamPanel`/`EventRow`、`useEventStream`（旧诊断流）、`usePersistentEvents`（持久流，sessionStorage 游标）、`useJobUpdates`、`useTimelineStages`（阶段条） | |
| jobs | `JobControls`（run/check/cancel）、`HarnessSelect`（模型档位） | |
| archive | `VersionList`、`ArchiveSummary`（旧全量版本） | |
| ui | Button/Chip/ErrorCard/ScrollArea/StatusBadge/Tooltip/icons | 无依赖原语 |
| lib | `api.ts`（fetch 封装+ApiError 统一错误码）、`queries.ts`（react-query keys）、`draft.ts`、`preview.ts`、`pdf.ts`、`events.ts`、`timeline.ts`、`models.ts`、`harnesses.ts`、`glossary.ts`、`uploads.ts`、`versions.ts`、`humanize.ts`、`cn.ts` | |
| stores | `stores/ui.ts`（zustand vanilla：屏路由镜像、栏宽、预览模式、bbox 档；localStorage `ieet.navw/inspw/screen/bboxMode`）、`stores/bbox.ts`（每文档 label 可见性 `ieet.bboxVisibility.<did>` + 描边样式） | **只有 2 个 zustand store**，其余状态在 react-query |

### 5.2 坐标换算（换算点唯一在前端）

- `lib/preview.ts:62` `coordSystemOfMode`：bbox 四档 ↔ 坐标系唯一映射（layout→`pdf_native`，parse/target→`pdf_topleft`）；`geometryBboxes`（:165）按 `coord_system` 选字段（recognition_entities/entities/paragraphs），**不做换算**；`pickPreviewArtifacts`（:67）mono 首选→dual→任意 pdf。
- `BboxLayer.tsx:77` `pdfToScreen`：cropbox 偏移 + `viewport.convertToViewportRectangle`；`:116` `screenToPdfBox` 逆变换（拖拽写回草稿 `layout.box`）。**服务端只标注 `coord_system` 不转换**（`views.py:13`、`ARCHITECTURE.md:109`）。
- 只有 layout 档可拖拽（`layerMode==='layout'` 准入）；parse 框点击选中段落；target 框只读（null=没识别、[]=识别了没框，语义严格区分）。

### 5.3 pdf.js 使用

`lib/pdf.ts`：`getDocument` + 本地 worker（`public/pdfjs/`，`scripts/sync-pdfjs-assets.mjs` 从 pdfjs-dist 同步，**不走 CDN**）；cmap/标准字体本地。产物 URL 直接交给 pdf.js 发 Range 请求（`preview.ts:81-88`）。

### 5.4 API client 生成

**手写 fetch 封装**（`lib/api.ts`：apiGet/apiPost/…，统一 `{"error":{code,message,detail}}` 信封解析成 ApiError）+ **openapi-typescript 只生成类型** `src/api/schema.d.ts`（`pnpm gen:api` 从运行中服务 `/openapi.json`，`package.json:16`）。不是完整 client 生成。

### 5.5 前端测试覆盖（`web/tests/`，38 文件）

| 主题 | 文件 |
|---|---|
| 坐标/换算 | `preview-coords.test.ts`、`edit-coords.test.ts`、`bbox-layer.test.tsx`、`bbox-editor.test.tsx`、`bbox-visibility-editing.test.tsx`、`bbox-categories.test.tsx` |
| 预览行为 | `preview-area.test.tsx`、`preview-toolbar.test.tsx`、`preview-queries.test.tsx`、`page-transition.test.tsx` |
| 事件流 | `events.test.ts`、`event-stream-panel.test.tsx`、`persistentEvents.test.tsx`、`job-updates.test.tsx`、`timeline-stages.test.ts` |
| 任务/编译 | `job-controls.test.tsx`、`job-queries.test.tsx`、`jobs.test.ts`、`compile-bar.test.tsx`、`batch-panel.test.tsx`、`harness-selection.test.tsx` |
| 草稿/编辑 | `draft.test.ts`、`paragraph-editor.test.tsx`、`candidate-panel.test.tsx` |
| 外壳/导航 | `app.test.tsx`、`workbench-shell.test.tsx`、`paper-nav.test.tsx`、`routing.test.ts`、`ui-store.test.ts`、`archive-tab.test.tsx` |
| 其他 | `api.test.ts`、`uploads.test.ts`、`glossary-lib.test.ts`、`glossary-screen.test.tsx`、`download-button.test.tsx`、`humanize.test.ts`、`cn.test.ts` |

### 5.6 Electron 迁移评估

**可基本原样迁移**（纯 HTTP 消费，无 FastAPI 编译期耦合；Electron renderer 里 fetch/EventSource/localStorage/sessionStorage/pdf.js 全可用）：

- 全部 UI 组件（screens/components/ui）、两个 zustand store、全部 lib 纯函数（preview/bbox/draft/timeline/humanize/cn/routing）、react-query 层、pdf.js 栈、38 个测试（vitest 可继续跑）。
- 唯一强耦合点是**传输层语义**而非实现：SSE 两种游标语义、`ApiError` 错误码分支、ETag/304 预览清单、`null vs []` 识别框语义、revision 409 处理——这些是对 FastAPI 行为的编码约定，Rust 后端必须逐条复刻，前端才能不动。
- `API_BASE='/api/v1'` 相对路径 + Vite 代理 → Electron 下改为本地服务绝对地址或自定义协议，一点改动。
- 无 CORS 依赖（同源部署），Electron 中天然同源。

---

## 6. 测试守卫（`tests/`，76 文件）

| 主题 | 文件 | 守卫的行为 |
|---|---|---|
| 翻译协议 | `test_agent_protocol.py`、`test_markdown_format.py`、`test_markdown_labels.py` | 占位符多重集、锚点转换、段落标记、label 回退 |
| 翻译范围/流 | `test_translation_selection.py`、`test_translation_stream.py`、`test_translation_perf_style.py` | 哪些 label 进翻译、流式块落库、性能样式 |
| 质量检查 | `test_quality_checks.py`、`test_glossary.py`、`test_harnesses.py` | 确定性 verdict、词表编解码、harness argv/响应解析 |
| 管线编排 | `test_run_pipeline.py`、`test_single_entry.py`、`test_tools_registry.py`、`test_tool_agent_contract.py` | 七阶段、STAGE_INPUTS 哈希、bdt 单入口、registry 信封 |
| 布局解析 | `test_layout_coverage_gate.py`、`test_toc_detector.py`、`test_char_advance.py` | 覆盖率门禁、目录条目化、字符 advance |
| MinerU/Paddle/对齐 | `test_mineru_doclayout_adapter.py`、`test_mineru_layout_helper_labels.py`、`test_mineru_skip_translate_categories.py`、`test_paddle_doclayout.py`、`test_paddle_layout_regions.py`、`test_paddle_provider.py`、`test_provider_alignment.py`、`test_provider_ir.py`、`test_provider_ocr.py`、`test_resource_controller.py` | 两后端适配、label 映射同步断言（KNOWN 集合↔映射函数）、字符对齐、IR 结构、降阈值重试 |
| LaTeX 贴片 | `test_latex_bbox.py`、`test_latex_renderer_batch.py`、`test_latex_tex_template.py`、`test_latex_source_geometry.py`、`test_latex_fusion_alignment.py`、`test_typesetting_expand.py`、`test_typesetting_title_centering.py`、`test_force_break.py` | 资格门禁、批阶梯=顺序选档、模板、源几何、公式融合闸门、原生排版扩框/标题居中/强制换行 |
| 链接 | `test_link_remap.py`、`test_link_audit.py`、`test_link_correspondence.py`、`test_link_role_boundaries.py`、`test_keep_link_annotations.py`、`test_cornermark_links.py` | 四级定位、审计分类、注记保留、角标链接 |
| 排版精修/覆盖 | `test_layout_refine.py`、`test_layout_overrides.py`、`test_layout_lint.py` | 浮动规划、覆盖校验、lint |
| serve（17 个） | `test_serve_{app,artifacts,block_compile,candidates,compile,database,documents,draft,events,fonts,glossary,job_updates,jobs,migrate,models,paper_meta,profiles,static,store,stream_preview,uploads,versions}.py` | 路由行为、并发页锁、revision 冲突、SSE 游标、资产授权、迁移幂等 |
| debug（8 个） | `test_debug_*.py` | 诊断归档/回放/查看器 |
| 译文侧识别 | `test_target_layout.py` | 三态语义、失败不阻断 build、旧 IR 清除 |

**可转 Rust 规约测试的纯逻辑**（无 IO、无 PDF）：`protocol.py` 全部（占位符提取/多重集/修复反馈）、`markdown_view` 的锚点转换/`repair_target`/`_fix_empty_spans`/`repair_text`、`layout_overrides.validate/merge_patch`、`quality_checks`（比例/密度统计）、`layout_refine` 的全部 `plan_*` 纯几何函数与四道门禁、`link_audit` 的 inventory/数字抽取、`run.py` 哈希依赖表、`provider_ir` 的 id 规则与跨页修复、`fusion` 的公式分类、前端 `preview.ts`/`BboxLayer` 坐标函数（Rust 侧需同构实现并共享同一组测试向量）。

---

## 7. 对重写的价值评估

### 7.1 必须在 Rust 版保留的行为/规约

| # | 规约 | 理由 | 证据 |
|---|---|---|---|
| 1 | **段落身份协议**：确定性 id `P01-001`、id/label 锚点、模型不得重编号；id 集合严格匹配（多/漏即拒） | 全链路的对账主键：翻译写回、事件、草稿、编译、审计全靠它 | `markdown_view.py:265-273,969,1052` |
| 2 | **占位符多重集协议**：`{vN}`/`<style>`/`<bN>` 丢失=违规、幻觉=违规；**顺序自由**（语序重排合法） | 公式与样式无损还原的唯一依据；顺序强制会逼模型回贴错误语序 | `protocol.py:21-92`、`markdown_view.py:1009-1018` |
| 3 | **确定性修复 + 可观察回退**：`repair_target` accepted/proportional 两模式；漏译/空译回退原文并记 fallback_ids/empty_ids；apply 成功≠无漏译 | 修复必须确定性可测；回退必须留证据 | `markdown_view.py:229-259,979-1007` |
| 4 | **续跑哈希**：STAGE_INPUTS 每阶段输入 sha256 存 run_state.json，`--from` 校验 stale_upstream 给 suggested_from；缺产物报 missing_artifact 不静默补跑 | 防止过期输入静默复用——正确性边界 | `run.py:61-90,771-842`、`pipeline.md:54` |
| 5 | **revision 语义**：单调 +1（删除也 +1）、base_revision 乐观并发 409、编译/导出发布前查 revision+任务状态 | 多标签页/流式编译不互相覆盖 | `draft.py:1-25`、`database.py:113-148`、`block_compile.py:651` |
| 6 | **坐标系约定**：三套坐标（provider=未旋转左上、IL/PDF=左下 y 向上、屏幕）；服务端只标注 `coord_system` 不转换；y'=H−y 换算点唯一；前端 `pdfToScreen` 是唯一换算点 | 现有前端可原样迁移的前提；历史上「译文框显示源文框」就是坐标系混用事故 | `provider_alignment.py:64-75`、`schemas.py:36-38`、`views.py:13`、`preview.ts:62`、`BboxLayer.tsx:77` |
| 7 | **链接四级定位 + 注记原地更新 + 审计**：stamp/char_union/anchor/paragraph 优先级、/Rect 原地改（动作/Border 全保）、URI 集合门禁不过即回滚、审计五分类 | 本 fork 相对上游的核心卖点；对象身份映射在 Rust 需换成显式 char id | `link_remap.py:12-58`、`overlay.py:1888-1922`、`link_audit.py:168` |
| 8 | **布局覆盖率门禁**：非空白原生字符被区域覆盖比例 ≤0.5%，纯白排除分子分母另行计数，超限停止解析 | 防静默漏译的第一道闸 | `pipeline.md:23`、`paddle_doclayout.py:95-127` |
| 9 | **not_replaced 语义**：非正文本体标签/源文单行 → 不替换是「落定」不是失败，中性事件 | 「原文就是正确结果」；否则流式预览永远等不完一页 | `block_compile.py:323,786-800,716` |
| 10 | **选档一致性**：批编译快路与逐档顺序必须选出同一字号/行距；fit 容差水平 2.5pt/垂直 0.5pt | 提速只许省进程不许改结果 | `renderer.py:654,738,918-933`、`ARCHITECTURE.md:105` |
| 11 | **浮动三级顺序 + 跨页四道门禁 + FloatReservations 并发账本**；同页渲染并行/页状态提交串行（页锁） | 并发正确性：不丢 patch、不抢净空、不在原位留空白 | `layout_refine.py:606-637`、`block_compile.py:122,1040`、`serve/stream_preview.py:90` |
| 12 | **事件语义**：持久 SSE 数字游标跨重启续传；`null`=没识别 vs `[]`=识别了没框；不编造进度/ETA；断线≠成功 | 前端断线恢复行为已按此编码 | `events.py:408`、`http-api.md:124-132,99-108` |
| 13 | **译文侧识别三态**：无清单 404 / skipped-failed 200+null+reason / ok 200+[]；绝不回退源侧 IR 冒充译文框 | 「译文框显示源文框」事故的制度化修复 | `pipeline.md:38`、`http-api.md:124-132` |
| 14 | **资产内容寻址 + 删除保留共享资产**；哈希去重 | 磁盘与一致性基础 | `asset_store.py:19-33`、`database.py:327-335` |
| 15 | **安全边界**：argv 只由服务端构造、密钥不进 job 命令不落盘（argv 只记 sha256 指纹）、信封脱敏、模型凭据 0600、httpx trust_env=False | Rust 版同样面对「job 参数可注入命令」风险面 | `runner.py:5-19,129-191`、`models.py:62-103,165` |
| 16 | **公式五级分类闸门**（cornermark→text→mineru→simple_math→fragment），一一对应失败走矢量路径**绝不猜** | 公式保真的根 | `fusion.py:10-39` |
| 17 | **能力探测降级**：LaTeX/字体/模型缺失 → 回退原生渲染 + `available=false` + 报告留痕，任务不失败 | 桌面环境不可假定装了 TeX | `overlay.py:33`、`routers/fonts.py:28-45` |
| 18 | 双语对照输出（拼宽 dual + 链接/TOC 搬运） | 产品需求 | `pdf_creater.py:1240-1390` |

### 7.2 可以丢弃的历史包袱

| # | 包袱 | 理由 |
|---|---|---|
| 1 | **双轨存储**：`.bdt-serve/` 兼容 JSON（draft.json、job 文件、全量编译状态、版本）与 SQLite 并存，「有 DB 记录优先读 DB」 | 重写时 SQLite（或 Rust 侧等价物）可做唯一真相源；迁移器一次性导入后退役。文档自己都标注「统一迁移和旧路径退役时间未定」（`ARCHITECTURE.md:116`） |
| 2 | **两套编译协议**：旧 `action=compile` 全量隔离重建 + 版本归档 vs `BlockCompiler` 局部补丁；`scope=pages` 仍降级全量 | 局部补丁路径已覆盖编辑闭环；全量重建保留为「重新 build」一种入口即可，版本归档可与 exports 合并 |
| 3 | **两套 SSE**：诊断 `events.jsonl` 归档流 + 持久 `job_events` 流，游标不混用是持续负担 | Rust 版只留持久流（run 归档作为离线导出） |
| 4 | **两套流式预览**：CLI `StreamPreview`（隔离副本整本重建、串行）与 serve `ServeStreamPreview`（并行池+页锁） | 只留后者语义 |
| 5 | **LaTeX 批阶梯快路实现**（一档一页 `build_ladder_tex`） | 是省 xelatex 进程启动开销的优化（进程启动 ~0.85s 占大头）。Rust 版若换排版引擎或直接管理进程池，此技巧可重新评估；**但「选档结果与顺序编译一致」的规约必须保留**（§7.1 #10） |
| 6 | **state.pkl pickle 格式**（含 pickle 的 PdfCharacter 对象引用、进程内 `id()` 存活判定） | Python 特有：对象身份靠 pickle/`id()`。Rust 需要显式 stable char id（如 `page:xref:idx`），序列化改 JSON/protobuf；这也顺带修掉「state.pkl 不能当跨版本永久格式」的已知债务（`online-translation.md:37`） |
| 7 | **XML/XSD IL 序列化**（xml_converter、il_version_1.rnc/.rng/.xsd） | 序列化格式与 IR 概念可分离；Rust 版保留概念模型（§1.3 表）即可，schema 文件不必移植 |
| 8 | **bdt 单入口 + 子进程信封协议**：serve spawn `python -m babeldoc_tools`、stdout 末行 JSON 信封、PYTHONPATH 前置、stderr 末三行兜底 | Electron/Rust 下引擎与服务同机同进程（或 sidecar），不需要「唯一 CLI 入口」约束和信封脱敏那套为子进程设计的协议。`tests/test_single_entry.py` 的守卫对象消失 |
| 9 | **批量 JSON 翻译协议兼容**（`protocol.py` 的 `## Here is the input:` 形态、ILTranslatorLLMOnly、sheet.jsonl 兼容清单） | 历史回放用；Markdown 锚点协议是主协议 |
| 10 | **旧 debug 归档/查看器体系**（`bdt debug`、debug_viewer、8 个 debug 测试、`debug/runs/` 归档） | 诊断价值真实但形态是 Python 生态专供；Rust 版用结构化日志/tracing 重做，不必复刻查看器 |
| 11 | **legacy_parse.py / 高层 high_level 的旧翻译入口** | 已被 agent 管线取代的入口 |
| 12 | **harness 外部 CLI 通道的 agy/pi 适配细节**（stream-json/NDJSON 解析） | 「stdin 提示词/stdout 结果 + 流式增量」的**边界**值得保留，但具体 CLI 适配是本地工具耦合；OpenAI 兼容 HTTP 通道（models.py）更通用，可作 Rust 版主通道 |
| 13 | **`--latex-bbox-mode repair`**（复现旧门禁） | 过渡期复现模式，新架构无旧可复现 |
| 14 | 每文档 `local_pages`/`local_previews` 旧表与 pages 表并存痕迹 | 属双轨存储的一部分 |

### 7.3 重写规模提示（供排期参考）

- Python 侧最重的三块：`new_parser`（PDF 内容流解释器，~40 文件）、`pdf_creater`+`typesetting`（内容流重发+排版，~4100 行）、`latex_bbox`（~6600 行）。Rust 生态有 `lopdf`/`pdf-rs`/`mupdf-rs` 可选，但「在源 PDF 上重发内容流 + 选择性替换字符 + 保注记」的语义需要重新设计，这是最大风险点。
- MinerU 客户端、provider IR、对齐、协议校验、布局规划、服务层（axum 对 FastAPI 逐路由映射）都是直接可移植的纯逻辑/IO，规约测试先行即可。
- 前端近乎整体复用（§5.6），成本集中在传输层语义复刻。

### 7.4 意外之处（值得重写时注意）

1. `document_il` 在 `babeldoc/format/pdf/document_il/`，不是 ARCHITECTURE.md 代码地图暗示的顶层路径。
2. 链接不在 DocIL schema 里，靠独立快照 + **pickle 对象身份 + 进程内 `id()` 集合**——这是最 Python 特有的设计，Rust 必须换成显式字符 id。
3. 模型调用有两条并行通道：外部 CLI 子进程（pi/agy，stdin/stdout）与 OpenAI 兼容 HTTP（仅 serve 配置）；没有「内置 SDK 直连某云」。
4. 前端 API client 是手写 fetch + openapi-typescript 只生成类型，不是生成的完整 client——迁移时 API_BASE 一改就能指向 Rust 服务。
5. 批量编译的并发模型很讲究：xelatex 渲染不持锁并行、页状态提交按页锁串行、浮动落点有数据库级账本、ONNX 线程夹 4——这些是实测调出来的（0.78s→0.43s/次、21×6s 长队），不是装饰。
6. fit 判定水平 2.5pt 容差是为「TeX/PyMuPDF 宽度口径是 advance 盒」的实测差异调的魔数，重写排版引擎时要重新标定。
7. 一页贴片梯队的全部候选与一个候选编译成本几乎相同（xelatex 启动+导言区占 ~0.85s，fontspec/xeCJK 不能 `\dump`）——若 Rust 版仍用 xelatex 子进程，这个约束依然成立。
