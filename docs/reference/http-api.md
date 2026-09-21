# HTTP 与工作台

适用于当前 `bdt serve`，核对日期：2026-09-19。字段 schema 看运行服务的 `/openapi.json` 或 `/docs`；行为同时核对 `babeldoc_tools/serve/routers/` 与对应服务函数。旧 OpenAPI 描述仍有“自动防抖”等过时文字，不据此覆盖执行逻辑。

## 公共约定

- 前缀 `/api/v1`。成功直接返回资源 JSON；错误为 `{"error":{"code":"…","message":"…","detail":{}}}`，无详情时省略 `detail`，不含 CLI 的 `ok` 字段。
- 默认地址 `127.0.0.1`，端口默认 `0`（自动分配）；服务绑定成功后输出启动 JSON，日志到 stderr。当前没有内置用户认证，不注册 CORS；静态 `web/dist` 与 API 同源，开发时走 Vite 代理。
- root 模式的 `<root>/<did>/` 是文档 workdir。workdir 模式仅暴露指定文档，不允许上传。`did` 只接受非隐藏的单段目录名，拒绝路径和符号链接越界。
- 缺失产物导致计数为 `null` 时，前端显示未知，不能当作零。几何接口默认 PDF point、左上原点（y 向下）；草稿 `layout.box` 是 PDF 坐标、y 向上，转换由前端编辑逻辑明确处理。
- 常见冲突是 `409 revision_conflict`（带当前 revision）、`409 document_busy`；参数/草稿非法为 422；下载不存在或不属该文档为 404。错误码以 `app.py` 映射为准。

## 接口地图

下表路径省略 `/api/v1`；`D` 表示 `/documents/{did}`，`P` 表示 `D/paragraphs/{pid}`。

| 方法与路径 | 责任 / 实现 |
|---|---|
| `GET /health` | 服务根可用性与文档数；`app.py` |
| `GET /documents`、`POST /documents` | 列表、上传；`routers/documents.py`、`uploads.py` |
| `GET D`、`D/stage-state`、`D/paragraphs`、`D/geometry`、`D/check` | 元信息、七阶段状态、段落、几何、质量；`views.py` |
| `DELETE D` | 删除文档（**破坏性**）：workdir 目录树 + 数据库行；`document_busy`(409) / `delete_not_allowed`(400) |
| `GET D/events`、`D/events/stream` | 诊断分页与两种 SSE；`routers/events.py` |
| `POST D/jobs`、`GET D/jobs` | 全量任务提交/列表；`routers/jobs.py`、`runner.py` |
| `GET /jobs/{jid}`、`POST /jobs/{jid}/cancel` | 任务状态与取消；任务详情不在文档路径下面 |
| `GET/PATCH/DELETE D/draft` | 草稿与 revision；`draft.py` |
| `POST D/blocks/{block_id}/compile` | 显式编译单段；`block_compile.py` |
| `POST D/blocks/compile` | 批量编译多段（shift 多选，一个 job）；`block_compile.py` |
| `POST D/export`、`GET D/exports/latest` | 导出当前 revision、下载导出；`block_compile.py` |
| `POST P/retranslate`、`GET P/candidates` | 生成、查看重译候选；`candidates.py` |
| `POST P/candidates/{cid}/adopt`、`…/reject` | 采用写草稿，拒绝只改候选状态 |
| `GET D/artifacts`、`GET/HEAD D/artifacts/{name}` | workdir 白名单下载，`name` 是相对路径，支持 Range；`artifacts.py` |
| `GET D/preview-pages` | 当前页资产清单（页资产、完整标记、页版本）；ETag 随清单内容变化，304 表示无变化 | `routers/artifacts.py` |
| `GET D/assets/{digest}` | 经文档归属验证的本地资产下载；当前 `pages.page_asset`、导出和兼容 `local_previews` 均可授权 |
| `GET D/versions`、`D/versions/{revision}/pdf` | 旧全量编译版本归档；`versions.py` |
| `GET/PUT /profiles` | 服务端提供方配置；`profiles.py` |
| `GET/PUT /models`、`GET/DELETE /models/{model_id}`、`POST /models/{model_id}/test` | 已保存模型配置与连通测试；`models.py`；测试会实际调用提供方 |
| `GET/PUT/DELETE /glossary` | JSON 词表条目，服务端复用 CSV 编解码；`glossary.py` |
| `GET /fonts` | 段落级中文字体族清单（id/label/serif/available）；`routers/fonts.py` |

