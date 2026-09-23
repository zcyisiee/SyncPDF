# 管线与数据

本文描述当前代码，核对日期：2026-09-19。全局责任分布见根目录 `ARCHITECTURE.md`；使用命令见 [运行指南](../guide/cli.md)。

## 阶段契约

`babeldoc_tools/run.py::STAGES` 定义 `parse → translate → apply → build → check → review → report`。`review` 是编排内部阶段，通过 `run --reviewer` 配置，没有独立 `bdt review` 子命令。以下路径除特别注明外均相对 workdir。

| 阶段 | 主要输入 → 产物 | 实现入口 |
|---|---|---|
| parse | PDF、布局配置 → `agent/document.md`、`anchors.json`、`sheet.jsonl`、`state.pkl` | `parse.parse_document` → `markdown_view.extract_markdown` |
| translate | 原文、锚点、提示词、可选术语表 → `agent/prompt.md`、`translated.md` | `translate.translate_document` |
| apply | 译文 Markdown、解析状态 → `agent/translated.jsonl`、更新后的 `state.pkl`、`il_translated.applied.json`、`apply_report.json` | `translate.apply_translation` → `markdown_view.apply_markdown` → `workflow.apply` |
| build | IR、排版覆盖、源 PDF → `output/*.mono.pdf`、可选 `*.dual.pdf`，以及 `agent/layout_geometry.json`、`reconstruct_report.json`、译文侧识别产物（见下） | `layout.build_pdf` → `workflow.reconstruct` |
| check | 解析/写回/重建结果 → `agent/review_verdict.json`、`layout_lint.json`、`link_audit.json` | `review.check_document`（聚合结构、排版与链接检查） |
| review | 上述证据、reviewer 命令 → `agent/review_prompt.md`、`agent_review.json` 等审查记录 | `run._run_review_stage` |
| report | 检查与构建产物 → 默认 workdir 根 `FINAL_REPORT.md` | `report.report` |

解析路径为原生字符 IR → 布局后端 → 公式保护/段落组织/样式提取 → Markdown。布局可选 MinerU（云 API 或缓存回放）与 Paddle（本地运行时），对应 `babeldoc/docvision/`。源链接/书签、提供方结构和对齐证据位于 `agent/source/` 等解析产物中；定位具体格式应查看写入模块，不把历史目录名当通用协议。

MinerU 适配器在在线、缓存及回放路径中，将显示视图的 bbox 按 PDF 页旋转矩阵转换为未旋转 MediaBox 的左上坐标；布局区域与 provider IR 的 block/line/span 使用同一坐标系。原始布局 JSON 与缓存保持提供方坐标。Paddle 已输出未旋转坐标；共享 LayoutParser 只负责翻转 y 轴到 IL。

布局覆盖率门禁在段落组织前统计具有 bbox 的非空白原生字符；纯空白不进入分子、分母与未覆盖预览，数量另记为 `ignored_whitespace_chars`。未识别 Unicode 的非空白字符仍参与门禁。默认未覆盖比例上限为 0.5%，审计文件始终写入 `<workdir>/<输入文件名去扩展名>/layout_coverage.json`。超限停止解析；阈值以内的未覆盖字符仍会记录，门禁通过不等于覆盖了所有内容。

构建由 `document_il/midend/typesetting.py` 与 `backend/pdf_creater.py` 执行。首遍 Typesetting 在译文放不进源框时先扩框再缩字：障碍包括段落、有效的非空白孤立字符、既有 `pdf_figure`，以及 `page_layout` 中的 `figure`、`table`、`formula`/`isolate_formula` 语义区域；未归入段落的纯空白字符不作为障碍。这样可避免页眉或页顶空格把段落扩到页面顶部，同时保留上下间距、横向容差和页边界规则。默认启用 `backend/latex_bbox/`；可用时按段落渲染，不适用或失败时记录回退。`--render` 的页面图默认在 `output/render/`，实际文件名以返回 JSON 为准。PDF 已生成不表示质量检查通过。

