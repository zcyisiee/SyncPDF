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
| `GET D/events`、`D/events/stream` | 诊断分页与两种 SSE；`routers/events.py` |
| `POST D/jobs`、`GET D/jobs` | 全量任务提交/列表；`routers/jobs.py`、`runner.py` |
| `GET /jobs/{jid}`、`POST /jobs/{jid}/cancel` | 任务状态与取消；任务详情不在文档路径下面 |
| `GET/PATCH/DELETE D/draft` | 草稿与 revision；`draft.py` |
| `POST D/blocks/{block_id}/compile` | 显式编译单段；`block_compile.py` |
| `POST D/export`、`GET D/exports/latest` | 导出当前 revision、下载导出；`block_compile.py` |
| `POST P/retranslate`、`GET P/candidates` | 生成、查看重译候选；`candidates.py` |
| `POST P/candidates/{cid}/adopt`、`…/reject` | 采用写草稿，拒绝只改候选状态 |
| `GET D/artifacts`、`GET/HEAD D/artifacts/{name}` | workdir 白名单下载，`name` 是相对路径，支持 Range；`artifacts.py` |
| `GET D/assets/{digest}` | 经文档归属验证的本地资产下载 |
| `GET D/versions`、`D/versions/{revision}/pdf` | 旧全量编译版本归档；`versions.py` |
| `GET/PUT /profiles` | 服务端提供方配置；`profiles.py` |
| `GET/PUT /models`、`GET/DELETE /models/{model_id}`、`POST /models/{model_id}/test` | 已保存模型配置与连通测试；`models.py`；测试会实际调用提供方 |
| `GET/PUT/DELETE /glossary` | JSON 词表条目，服务端复用 CSV 编解码；`glossary.py` |

下载不要自行拼文件系统路径。普通产物白名单和哈希资产的授权规则不同，前端应使用服务端返回的清单/引用。完整响应字段不在这里复制，避免维护第二份 schema。

## 上传与任务

上传为 multipart 的 `file` 字段；服务端校验文件名、`%PDF-` 文件头和 200 MiB 大小上限，流式落盘，按 SHA-256 去重。重复 PDF 可返回已有 `did`。上传只保存并登记文件，不会自动创建解析结果或启动翻译。

`POST D/jobs` 当前接受 `run / check / compile`；即使 schema 的 action 枚举有 `retranslate`，这个通用端点也拒绝它，局部重译必须走段落候选端点。

```json
{"action":"run","from":"parse","profile":"已配置的提供方 id","use_glossary":true}
```

客户端传 profile id；执行命令在服务器解析，不能通过 job 请求传 translator/reviewer 命令或密钥。模型配置端点是独立的凭证输入边界，返回的是脱敏元信息。AI 审查是否运行由 reviewer 配置决定，不能根据翻译完成推断已审查。

提交成功为 202，响应 `status=queued` 是接收确认；真实状态读 `/jobs/{jid}`。状态包括 `queued / running / succeeded / failed / canceled / interrupted`。同一文档限制活动任务；全量 runner 与局部编译器各有本机并发控制，不能据此假定已支持多进程共享调度。服务重启会把残留任务收敛为中断，不自动恢复计算。

## 草稿、编译与导出

```json
{"base_revision":3,"paragraphs":{"P01-001":{"target":"修改后的译文"}}}
```

`PATCH D/draft` 成功递增 revision；`DELETE` 清空也递增。字段 `null` 可删除对应覆盖，整段 `null` 删除该段全部覆盖。`target` 使用 canonical 占位符（如 `<style id='1'>`、`{v3}`），不要直接传 Markdown 短锚点。`layout` 的合法键与数值范围由 `draft.py` 和 `babeldoc/tools/agent/layout_overrides.py` 校验。

当前保存草稿不自动编译。活动的非 block 任务期间拒绝草稿写入；block 编译允许新编辑，过期结果由 revision 检查挡住。保存后按目的选择：

| 操作 | 调用 | 成功产物 |
|---|---|---|
| 看一段的局部排版 | `POST D/blocks/{block_id}/compile`，带 `base_revision` | 局部页面/预览资产；不等于最终导出 |
| 下载当前修订 | `POST D/export`，带 `base_revision` | 数据库 `exports` 记录与导出资产 |
| 兼容全量重建 | `POST D/jobs`，`action=compile`、`base_revision` | 隔离重建后发布到 output，并进入旧版本归档 |

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