下载不要自行拼文件系统路径。普通产物白名单和哈希资产的授权规则不同，前端应使用服务端返回的清单/引用。完整响应字段不在这里复制，避免维护第二份 schema。

## 上传与任务

上传为 multipart 的 `file` 字段；服务端校验文件名、`%PDF-` 文件头和 200 MiB 大小上限，流式落盘，按 SHA-256 去重。重复 PDF 可返回已有 `did`。上传只保存并登记文件，不会自动创建解析结果或启动翻译。

`GET /documents` 与 `GET D` 的标题与作者字段来自本地抽取，不走网络也不调模型：`title` 优先取源 PDF metadata 的 `title`，其次取 provider IR 首页第一个 `type == "title"` 块的文本；`authors` 取 metadata 的 `author`，其次取紧随 title 块的第一个 `type == "text"` 块（形如 `Jack Brimberg <sup>a</sup>, Said Salhi <sup>b</sup>`，入库时剥掉 `<sup>` 单位上标）。两者**各自独立回退**，抽不到保持 `null`。`first_author` 是 `authors` 的第一个条目。结果落在 `papers.title` / `papers.authors` 两列（只填空的幂等写），读取时缺字段就懒回填，所以老文档不重跑 pipeline 也会显示真实标题；`title` 仍为 `null` 时前端卡片才退回源文件名。

`DELETE D` 删除文档（前端在文件库卡片上右键）：workdir 目录树与数据库行（草稿、页面、任务事件等）一起删；按内容寻址的资产文件**保留**（可能被其它文档共用），回收交给 `bdt serve --cleanup`。有活动 job 时拒绝（409 `document_busy`，子进程还在写这个 workdir）；`bdt serve --workdir` 模式拒绝（400 `delete_not_allowed`，那等于删服务自己的根）。删除顺序是"先数据库行、后目录树"，目录删除失败会留下可重试的孤儿目录，不会出现"数据库说没有、磁盘上还在"的不一致。

子进程失败而 stdout 没有收尾 JSON 信封时（通常是导入失败/参数错误/崩溃），job 的 `error_message` 会附上子进程 stderr 的末三行（已脱敏、截断到 400 字符）。没有这个片段时 `envelope_unparsed` 会把真实原因完全吞掉。

`POST D/jobs` 当前接受 `run / check / compile`；即使 schema 的 action 枚举有 `retranslate`，这个通用端点也拒绝它，局部重译必须走段落候选端点。

```json
{"action":"run","from":"parse","profile":"已配置的提供方 id","use_glossary":true,"preview_workers":16}
```

客户端传 profile id；执行命令在服务器解析，不能通过 job 请求传 translator/reviewer 命令或密钥。模型配置端点是独立的凭证输入边界，返回的是脱敏元信息。AI 审查是否运行由 reviewer 配置决定，不能根据翻译完成推断已审查。

`preview_workers`（可选，1..`MAX_PREVIEW_WORKERS`，缺省取上限）只对会跑翻译阶段的 `run` 生效：翻译进行中的流式预览编译并行度。上限 = `min(16, cpu_count-2)`，随服务所在机器核数走（`serve/limits.py`）；越界值 422。贴片渲染全部并行，同一页的块只在页状态提交时串行；优先级为 job 字段 > `bdt serve --preview-workers` > 缺省。其余 action 给了该字段也不进记录（与 compile 带 profile 同口径）。

提交成功为 202，响应 `status=queued` 是接收确认；真实状态读 `/jobs/{jid}`。状态包括 `queued / running / succeeded / failed / canceled / interrupted`。同一文档限制活动任务；全量 runner 与局部编译器各有本机并发控制，不能据此假定已支持多进程共享调度。服务重启会把残留任务收敛为中断，不自动恢复计算。

## 草稿、编译与导出

```json
{"base_revision":3,"paragraphs":{"P01-001":{"target":"修改后的译文"}}}
```

