# 云端版接口（`bdt cloud`）

适用于 `bdt cloud serve`（镜译 SyncTranslate 云端版），核对日期：2026-09-25。行为以 `babeldoc_tools/cloud/`（路由 `app.py`，业务 `jobs.py`，翻译 `runner.py`）和 `tests/cloud/` 为准；与本机工作台 `bdt serve` 的 [HTTP 参考](http-api.md) 是两套独立接口，互不共用数据库与文件。

## 公共约定

- 前缀 `/api`，只监听 loopback，由 nginx 反代；静态前端 `cloud-web/dist` 由 nginx 托管，不经过本进程。没有 OpenAPI 页面。
- 成功直接返回资源 JSON；错误为 `{"error":{"code":"…","message":"…"}}`，`message` 是可直接展示的中文。状态码映射在 `app.py::_STATUS`：未登录/邀请码错 401，登录限速与额度用完 429，找不到或不属于本人 404，状态冲突 409，文件过大 413，非 PDF 415，PDF 不可用 422。
- 登录：`POST /api/login {code}` 设 HttpOnly cookie `bdt_session`（`SameSite=Lax`，HTTP 部署不加 `Secure`），有效期 30 天；库里只存 token 的 sha256。同一 IP（取 nginx 写入的 `X-Real-IP`）10 分钟内失败 10 次后，在窗口结束前一律 429；限速状态在进程内存中，重启即清零。邀请码只能由服务器上的 `bdt cloud invite` 生成。
- 所有 job 接口先校验“本人且未删除”，否则一律 404，不区分“不存在”和“别人的”。

## 接口地图

| 方法与路径 | 责任 |
|---|---|
| `POST /api/login`、`POST /api/logout`、`GET /api/me` | 邀请码登录、退出；`me` 返回名字、打码邀请码、`daily_quota/used/remaining` |
| `GET /api/jobs` | 本人历史（含进行中），新到旧；每项带 `warnings` 数 |
| `POST /api/jobs` | multipart：`file` + `model` + `thinking`；返回 job 视图（201） |
| `GET /api/jobs/{id}` | job 视图：状态、页数与每页尺寸、`stats`（完成后）、`final_rev`、排队时的 `queue` |
| `POST /api/jobs/{id}/cancel` | 取消排队中/翻译中的 job；否则 409 `job_not_active` |
| `DELETE /api/jobs/{id}` | 软删除（只打 `deleted_at`）；进行中 409 `job_active` |
| `GET /api/jobs/{id}/events` | SSE：先回放、再推实时，见下文 |
| `GET /api/jobs/{id}/pages/{n}.webp?v=src\|tr&rev=` | 预览图 |
| `GET /api/jobs/{id}/download?kind=translated\|dual` | 下载“原名-中文.pdf”或“原名-中英对照.pdf”；未完成 409 `not_ready` |

## 上传、缓存与额度

- 校验顺序：大小 ≤ 50MB（`file_too_large`）→ `%PDF-` 魔数（`not_pdf`）→ pymupdf 可打开、未加密、≤ 60 页、有文字层（`pdf_unreadable`/`pdf_encrypted`/`too_many_pages`/`no_text_layer`）。模型目前只有 `deepseek/deepseek-flash`（`pi` 通道），思考强度 `low|medium|high`。
- 原文按 sha256 去重存入 `sources/`。**缓存键 = 原文 sha256 + 模型 + 思考强度 + 引擎版本**，引擎版本取 `syncpdf-cli` 二进制的 sha256（重新构建引擎即视为新版本）。
  - 键已有 `done/partial` 译文：直接建一条同状态、`cache_hit=true` 的 job，事件为“这篇论文已有译文，已直接加载 ⚡”，不扣额度。
  - 键正在排队/运行：新 job 挂到同一次翻译上，复制已有进度事件，不重复排队。
  - 键上次失败或被取消：同一键重新排队。
- 额度按北京时间自然日统计：非缓存命中、且状态不是 `canceled/failed` 的 job 数。排队和运行中的 job 已占额度，取消或失败即退回；挂靠到别人正在跑的翻译上也占额度。

## 任务状态与执行