首遍产物里确实有段落被 LaTeX 缩字时，`bdt build` 会再跑一遍「编译后扩框」：用本地 PP-DocLayoutV3（ONNX + CoreML，热跑约 0.1s/页）识别译文 PDF 的版面区域，再加 pymupdf 的精确墨迹兜底（实测区域检测漏过一个小标题），把这些段的贴片矩形扩到相邻墨迹之间再重排。同一页先给所有目标**向下**扩，向下没净空的再**向上**扩；障碍集里带着刚扩过的框，相邻两段不会抢同一段净空。向上扩依赖「首行几何按原框量测」：`\topskip` 取「首行字顶 − 框顶」，这个差跟着新框顶一起涨就换不来任何可用高度（实测与不扩逐字节一致）。整个精修只改贴片矩形，擦除范围与源行量测仍按原框，不写 `layout_overrides.json`，也不改 Typesetting 输入框，因此续跑哈希与既有排版覆盖语义不变；结果记在 `reconstruct_report.json` 的 `latex_refine`。没有缩字段、模型/依赖缺失，或传 `--no-latex-refine` 时保持单遍（`BDT_LATEX_REFINE=0` 同样关闭，供测试/排查用）。这与既有 `overlay._expand_vertical_failures` 不冲突：后者仍只在源版面找净空、且只在第一遍内生效。

### 译文侧版面识别（`build` 之后的附加产物）

`layout_geometry.json` 与源侧 `agent/source/provider/provider_ir.json` 都源自**源文档**，所以它们描述的是原文版面（同一段落的 `src_box` 与 `layout_box` 常常逐位相同）。mono PDF 产出后，`build_pdf` 会再对**译文 PDF** 跑一次 MinerU 识别，产出一份独立的译文侧 IR：

| 产物 | 内容 |
|---|---|
| `agent/target/provider/provider_ir.json` | 译文 PDF 的 block/line/span IR，**MinerU 原生坐标**（左上原点、y 向下），不做换算 |
| `agent/target_recognition.json` | 识别清单：`status`（`ok`/`skipped`/`failed`）、`reason`、`pdf`（workdir 相对路径）、`pdf_sha256`、`provider`、`page_count`、`created_at` |

开关是 `bdt build/run --target-layout` / `--no-target-layout`。默认在 `run_state.json` 记录的布局后端为 `mineru` 且 `MINERU_API_TOKEN` 可用时执行；后端是 `paddle`、token 缺失或显式关闭 → `status=skipped` + 原因，不发网络请求。识别失败（网络/超时/额度）写 `status=failed` + 原因，**不阻断 build**。识别没成功时上一轮的 IR 会被删掉，避免拿旧修订的框叠新 PDF。

实现复用源侧解析的同一个 MinerU 客户端（`MinerUDocLayoutModel.fetch_layout_json` / `recognize_pdf_provider_ir`）：内容哈希缓存（`~/.cache/babeldoc/mineru-layout.v1/`）在两条链路间共享，所以对同一个 PDF 重复识别不会重复计费。清单里的 `pdf_sha256` 就是被识别那份 PDF 的内容哈希。消费方是 `GET /documents/{did}/geometry?kind=target`（[HTTP API](http-api.md)），前端「译文框」用它；缺产物时前端明确提示「译文版面尚未识别」，不静默回退到原文几何。

## 文本协议

- 段落身份由本次解析分配，典型形式 `P01-001`（页号从 1 起）；模型和前端必须沿用原 id，不能自行重编号。
- Markdown 短锚点为 `[[S1]]…[[/S1]]`、`[[F3]]`；内部 canonical 文本使用 `<style id='1'>…</style>`、`{v3}`。转换归 `markdown_view.py`，前端草稿使用 canonical 文本。
- `apply_markdown` 检查锚点多重集，允许因译序调整改变顺序并记录警告；不是“顺序完全不能动”。确定性修复仍无法消除的违规或额外 id 会阻止写回。
- 缺失段落、空译文等情况可能回退原文并记录 `fallback_ids / empty_ids` 等证据；因此 `apply` 成功不等于没有漏译。交付必须再检查报告与成品。
- `translate --ids` 生成局部重译并合并回 `translated.md`；不会自动完成写回和重建。术语表由 `babeldoc_tools/glossary.py` 统一校验并进入翻译提示词。

外部翻译/审查命令由 `common.run_translator` 用参数列表启动（不经 shell 展开），提示词经 stdin，结果经 stdout。脚本 wrapper、内置 harness 与模型适配都遵循这一边界。提示词文件在 `skills/document-translate/agents/`；JSON 解包等提供方细节归适配层。

## 恢复与质量判断