`PATCH D/draft` 成功递增 revision；`DELETE` 清空也递增。字段 `null` 可删除对应覆盖，整段 `null` 删除该段全部覆盖。`target` 使用 canonical 占位符（如 `<style id='1'>`、`{v3}`），不要直接传 Markdown 短锚点。`layout` 的合法键与数值范围由 `draft.py` 和 `babeldoc/tools/agent/layout_overrides.py` 校验：四个数值键（`scale_cap/font_scale/line_skip/box_scale`）、`box`、强制换行键、三个样式布尔键 `bold/italic/serif`（`true/false` 覆盖，缺省或删键 = 跟随源文派生值），以及中文字体族键 `font_family`（值是 `GET /fonts` 返回的 `id`；非法 id → 422 `draft_invalid`）。`font_scale`、三个样式键与 `font_family` 在局部块编译时真正生效；`font_family` 只影响局部块编译（拉丁字形跟随该族的 `serif`，除非同时显式给了 `serif`），全量编译路径不消费它。

`GET D/paragraphs` 的每个段落带 `style` 摘要（`font_size/bold/italic/serif/font_name`，由解析状态派生；解析状态不可用为 `null`），前端据此显示"当前 bbox 的编译样式"并提供三态覆盖下拉。

当前保存草稿不自动编译。活动的非 block 任务期间拒绝草稿写入；block 编译允许新编辑，过期结果由 revision 检查挡住。保存后按目的选择：

| 操作 | 调用 | 成功产物 |
|---|---|---|
| 看一段的局部排版 | `POST D/blocks/{block_id}/compile`，带 `base_revision` | 局部页面/预览资产；不等于最终导出 |
| 多段一起重排（shift 多选） | `POST D/blocks/compile`，`{"base_revision":n,"block_ids":[…]}`（去重保序、1..200 项） | 一个 job（`effective_scope=blocks`，`paragraph_ids` 回链）；各块草稿覆盖互不影响，渲染并行、同页提交串行，每个受影响页只合成一次 |
| 下载当前修订 | `POST D/export`，带 `base_revision` | 数据库 `exports` 记录与导出资产 |
| 兼容全量重建 | `POST D/jobs`，`action=compile`、`base_revision` | 隔离重建后发布到 output，并进入旧版本归档 |

不是每个块都会被 LaTeX 重排：`layout_label` 不属于正文本体标签（如 `title`）、或**源文**只有一行的块**按设计不替换**，保留基线原文。这类块记 `compile_blocks.status='not_replaced'` 并发中性事件 `block_not_replaced`（`{paragraph_id, reason}`，`reason` 为 `label-not-eligible` / `single-line`），**不是** `preview_failed`——原文就是它们的正确结果。它与 `ok`、`preview_failed` 同属「落定」状态，流式预览据此判定整页是否可以合成。判定口径与一次性全量编译（`LatexBboxOverlay._select_candidates`）一致；源文行数量的是**源 PDF**，不是贴片 baseline（baseline 在文档跑过一轮后已经是译文页）。

局部编译（单段与批量）在贴片被缩字/溢出时自动"浮动"：用 PP-DocLayoutV3 对编译后的译文页重识别版面，按 同栏向下/向上扩 → 跨栏横向扩 → 跨页整框迁移 的顺序找净空并重渲染；成功发 `compile_float` 事件（`kind=expand/widen-right/widen-left/next-page`，含落点页与框），失败保留原贴片。**第三级「跨页整框迁移」缺省关闭**（`BDT_NEXT_PAGE_FLOAT=1/true/yes/on` 才开）：把正文段整块搬到下一页会在原位留一块空白（界面上就是「这段没渲染出来」），阅读顺序也断在下一页页底，而它要解决的只是「字被缩小了」。即使开启还要四道资格门禁同时满足——同页确无净空（前两级失败即证明）、缩字严重（`scale ≤ 0.75`，≈ 缩放阶梯 5.6 步）、落点在页面上半部、**原位不留空白**（本段原位必须有别的段落框或别的已定贴片覆盖，只有本段自己的贴片不算）；任一条不合格就保持原位，宁可缩字也不留空白。批量编译每块完成发 `block_compiled` 事件。fit 判定对水平方向放宽到 2.5pt 容差（垂直仍 0.5pt），轻微超宽不再触发缩字号。