- job 状态：`queued → running → done | partial | failed`，或随时 `canceled`。`partial` 表示有译文、但引擎报告未完整完成（有段落保留原文或内容未识别），前端显示黄色；`failed` 表示没有产出。
- 单个 runner 线程一次只跑一篇（按 `queued_at` 取最早的 translation），调用 `rust_backend` 驱动 `syncpdf-cli translate`（`--translator` 由 `bdt cloud serve --translator` 决定，服务器现用 `pi`：`--model <模型> --thinking <档>`；agy 通道则是 `--model <模型>-<档>`，agy 不接受 `--thinking`，强度并入模型名；其余参数 `--font-scale 0.9 --line-height 1.5 --layout-device cpu`）。运行 workdir 是 `work/<translation_id>/`，结束后把 `translated.pdf` 移入 `translations/<tid>/`、事件 gzip 保存，删除 workdir。
- 取消：排队中的直接结束；运行中的只有当这次翻译的**所有** job 都取消后才终止引擎进程组（SIGTERM，5 秒后 SIGKILL），然后下一篇开始。
- 重启恢复：启动时把 `running` 的 translation 放回队首、名下 job 回到 `queued`，追加事件“服务已重启，任务将重新开始”，并清掉残留 workdir；翻译从头重跑（`attempt` 加一）。停服（SIGTERM）时，uvicorn 最多等 SSE 长连接 3 秒；随后 lifespan 终止引擎，并把翻译留作 `running` 等重启重排，不记失败。因此 systemd 必须用 `KillMode=mixed`：只给主进程发 SIGTERM，主进程退出后剩余进程统一 SIGKILL。用 `control-group` 时引擎会先收到 SIGTERM，翻译被记为失败（见 [部署](../guide/cli.md#云端版)）。
- ETA：`queue = {ahead, eta_seconds}`，`ahead` 含正在跑的那篇，`eta_seconds = ahead × 最近 10 篇平均耗时`（没有记录时按 300 秒）。

## SSE 事件

`GET /api/jobs/{id}/events` 支持 `Last-Event-ID`（或 `?after=`）续传，15 秒心跳注释。除 `queue` 与 `end` 外每条事件都有递增 `id`，并带服务器时间 `ts`（秒）。只推面向用户的事件，不推段落事件。

| event | data | 含义 |
|---|---|---|
| `status` | `{status}` | job 状态变化 |
| `step` | `{step, fail?}` | 五个阶段：读取文件 / 识别版面 / 翻译 / 排版 / 生成文件；`step=5` 全部完成 |
| `milestone` | `{text}` | 醒目里程碑：已上传、已进入排队、开始处理、解析完成共 N 页、开始翻译、完成 50%、全部完成、可以下载、已取消、服务已重启 |
| `page` | `{page, rev, done, boxes, text?, first?}` | 某页译文已落盘；`rev` 用于取该页预览，`boxes` 是该页保留原文段落的框 |
| `warn` / `error` / `hit` | `{text, sub?, page?}` | 黄色提醒 / 红色失败 / 缓存命中 |
| `queue` | `{ahead, eta_seconds}` | 仍在排队时队列变化（无 id） |
| `end` | `{}` | job 已结束，服务端随后关闭连接 |

`boxes` 为页面比例 `[左, 上, 宽, 高]`：由引擎段落事件的 `source_bbox`（`pdf_user`）经原文页的变换矩阵（含旋转）换算到可见页面，四周留 3pt。完成后的最终框在 job 视图的 `stats.boxes` 中。

## 预览与下载

- 预览固定为宽 1600px、q80 的 WebP，渲染后缓存在 `preview/`，响应头为长期不可变缓存。`v=src` 用原文 sha 作键；`v=tr` 在翻译中需要 `rev=<attempt>.<revision>`（取自 `page` 事件），完成后用 job 视图的 `final_rev`（译文 sha 前缀）。
- 中英对照在首次下载时由 `syncpdf-cli dual --source --translated --output` 生成（A3 横向，左原文右译文，生成后自检），写入 `dual/<tid>.pdf` 短期缓存；同一翻译并发请求共用一把锁。原文与译文不被修改。

## 数据根与清理

`--root`（默认 `~/.bdt-cloud`）的布局见 `db.py` 文档字符串：`app.db`（SQLite WAL，只存元数据）、`sources/`、`translations/<tid>/`、`work/`、`preview/`、`dual/`、`tmp/`。启动时与每小时清理一次（`cleanup.py::LIMITS`）：引擎事件 7 天、对照版 24 小时或总量 2GB、预览总量 5GB（按 mtime 淘汰）、上传临时文件 1 天。原文与译文从不自动删除；无人引用超过 30 天的原文只计数。