`agent/run_state.json` 记录阶段结果、产物、质量结论及输入 SHA-256。`STAGE_INPUTS` 指明每阶段关心的输入；`--from` 校验被跳过阶段的记录，发现已变更输入时报错并提示重跑起点。缺必需产物报 `missing_artifact`，不静默补跑全部管线。它不是涵盖所有配置/依赖变化的通用构建缓存。

- `--prompt-only` 停在翻译准备状态。
- 无 reviewer 且未传 `--skip-ai-review`：`waiting_for_reviewer`、退出码 1，未完成交付。
- `--skip-ai-review` 显式跳过 AI 审查，不跳过本地 check。reviewer 的 `pass` 不能覆盖 check 的阻断项。
- reviewer 的 `needs_fix` 转为重译或排版修复动作；编排不自动执行无限修复，每类最多记录两轮，超限交人工处理。
- `bdt check --strict` 和 run 的 check 把无法确认的子检查也纳入失败判断；单独 `bdt check` 默认让调用者读取 verdict 后自行判断。

## 存储责任

CLI 以 workdir 文件为阶段交接；Web 在同一基础上增加 SQLite 与本地内容寻址资产。当前不是单一数据库覆盖一切。

| 数据 | 现有事实来源/用途 |
|---|---|
| 文档、草稿、段落修改、任务快照与持久事件 | `serve/database.py::MetadataDB`，`<store_base>/app.db`，SQLite WAL |
| 源 PDF、解析快照、页面/预览/导出资产 | `serve/asset_store.py::AssetStore`，`assets/<前两位 SHA-256>/<哈希>.<扩展名>`；数据库存索引 |
| CLI 可恢复状态与成品 | workdir 的 `agent/`、源/修复 PDF、`output/`；`state.pkl` 是程序生成的可信本地状态 |
| 兼容文件 | `.bdt-serve/` 的草稿 JSON、job 文件、全量编译状态及版本；SQLite 有草稿记录时优先读数据库 |
| 服务配置 | `.bdt-serve/` 下 profile、模型配置、术语表与独立凭证文件；不得放入公共下载或提交仓库 |
| 诊断归档 | `debug/runs/` 的事件、快照及产物副本；旧 SSE 使用它，不能与数据库事件混用游标 |

root 模式的 `store_base` 是服务根目录；workdir 模式则是工作目录本身。数据库中的 `papers` 表不等于已经有独立论文管理产品，Web 文档列表仍与 workdir 解析相连。

`serve --migrate` 将已有文档当前源 PDF、解析输入、草稿与可读成品登记为元数据/资产，保留旧 workdir，不导入全部历史日志。上传也会保留 `source.pdf` 与资产副本，所以哈希去重不代表磁盘已经只有一份。

`serve --cleanup` 只清服务根 `tmp/`、`cache/` 中超过默认 24 小时的普通文件；有活动任务会拒绝，且不遍历 `assets/`、不跟随符号链接。它不是完整生命周期 GC。容量问题还包括旧 workdir、隔离副本、debug、历史版本、资产与数据库 WAL；云端保留/回收策略见 [目标设计](../design/online-translation.md)。

## 局部编译边界

`serve/block_compile.py::BlockCompiler` 处理单段编译和导出：从当前草稿/翻译块与不可变解析输入生成页面补丁，记录资产、页面和 revision；发布前检查任务未取消且 revision 未过期。导出组合页面并记录 `exports`。流式译文通过 `ServeStreamPreview` 提交预览编译，与模型输出读取分离：完成的块立即进入并行编译池（worker 数 1..`MAX_PREVIEW_WORKERS`，上限 = `min(16, cpu_count-2)`，缺省取上限；来源优先级为 job 字段 `preview_workers` > `bdt serve --preview-workers` > 缺省）。池是一个共享线程池加每页一把锁（`PageLocks`）：贴片渲染（xelatex）不持锁、同页块并行；只有浮动规划/占位与 patch 读-改-写提交持该页的锁（读-改-写不丢更新）。跨页浮动贴片落到已发布页时该页重新合成一次。译文按 `(document_id, block_id)` 主键现查 `translation_blocks`（`_target`），**不**读段落行缓存的快照：翻译侧是「落库之后原地提交预览」，5s TTL 的快照里永远还没有正在编译的那一块。段落行缓存只负责几何。编译前先过与全量路径同口径的资格门禁（正文标签 + 源文行数 ≥ 2，源文行数量源 PDF），不合格的块抛 `NotReplaced`、记 `not_replaced`、保留基线原文，不算失败但算落定。批量编译 `compile_blocks` 同样只把页锁交给编译器，不再把整块编译包在锁里。`BlockCompiler` 实例缓存一次 LaTeX 能力探测（kpsewhich 子进程不再每段重复），`state.pkl` 反序列化按 workdir+mtime 进程内缓存，`ILTranslator` 按语言对进程内缓存且构造期间持锁（并发首编只建一份；`post_translate_paragraph` 只依赖占位符正则），解析快照查找（`hydrate_parse`）带 5s TTL 缓存，版面检测按页缓存（`PageLayoutCache`，ONNX Runtime 线程夹到 4），贴片编译缓存跨文档共享（`<store_base>/cache/stamps`，经 `BDT_LATEX_STAMP_CACHE` 传给 job 子进程）。