全量 compile 的 `scope=pages` 仍回退全量并记录原因。局部编译和导出不是它的同义参数。

`GET D/exports/latest` 默认要求最近记录成功且匹配当前草稿 revision，否则报 `export_not_ready`；可通过 `allow_previous=true` 明确请求旧记录对应文件。当前实现查询最近一条记录，不能承诺遍历所有失败记录寻找任意历史成功版。

详情的 `quality`、旧全量路径的 `compile`、新增 `preview_asset / export_revision` 各自描述不同状态（由文档详情路由填充）。全量编译失败时上一版 PDF 保留，并通过 `stale`/revision 表示过期。局部预览可用不代表导出就绪，也不代表完整质量门禁通过。

重译候选在隔离副本产生；采用才写草稿，拒绝不改正文。采用之后同样需要显式编译/导出。

## 进度与断线恢复

两种流共用 `D/events/stream` 路径，但不能混用游标。

| 模式 | 数据源 | 游标与恢复 |
|---|---|---|
| 默认诊断 SSE | `debug/runs/<run_id>/events.jsonl` | `id: <run_id>:<seq>`；`Last-Event-ID` 或 `run_id / after_seq`；无 run 时返回 `events_unavailable` |
| `?persistent=true` | SQLite `job_events` 已提交记录 | 数字 `id`；`Last-Event-ID` 或 `after_seq`；跨任务/服务重启继续读取该文档事件 |

默认模式还推送 `job_update` 通知，它不落盘且可能丢失，重连必须查询任务状态。`GET D/events` 仍是旧诊断分页：`next_after_seq` 是扫描位置，即使过滤后没有匹配事件也要推进；不是数据库持久事件分页。

新前端订阅实现在 `web/src/lib/usePersistentEvents.ts`，旧流消费在 `useEventStream` 等模块。显示真实阶段、段落事件与任务结果，不编造百分比或 ETA；断线不应被展示为任务成功。

## 识别框与 label 筛选

`GET D/geometry?kind=parse` 在 `recognition_entities` 中返回当前 `agent/source/provider/provider_ir.json`（兼容 `source/mineru/provider_ir.json`）的原始 block/span 框。递归包含容器子块、discarded 区域和全部 span kind，包括 `inline_equation`；不按翻译范围过滤，也不把未知 label 改成其他类别。非法或无面积的 bbox 跳过。页码为 1 基，坐标保留 PDF point、左上原点、y 向下。

每个识别实体含稳定 `id`、`kind=block|span`、原始 `label`、`page`、`box:{x0,y0,x1,y1}`、`parent_id`、`paragraph_id`。仅当 block 覆盖一个且仅一个段落至少 80% 面积时关联该段落；无唯一关联时只显示框。span 始终只读，不作为草稿段落或拖拽编辑目标。几何接口保留旧 `entities/relations`；`run_id` 仍只表示这两项的快照来源。

`labels:[{label,count}]` 汇总整份几何产物，`page` 只过滤几何，不过滤此清单。parse 优先统计 provider 框，`paragraph_labels` 单独统计旧段落快照；layout 统计排版段落。provider IR 缺失/损坏时 `recognition_entities=null`，回退旧快照；合法空 IR 返回 `[]`。仅有 IR、未开 debug 也能显示识别框。IR 和快照都不可用才返回 `snapshot_unavailable`。

工作台的类别区只列 label 复选框，选中显示、取消隐藏，选择按文档保存在浏览器并跨页保留。原文模式与对照模式的原文侧显示原始 block/span；译文侧继续使用段落/排版几何，避免把原文 span 坐标当成译文位置。可见 block 已包住的 text span 不重复描边；隐藏父框后，仍选中的 text span 可独立显示。公式等非 text span 始终独立显示。框采用类别固定颜色、圆角描边与浅色填充，未知 label 使用稳定散列颜色。旧 workdir 没有 provider IR 时无法凭空补出行内公式框。

当前仓库 `provider_ir.py` 声明 **27 个已知 block 类型、6 个已知 span 类型，去重后 28 个 label**。其中 span 包含 `text / inline_equation / interline_equation / image / table / chart`。这不是 MinerU 所有版本和后端的固定上限；例如新返回的未知 label 仍会进入清单，实际数量以本次响应的 `labels` 为准。