贴片 fit 不过时的有界阶梯（源字号+源行距 → 行距 ×1.1/×0.9 → 字号 ×0.95^k，≤12 步、下限 4pt）由 `BboxStampRenderer` 批编译：首选档单独编一次，未过则剩余候选压进**一次** xelatex（`build_ladder_tex` 一档一页），按同一优先级选档，选出的字号行距与逐档顺序编译完全一致；快路前提不成立（编译失败、标记/页数不符）退回顺序阶梯。挂诊断 recorder 时批阶梯仍按档各记一条 `candidate_evaluated`（共享整轮 TeX/PDF/日志，带 `batch_id`、页号与行号区间），`call_finished` 按真实进程数计。缩字/溢出贴片的浮动（同栏扩 → 跨栏扩 → 跨页迁移）把已落定的兄弟贴片（`FloatReservations`，按落点页登记）并入障碍集，同页并发浮动不会各自占用同一块净空。**跨页迁移这一级缺省关闭**（`layout_refine.next_page_float_enabled`，`BDT_NEXT_PAGE_FLOAT=1` 才开），开启后还须 `next_page_float_eligible` 的四道门禁全过（缩字 ≤ 0.75、落点在页面上半部、原位不留空白、同页确无净空）：搬走正文段会在原位留一块空白，代价大于「字被缩小」。

旧 `serve/compile.py::CompileService` 仍处理全量 `action=compile`：隔离副本 → 物化草稿 → `bdt run --from apply` → 校验产物 → 发布/版本归档。旧 PDF 在失败后仍可用，但必须显示旧 revision。`scope=pages` 在这条路径仍降级全量，不等于新段落编译接口。

保存草稿和采用重译候选都调用 `schedule`，但当前该函数不会启动编译。不要照抄残留的“1.5 秒防抖自动编译”注释。两条路径的接口与状态口径见 [HTTP 参考](http-api.md)。

## 开发中的 Rust 排版与删除边界

`engine/` 的 Markdown 闭合块交付后调用 Knuth–Plass 排版；正文默认两端对齐、末行左对齐，标题保留源对齐。逐run使用原始或用户指定字号、字体角色、颜色；容纳失败保留原文并报告 `typeset_overflow`，不自动缩字号。排版使用实际字形墨迹、cluster边界和固定源首行基线；同页其它源文字及图形限制扩框，错行双栏也受水平栏界限制。

内容流删除保留原 `TJ` 数值位移，按绑定的源code字节边界替换待删字形，推进含 `Tc` 和单字节空格的 `Tw`。`'` / `"` 的换行和间距状态继续生效。简单字体间接 `/Widths`、Type0 后代CID字体的 `/W` / `/DW` 会解析；Type3明确水平 `FontMatrix` 的宽度按其缩放解释。未知名义宽度（包括当前未支持的无 `/Widths` 简单字体）、不可信字节边界或非有限推进拒绝发布候选，不能用墨迹宽或1000代替。失败不修改原文档。

该Rust路径仍在开发中，公式原子、链接几何、dual与编辑重编译的完成情况以[唯一任务状态](../reports/2026-09-22-rust-electron-rewrite/task-state.md)为准；旧Python管线的字号阶梯行为见上文。

Rust的V3布局后端发出`layout_backend`事件，包含模型哈希及requested/configured provider。Auto发生CPU回退时另发`layout_backend_fallback`；显式`SYNCPDF_LAYOUT_DEVICE=coreml`失败直接上抛。CoreML只负责可兼容算子，其余算子可在CPU；provider名称不能单独证明GPU参与。布局区域缓存按模型/后端/分辨率/分数阈值分开，换权重或参数不得复用旧区域。
