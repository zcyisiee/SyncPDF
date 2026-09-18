# Web 前端 HTTP 契约（`bdt serve`）

> 冻结日期：2026-09-17（分支 `feat/web-frontend`）。本文是前端与后端的**公共约定**
> 单一事实来源；范围与顺序以 `.plan/web-frontend/PLAN.md` / `EXECUTION.md` 为准。
> HTTP 形状的**最终**事实来源是运行中服务的 `/openapi.json`（= `docs/frontend/api.md`
> 的机器可读版本）；两者冲突时以 OpenAPI 为准，并回头修本文。
> `docs/frontend/README.md` 顶部已标记为历史协议，不再据其实现。

## 0. 快速开始（最小可运行）

```bash
# 1) 装 web extra（不装也能用其它 bdt 子命令）
uv pip install --python .venv/bin/python "fastapi>=0.115" "uvicorn[standard]>=0.32" "python-multipart>=0.0.9"
#    或：uv sync --extra web

# 2) 指向一个已存在的文档根目录（其下每个子目录 = 一个 workdir）
PATH="$PWD/.venv/bin:$PATH" bdt serve --root tmp
#    只服务一个 workdir：
PATH="$PWD/.venv/bin:$PATH" bdt serve --workdir tmp/ccs3764-dyn

# 3) stdout 打印真实端口（默认 --port 0 = 自动分配），然后 curl
curl -sS http://127.0.0.1:<port>/api/v1/health
```

`bdt serve` 是长驻进程：stdout 只在**绑定端口成功后**打印一次启动信封
`{"ok": true, "data": {url, api, docs, host, port, mode, root, version, pid}}`，
之后所有日志走 stderr。`--open` 用同一个已确认监听的真实端口打开浏览器。

## 1. 公共约定

| 约定 | 规则 |
|---|---|
| Base path | `/api/v1`；**没有** `/projects/{pid}` 前缀（存储不做 projects 层，§3 布局） |
| 成功响应 | 直接返回资源 JSON，**不**包 `{"ok": true}` |
| 错误响应 | `{"error": {"code": str, "message": str, "detail"?: object}}`；`detail` 缺省不出现 |
| 内容类型 | `application/json`（上传为 `multipart/form-data`，下载为 `application/pdf` 等） |
| 时间 | 一律 UTC ISO8601（毫秒精度），如 `2026-09-17T15:54:45.123Z`；不返回本地时间字符串 |
| 命名 | 所有 JSON 键用 `snake_case`（与 `agent/*.json` 产物一致，不做驼峰转换） |
| 认证 | v1 无认证；默认只监听 `127.0.0.1`，供同源页面（serve 托管静态资源）或 Vite dev proxy 访问 |
| CORS | **不注册** CORSMiddleware：任何来源都拿不到 `Access-Control-Allow-*`；不要靠 CORS 放开 |
| 只读边界 | 读文档产物只能经服务端 resolver（单段 `did`、符号链接/目录逃逸拒绝、不读根目录外的文件） |
| 假成功 | 质量门禁保留：`waiting_for_reviewer` / `needs_fix` **不是**成功；编译成功也不代表 pipeline 成功 |
| 进度 | 只用真实事件与阶段时间；不编造百分比/ETA |

### 1.1 文档与 did

- 布局：`<root>/<did>/` 一个目录 = 一个文档 workdir；`did` = 目录名（如 `ccs3764-dyn`）。
- `bdt serve --root <dir>`：枚举 `<dir>` 的直接子目录。
- `bdt serve --workdir <dir>`：**只**公开这一个文档；兄弟目录不枚举、不可解析、不出现在错误信息里。
- `did` 必须是单段目录名：拒绝空串、`.` / `..`、`/`、`\`、NUL、以 `.` 开头的隐藏名。
- `did` 解析结果必须落在根目录内（先 `Path.resolve()` 再判包含）；符号链接越界一律拒绝。

### 1.2 错误码

| HTTP | `code` | 语义 |
|---|---|---|
| 400 | `invalid_document_id` | `did` 不是合法的单段目录名 |
| 400 | `path_escape` | `did` 解析后越出服务根目录（含符号链接越界） |
| 404 | `document_not_found` | 文档不存在，或不在 `--workdir` 的可见范围内 |
| 404 | `not_found` | 未知端点 |
| 404 | `snapshot_unavailable` | `geometry?kind=parse` 缺 parse 段落快照（W02） |
| 404 | `geometry_unavailable` | `geometry?kind=layout` 缺 `layout_geometry.json`（W02） |
| 404 | `paragraphs_unavailable` | `paragraphs` 四份段落产物都不存在（W02） |
| 404 | `events_unavailable` | 没有任何 run 归档，或指定的 `run_id` 不存在（W03） |
| 404 | `artifact_not_found` | 产物不在白名单内或不存在（不泄露存在性，W03） |
| 404 | `version_not_found` | 版本不在归档清单里 / 版本号不是十进制数字 / 文件缺失或越界（同一个码，W12） |
| 405 | `method_not_allowed` | 方法不允许（带 `Allow` 头） |
| 409 | `revision_conflict` | 草稿乐观并发失败（或 `PATCH /draft` / `action=compile` 的 `base_revision` 不符），`detail.current_revision` 给最新值（W09） |
| 409 | `document_busy` | 同文档已有活动 job（W07）；任务期间草稿写端点也返回它（W09） |
| 413 | `file_too_large` | 上传超过 200MB 上限，`detail.limit_bytes`/`size_bytes`（W08） |
| 422 | `validation_error` | 请求体/参数校验失败，`detail.errors` |
| 422 | `draft_invalid` | 草稿字段/范围不合法：`detail.errors` 逐条给 `paragraphs.<id>.<字段>`（W09） |
| 422 | `forbidden_field` | 客户端自带了命令/密钥字段（`translator`/`reviewer`/`api_key`…）或脚本引用形状不合（W07/W08）。**优先于** `validation_error`：body 里有这类字段就先报它 |
| 422 | `invalid_pdf` | 上传缺文件名或前 5 字节不是 `%PDF-`（W08） |
| 422 | `script_path_forbidden` | `PUT /profiles` 的脚本引用不在白名单目录内/不存在（W08） |
| 500 | `internal_error` / `invalid_root` | 服务内部错误；启动时根目录非法 |
| 503 | `root_missing` | 运行期根目录不可用（health 也不谎报 `ok`） |

CLI 层（非 HTTP，仍是 `bdt` 的单行 JSON 信封）：`web_extra_missing`（缺
fastapi/uvicorn，message 里给安装命令）、`invalid_port`、`port_unavailable`。

### 1.3 分页 / 游标

- 事件分页：`?after_seq=<int>&limit=<int>`（默认 500，上限 2000；另有 `stage`/`kind` 过滤与 `run_id` 选择），响应 `{"run_id": <str>, "events": [...], "next_after_seq": <int>, "has_more": <bool>}`。`run_id` 是本页事件实际来源的 run（前端组 SSE `Last-Event-ID` 需要；W03 additive 扩展）。
- **即使本页没有匹配事件也要推进 `next_after_seq`**（游标是扫描位置，不是匹配位置），
  否则过滤条件会卡住轮询。
- `events.jsonl` 的 `seq` **只在单个 run 内**单调递增：跨 run 必须用 `run_id` 作为身份，
  游标语义是 `(run_id, seq)`。文件尾部未以换行结尾的半行不发布；损坏行跳过。

### 1.4 SSE（事件流）

- `GET .../events/stream`：`Content-Type: text/event-stream`，每条形如
  `event: <kind>`、`id: <run_id>:<seq>`、`data: {seq, at, stage, kind, data}`。
- 断线续传用 `Last-Event-ID`（`<run_id>:<seq>`）或 `?after_seq=`；run 切换时必须换 `run_id`。
- job 生命周期事件（`job_queued/started/finished/canceled/failed`）必须是服务端**持久化**的，
  不能由每个 SSE 连接临时生成。

#### 1.4.1 虚拟 kind `job_update`（W14，同一条流）

- 形状：`event: job_update`、`id: <job_id>:<第 n 次状态变化>`、
  `data: {"kind":"job_update","data":{"job_id","action","status","from_stage","error_code"}}`
  （恰好这 5 个字段；命令/信封/pid/路径**不进** SSE）。`status` 取值与 §3.4 一致，
  `action` 是 `run|retranslate|compile|check`；`from_stage` 可能为 `null`。
- **纯通知层，不落盘**：真相仍是 §3.4 的 `jobs.jsonl`（生命周期事件）+ `jobs/<jid>.json`
  （快照）—— 服务端先把状态写完盘，才在**同一条 SSE 流**里推一帧；
  因此“job 生命周期事件必须持久化”的红线仍然成立（持久化的是 jobs.jsonl，
  `job_update` 只是那次已落盘变化的一帧通知）。重启/断线不重放：没有历史补发，
  客户端重连后要自己 `GET /documents/{did}/jobs` 校准（前端就是这么做的）。
- **按文档过滤**：一条 SSE 流只收该 `did` 的 job 状态变化。
- **没有订阅者就丢弃**：不积压、不需要后台任务；重连拿到的是之后的变化。每个连接一个
  有界收件箱（64 帧），来不及取就丢最旧（通知层宁丢不积压；丢的那次状态在 jobs.jsonl 里）。
- **不污染续传游标**：`id` 的 `<job_id>` 不是 run_id 形状（`<UTC时间戳>Z-<6位十六进制>`），
  所以浏览器自动重连带上 `Last-Event-ID: j_...` 时会被当作“没有游标”忽略，
  run 侧按 `?after_seq=` 重来，不会把某个 run 的游标顶走。
- **只在有 run 归档时有这条流**：没有任何 run 归档时该端点仍是 `404 events_unavailable`
  （§1.3 的口径不变）。所以 job 从提交到“子进程建出 run 归档”之间没有 SSE 可言 ——
  那段过渡期前端用兜底轮询（§4.6）。

### 1.5 归档下载

- `GET /documents/{did}/artifacts`：产物清单（`name`、`path`、`size`、`mtime`、`kind`；**清单不加** `revision` 字段）。
- 下载链接的编译修订由**详情**给出：`compile.artifact.revision`（§3.2）+ `compile.stale`；
  同一个 revision 覆盖该次编译发布的全部 `output/*.pdf`。旧 PDF 不得被前端标成最新。
- `GET /documents/{did}/artifacts/{name}`：只允许清单内的白名单名字，支持 Range
  （`Accept-Ranges: bytes`，`206` + `Content-Range`），pdf.js 依赖这一点。
- **下载链接必须关联 artifact 的编译 revision**；旧 PDF 不得被前端标成最新（见 §3.2）。

## 2. 已实现端点（W01）

### `GET /api/v1/health`

```json
{
  "status": "ok",
  "api": "/api/v1",
  "version": "0.1.0",
  "mode": "root",
  "root": "/abs/path/to/root",
  "documents": 2
}
```

`mode` ∈ `root` | `workdir`；`workdir` 模式下 `documents` 恒为 1，`root` 仍是实际枚举的目录
（`--workdir` 的父目录）。根目录运行期不可用 → `503` + `root_missing`。

### `GET /openapi.json` / `GET /docs`

FastAPI 自带（OpenAPI 3.1）。`openapi-typescript` 从这里生成 `web/src/api/schema.d.ts`。

### `GET /` 及其下的前端静态资源（W15）

**不是接口，但同一个源**：`web/dist`（`pnpm build` 的产物）存在时，`bdt serve` 在同一个
端口上伺服 SPA —— `GET /` → `index.html`（`Cache-Control: no-cache`）、带内容哈希的
`/assets/*` → `public, max-age=31536000, immutable`、其它固定名资源（`pdfjs/*`）→ `no-cache`、
其余路径（前端 hash 路由的深链）**回退**到 `index.html`。`web/dist` 不存在就静默跳过
（只伺服 `/api/v1`，启动信封不变）；挂载点在所有接口路由**之后**注册，所以 `/api`、`/docs`、
`/redoc`、`/openapi.json` 永不被回退接住（未知的 `/api/v1/...` 仍是 JSON 错误信封的 404）。
前端拿到的相对地址（`/api/v1`、`/artifacts/...`）因此天然同源，不需要 CORS，也不需要
Vite 代理（开发模式仍可用代理，见 `web/README.md`）。

W01 **只**有这两个端点；没有写端点、没有假 stub。校验（越界/符号链接/`did` 规则）已在
服务端 resolver 实现并被测试覆盖；W02 起这些规则经 §3.1 的六个只读文档端点暴露。

## 3. 契约词汇（字段冻结；端点在后续任务实现）

### 3.1 只读文档端点（前六个：W02；后三行：W03）

| 方法 | 路径 | 说明 | 状态 |
|---|---|---|---|
| GET | `/api/v1/documents` | 文档列表（`did`、页数/段数、阶段状态、最近活动时间） | 已实现（W02） |
| GET | `/api/v1/documents/{did}` | 元信息：`pdf`、`config`、页数/段数、质量与编译状态 | 已实现（W02） |
| GET | `/api/v1/documents/{did}/stage-state` | 7 阶段状态 + **真实** `started_at`/`finished_at`/耗时 | 已实现（W02） |
| GET | `/api/v1/documents/{did}/paragraphs` | 段落面板数据（原文/译文/排版参数 join） | 已实现（W02） |
| GET | `/api/v1/documents/{did}/geometry` | `?kind=parse\|layout&page=N`，bbox（`pdf_topleft`） | 已实现（W02） |
| GET | `/api/v1/documents/{did}/check` | 结构审查/排版 lint/链接审计三组组装 | 已实现（W02） |
| GET | `/api/v1/documents/{did}/events` | 分页事件（§1.3） | 已实现（W03） |
| GET | `/api/v1/documents/{did}/events/stream` | SSE（§1.4） | 已实现（W03） |
| GET | `/api/v1/documents/{did}/artifacts[/{name}]` | 清单 + 白名单 Range 下载 | 已实现（W03） |
| GET | `/api/v1/documents/{did}/versions` | 版本归档清单（新 → 旧）+ 当前编译上下文 | 已实现（W12） |
| GET | `/api/v1/documents/{did}/versions/{r}/pdf` | 下载任一历史版本（`inline`，`<名>.r<r>.pdf`） | 已实现（W12） |

阶段名固定为 `parse` → `translate` → `apply` → `build` → `check` → `review` → `report`
（`babeldoc_tools/run.py::STAGES`）。

几何约定：`debug_recorder` 的坐标一律 **PDF point、左上原点、y 向下**
（`pdf_topleft`），前端**不要**再翻转 y；原始 IL 坐标（y 向上）由后端在
`geometry` 端点里标注来源并转换。

### 3.2 质量状态与编译状态必须分离

```json
{
  "quality": {
    "check": {"verdict": "pass", "blockers": [], "warnings": [], "at": "..."},
    "reviewer": {"status": "pass", "fix_rounds": {"retranslate": 0, "layout": 0}, "at": "..."},
    "pipeline_ok": true
  },
  "compile": {
    "status": "ok",
    "revision": 7,
    "stale": false,
    "artifact": {"name": "paper.mono.pdf", "revision": 7, "size": 123456}
  }
}
```

- `quality.check.verdict` ∈ `pass` | `needs_fix`（子项不可用时该子项为 `not_available`）。
- `quality.reviewer.status` ∈ `not_run` | `waiting_for_reviewer` | `pass` | `needs_fix` | `needs_human_review`。
- `quality.pipeline_ok`：只有质量门禁全绿才为 `true`；**编译成功不得置 true**。
- `compile.status` ∈ `none` | `running` | `ok` | `failed`；`compile.revision` 与
  `compile.artifact` **恒指最近一次成功发布的产物**（`artifact = {name, size, revision}`），
  `status`/失败原因描述最近一次尝试。真源是 `<did>/.bdt-serve/compile.json`（W09）：
  从没编译过 → `none`/`0`/`null`；编译失败时旧 PDF 仍在、仍可下载，只是被标成
  `failed` + `stale`。
- `compile.stale = (draft.revision > compile.revision)`。
- 下载/展示必须带 `artifact.revision` 与 `stale`：失败或取消不得破坏上一份可下载 PDF，
  旧 PDF 不标成最新（`stale: true` 时前端显式提示）。
- `artifact.name` 是**裸文件名**；下载键是 workdir 相对路径 —— 用
  `output/<artifact.name>` 在 `GET /artifacts` 清单里对接（清单里 `name == path`）。
- 同一个 revision 只覆盖该次编译**发布的那批文件**（`compile.json.files`，当前=不 `--dual`
  的 mono）。`output/` 里可能有更早 `bdt run --dual` 留下的 dual PDF，它**不属于**这个
  revision（如果 W10 要 dual 预览，得让 compile 也带 `--dual`，不在 W09 范围）。
- **历史版本**（W12）不在这里：每次成功发布自动归档一份到 `<did>/.bdt-serve/versions/`，
  由 §3.7 的两条端点给出。`output/` 的最新发布仍是下载键主路径（W10 的下载按钮不改），
  §3.7 只加「任一历史版本」。

### 3.3 草稿与 revision（已实现 W09）

`<did>/.bdt-serve/draft.json`（服务端私有状态，与 job 状态同级；**不进** `agent/` 产物区，
也不在产物下载白名单里）：

```json
{
  "revision": 3,
  "updated_at": "2026-09-17T15:54:45.123Z",
  "paragraphs": {
    "P05-002": {
      "target": "改后的译文",
      "layout": {
        "scale_cap": 0.9,
        "font_scale": 1.05,
        "line_skip": 1.4,
        "box_scale": 1.02,
        "box": [72.0, 640.0, 520.0, 780.0]
      },
      "updated_at": "2026-09-17T15:54:45.123Z"
    }
  }
}
```

- `target`：该段译文（覆盖 `agent/translated.jsonl` 的同 id 值）。**表示形式与
  `GET /paragraphs` 的 `target` 一致**：canonical IR 占位符（`<style id='1'>` / `</style>` /
  `{v3}`，即 IR 的权威文本形式）。写回 `agent/translated.md` 时由服务端转成等价的
  短锚点形式（`[[S1]]` / `[[/S1]]` / `[[F3]]`）后再合并 —— 前端**不要**自己做这个
  转换，也不要传 `[[S1]]` 形式（`translated.md` 的表示属于后端细节）。
- `layout`：段落排版覆盖，键名与取值范围必须与
  `babeldoc/tools/agent/layout_overrides.py` 一致 —— `scale_cap` 0.1–5.0、
  `font_scale` 0.2–5.0、`line_skip` 0.8–3.0、`box_scale` 0.3–5.0；
  `box` 是 `[x, y, x2, y2]`，**PDF 坐标 y 向上**（与几何端点的 `pdf_topleft` 相反，
  前端拖拽时必须换算，禁止直接透传屏幕坐标）。
  服务端用同一份 `layout_overrides.validate` 校验（含 `box` 的 `x2 > x` / `y2 > y` 与
  列表键），非法值 → `422 draft_invalid`。
- **写端点**（W09）：`PATCH {base_revision, paragraphs}`，`paragraphs` 是
  `{段落 id: {target?, layout?}}`：字段 `null` = 删该字段，整段 `null` = 删该段全部覆盖；
  成功返回新草稿并让 `revision + 1`。`DELETE` 清空全部覆盖，`revision` **继续 +1**
  （不回退）。`GET` 从没写过 → `revision=0` + 空 `paragraphs`（不是 404）。
- `revision`：单调递增整数，从 0 开始（第一次成功写入 → 1），每次成功写入 +1；不因重启回退。
- 乐观并发：写请求带 `base_revision`；不匹配 → `409 revision_conflict`
  （`detail.current_revision`）。两标签页冲突由前端提示刷新/重试。
- **活动任务期间草稿只读**（EXECUTION.md 纠偏 7）：该文档已有 `queued`/`running` job
  （含正在跑的编译）时，`PATCH`/`DELETE` → `409 document_busy`（`detail.job_id`）。
- 草稿保存触发服务端 1.5s 防抖编译（纯服务端定时器，浏览器断开不丢）；编译在跑的
  1.5s 内到点也会被跳过（不重复编译）。重启后定时器丢失 = 下次写草稿再触发。

### 3.4 jobs（已实现 W07：`run`/`check`；W09：`compile`；`retranslate` 见 §3.6）

```json
POST /api/v1/documents/{did}/jobs
{"action": "run", "from": "build", "pages": "1,3-4", "dual": false, "profile": "deepseek-flash"}
→ 202 {"job_id": "j_01H...", "status": "queued", "action": "run"}

POST /api/v1/documents/{did}/jobs
{"action": "compile", "scope": "full", "base_revision": 7}
→ 202 {"job_id": "j_01H...", "status": "queued", "action": "compile"}
```

- `action` ∈ `run` | `retranslate` | `compile` | `check`（固定四值）。
- 客户端只能给：`action`、`from`、`pages`、`dual`、`profile`、`scope`、`base_revision`；
  `translator`/`reviewer`/`timeout`/`api_key` 之类收到即 `422 forbidden_field`（不是"参数错了"）。
- `from` 只对 `action=run` 有效，取值 = §3.1 的 7 个阶段。起点是 `parse`（含缺省）时
  服务端在 argv 里**自动带上** `<workdir>/source.pdf` 位置参数（parse 的输入；上传后就在
  那里）；`translate` 及之后不需要它（也不接受客户端传 PDF 路径）。parse 的 MinerU token
  由 **serve 进程环境**（`MINERU_API_TOKEN`）提供，客户端永远不传。
- `paragraph_ids` / `feedback`（旧设计稿里的字段）**不在** `POST /jobs` 的请求体里：重译候选
  必须绑定一个段落（候选 id 由服务端发号），入口是 §3.6 的
  `POST /documents/{did}/paragraphs/{pid}/retranslate`。`POST /jobs` 收到 `action=retranslate`
  仍然诚实报 `422 action_not_available`（`detail.hint` 给出替代入口），不返回假 job。
- `scope` / `base_revision` 只对 `action=compile` 有效（给了别的 action → `422 forbidden_field`）；
  `from` / `pages` / `dual` 只对 `action=run` 有效。v1 页级编译按已批准设计**回退全量**，
  记录里返回 `requested_scope` / `effective_scope` / `downgrade_reason`。
- `action=compile`（W09）：不接 `profile`（不调翻译/审查，客户端带了也不进记录）。它把当前草稿
  物化到**隔离副本**里跑 `bdt run --from apply`（apply + build），成功后把
  `output/*.pdf` 原子发布回真 workdir（同名覆盖）、`compile.json` 记 `revision = 捕获的草稿 revision`；
  失败/取消/超时只删副本，**上一版 PDF 分毫不动**。成功判据是"build 阶段 ok 且副本里确实有新
  PDF"：`--from apply` 之后的质量门禁（check/review）不过在命令层面是 exit 1，但编译仍算成功，
  门禁结论留在 job `envelope` 里，`quality.pipeline_ok` 不受影响。`base_revision` 与当前草稿
  不符 → `409 revision_conflict`。
- `profile` 只接受 provider profile id；**不接受**客户端任意命令、密钥或 shell 字符串。
- 同文档同时最多 1 个活动 job（冲突 `409 document_busy`）；跨文档并发但全局限流。
- `GET /api/v1/jobs/{jid}`、`POST /api/v1/jobs/{jid}/cancel`、`GET /api/v1/documents/{did}/jobs`（`?status=` 按状态过滤，新 → 旧）。
- job 记录里的 `trigger`（W12）**只有 `action=compile` 有值**：`debounce` = 草稿保存后的服务端
  防抖自动编译，`manual` = 显式 `POST /jobs {action:"compile"}`；其它 action（含重启恢复留下的
  历史记录）为 `null`。版本归档靠它记每一版是怎么来的（§3.7）。
- 取消：终止整个进程组（连带 translator 孙进程），清理后才释放文档锁；明确不自动重跑收费调用。
- job 状态持久化（重启后核对进程身份）；不凭孤立 PID 发信号。
- `GET /jobs/{jid}` 的 `envelope` **已脱敏**（W08）：`data.config.translator/reviewer` 的命令
  字符串替换为 `<profile:<id>>`，带 token 的 `data.debug.url` 已移除（`run_id`/`manifest` 保留）。
  落盘的那一份（`.bdt-serve/jobs/<jid>.json` 快照）就是脱敏后的文本；`jobs.jsonl` 是状态
  变迁审计日志（只记状态/时间/pid，不带 envelope）。
- job 记录里的 `paragraph_id` / `candidate_id` 只对 `action=retranslate` 有值（§3.6）：前端从
  job 就能找到它对应的候选，不需要自己拼 `pid → 最新候选` 的映射。

### 3.5 上传与 profiles（已实现 W08）+ 其余端点（未实现）

| 路径（前缀 `/api/v1`） | 说明 | 任务 |
|---|---|---|
| `POST /documents` | multipart 上传 PDF → 建 did | 已实现（W08） |
| `GET/PUT /profiles` | provider profiles（仅名字对前端可见） | 已实现（W08） |
| `GET/PATCH/DELETE /documents/{did}/draft` | 草稿读写（§3.3） | 已实现（W09） |
| `POST /documents/{did}/paragraphs/{pid}/retranslate`、`GET …/candidates`、`POST …/candidates/{cid}/adopt\|reject` | 重译候选：生成不改译文、采用进草稿（§3.6） | 已实现（W11） |
| `GET /documents/{did}/versions[/{revision}/pdf]` | 版本归档清单 + 任一版本下载（§3.7） | 已实现（W12） |
| `GET/PUT/DELETE /glossary` | 全局术语表 CRUD（JSON 整表；CSV 只在前端） | 已实现（W13，§3.8） |

上表**尚未实现**的行：调用它们会得到 `404 not_found`（统一错误信封），不要在前端把
`404` 当成业务错误处理。

#### `POST /documents`（W08）

`multipart/form-data`，字段名 `file`（其余字段忽略）。校验 `%PDF-` 魔数与 200MB 上限，
在服务根目录下建 `up-<slug>-<yyyymmdd-hhmmss>` 目录（`slug` 只取文件名的 `[a-z0-9-]`，
同名冲突递增 `-2`/`-3`），字节流式写进 `<did>/source.pdf`（tmp + rename 原子落盘，
**不预建** `agent/` 骨架）。上传后 W05 的"原文"预览（`kind=source`）与文件库列表立即可用。

```json
→ 201 {"did": "up-attention-is-all-you-20260917-172233", "bytes": 1300917, "source": "source.pdf"}
→ 413 {"error": {"code": "file_too_large", "message": "…", "detail": {"limit_bytes": 209715200, "size_bytes": 316457912}}}
→ 422 {"error": {"code": "invalid_pdf", "message": "…", "detail": {"reason": "not_pdf", "magic": "3c68746d6c"}}}
```

#### `GET/PUT /profiles`（W08）

```json
GET → 200 [{"id": "deepseek-flash", "label": "Deepseek Flash", "has_translator": true, "has_reviewer": false}]

PUT {"id": "deepseek-flash", "label": "DeepSeek Flash", "translator_script": "scripts/agy-translator.sh", "reviewer_script": null}
→ 200 {"id": "deepseek-flash", "label": "DeepSeek Flash", "has_translator": true, "has_reviewer": false}
```

- 响应里**没有命令字段**：translator/reviewer 命令字符串只存在于服务端
  （`<store_base>/.bdt-serve/profiles.json`），前端只用 `id` 提 job、用 `label` 显示。
- `translator_script`/`reviewer_script` 是**脚本路径引用**（`^scripts/[A-Za-z0-9._/-]+$`），必须解析到
  `<store_base>/scripts/` 或仓库 `scripts/` 白名单目录内且确实存在（`..` 穿越/符号链接越界拒绝）；
  含空格引号分号 `$` 之类 shell 元字符 → `422 forbidden_field`，白名单外/不存在 → `422 script_path_forbidden`。
  服务端把它解析成**绝对路径**再落盘（子进程 cwd = 文档 workdir，相对引用在那里解析不到）。
- 字段**缺席** = 不动该字段；显式 `null`/空串 = 删该字段；三个字段都空 = 删整个 profile
  （之后用它提 job 会 `422 unknown_profile`）；只给 `id` 的请求是 `422 validation_error`。
- `translator`/`reviewer`/`api_key`/`command`/`shell` 之类字段收到即 `422 forbidden_field`（不回显）。
  这个判断在**模型校验之前**执行（FastAPI 依赖）：`{"id":"x","translator":"echo hi"}` 即使同时
  缺"可改字段"，报的也是 `forbidden_field`，不是 `validation_error` —— 别把它当成参数没写全。
- 写盘是同目录 tmp + `os.replace` 的原子写，且只动目标 id 那一条。
- `DELETE /profiles` 不在 v1 范围（"清空三个字段"就是删除）。

### 3.6 重译候选（已实现 W11）

对不满意段落让 AI 重译，但**候选未采用前绝不进正文**。四条端点：

```json
POST /api/v1/documents/{did}/paragraphs/P05-002/retranslate
{"profile": "deepseek-flash"}
→ 202 {"candidate_id": "c_0001", "job_id": "j_01H...", "status": "queued", "action": "retranslate"}

GET /api/v1/documents/{did}/paragraphs/P05-002/candidates
→ 200 {"pid": "P05-002", "items": [
    {"id": "c_0002", "pid": "P05-002", "source": "……原文……", "baseline_target": "基线译文",
     "candidate_target": "候选译文", "status": "pending", "model_label": "deepseek-flash",
     "job_id": "j_01H...", "created_at": "2026-09-17T15:54:45.123Z", "adopted_at": null}]}

POST /api/v1/documents/{did}/paragraphs/P05-002/candidates/c_0002/adopt
→ 200 {"revision": 4, "updated_at": "...", "paragraphs": {"P05-002": {"target": "候选译文", ...}}}

POST /api/v1/documents/{did}/paragraphs/P05-002/candidates/c_0002/reject
→ 200 {"id": "c_0002", "status": "rejected", ...}
```

存储：`<did>/.bdt-serve/candidates.json`（服务端私有状态，与 `draft.json` 同级，**不进**
`agent/`，也不在产物下载白名单里）：

```json
{"version": 1, "next_id": 3, "items": [
  {"id": "c_0001", "pid": "P05-002", "source": "……", "baseline_target": "基线译文",
   "candidate_target": "候选译文", "status": "pending", "model_label": "deepseek-flash",
   "job_id": "j_01H...", "created_at": "2026-09-17T15:54:45.123Z", "adopted_at": null}]}
```

- `status` ∈ `pending` | `adopted` | `rejected`（状态机：`pending` → 采用/拒绝；都是终态）。
- `id` = `c_` + 4 位十进制（`next_id` 单调发号，删行不回退）。
- `source` / `baseline_target` 是**生成时刻**的快照：`source` 与 `GET /paragraphs` 的
  `source` 同源（canonical）；`baseline_target` 取 `translated.jsonl`。
- `candidate_target` 与 `GET /paragraphs` 的 `target`、草稿 `target` **同一表示**（canonical IR
  占位符）。为 `null` = 还在生成中（job 结束前）；生成失败/被取消的候选行会被**删掉**
  （列表里不留半条），所以这个态只在 job 活动期间可见。
- `model_label` 是 profile **id**（命令字符串永不出现）；`job_id` 是生成它的
  `action=retranslate` job（该 job 的 `paragraph_id`/`candidate_id` 指回这里）。
- 列表顺序：同一段里最新的 `pending` 在前，其后已决定的候选（新 → 旧）。
- 重启：已生成的候选仍在（`candidates.json` 持久化）；**生成中**（`candidate_target=null`）
  的行在启动时删掉 —— job 不会跨进程存活，那些行永远等不到结果。

**零副作用（红线）**：`retranslate` 只写 `candidates.json` + job 状态。生成在
`<did>/.bdt-serve/candidates-<job_id>/` 的**隔离副本**里跑 `bdt translate --ids <pid>`，
`agent/translated.md`、`agent/translated.jsonl`、`draft.json`、`compile.json`、`output/`
一概不动（副本用完即删）。理由：`translate.retranslate_blocks` 会把新译文合并进
`translated.md`，只能在副本里跑；`translated.md` 的变更只允许发生在**采用后的编译**
（隔离副本 → 发布），这条链路是 §3.2/§3.3 的那一条。

**命令来源**：请求体只有一个 `profile`（id）。translator 命令由服务端从 profile 解析后进
argv，客户端永远不传命令/密钥（带了 → `422 forbidden_field`）。v1 不接受 `feedback` 之类
自由文本（提示词由服务端拼）。

**采用（adopt）**：写草稿 `target = candidate_target`（走 :3.3 的草稿通道：`revision+1`，
**不需要** `base_revision` —— 服务端在草稿写锁内取当前 revision）→ 候选标 `adopted` +
`adopted_at` → 触发 1.5s 防抖编译（与 `PATCH /draft` 完全同一条链）。返回体就是新草稿
（与 `PATCH /draft` 同形状），前端可直接写进草稿缓存。

**错误码**（都走统一错误信封）：

| 场景 | 状态码 | `code` |
|---|---|---|
| 文档不存在 / 越界 | 404 / 400 | `document_not_found` / `invalid_document_id` |
| `pid` 不在段落产物里（含形状不合法） | 404 | `paragraph_not_found` |
| `cid` 不存在，或不属于路径上的 `pid` | 404 | `candidate_not_found` |
| 没给 `profile`，或该 profile 没配 translator | 422 | `profile_missing` |
| 未知 profile | 422 | `unknown_profile`（`detail.available` 只列 id） |
| 请求体里带 `translator`/`reviewer`/`timeout`… | 422 | `forbidden_field` |
| 候选已采用/已拒绝（再采用/再改回去） | 409 | `candidate_decided` |
| 候选还在生成中（`candidate_target=null`）就采用 | 409 | `candidate_not_ready` |
| 该文档有活动 job：生成（要排队）/ 采用（要写草稿） | 409 | `document_busy`（`detail.job_id`） |

- **拒绝不受忙限制**：它不改草稿、不触发编译，重复拒绝幂等（200 返回当前候选）。
- 生成是一个 job：排队/全局并发上限/取消/重启恢复全部与 §3.4 同一套规则；`job.profile` 是
  所用 translator profile 的 id，`job.from_stage` 为 `null`。取消会连带杀掉 translator
  孙进程（收费调用），候选行随之删掉。
- 候选**不影响** `GET /paragraphs` / `GET /documents/{did}`（§3.1/§3.2）：采用之前，预览、
  详情、编译产物里都看不到候选译文；采用之后，草稿 revision 变了，`compile.stale` 才是
  那个"该重新编译"的真信号。

### 3.7 版本归档（已实现 W12）

每次**成功**的编译发布（`settle_compile` 的成功分支）都把那一份 PDF 自动归档成一个版本：
`<did>/.bdt-serve/versions/<revision>.pdf`（同一 revision 一份）+ 往
`<did>/.bdt-serve/versions.json` 追加一行。`revision` = 那次编译捕获的草稿 revision
（与 `compile.revision` 同源）。归档**不改发布语义**：`output/` 的最新 PDF 仍是下载键主路径
（W10 的下载按钮不改，只是多了「历史版本」入口），versions 是历史。

```json
GET /api/v1/documents/{did}/versions
→ 200 {
  "did": "paper",
  "current_revision": 3,
  "stale": true,
  "items": [
    {"revision": 3, "created_at": "2026-09-17T15:54:45.123Z", "trigger": "debounce",
     "artifact_name": "paper.mono.pdf", "bytes": 123456,
     "sha256_head": "3f1c…", "quality": {"check_verdict": "pass", "pipeline_ok": true}},
    {"revision": 2, "created_at": "2026-09-17T15:20:03.004Z", "trigger": "manual",
     "artifact_name": "paper.mono.pdf", "bytes": 121000,
     "sha256_head": "9ab0…", "quality": {"check_verdict": "needs_fix", "pipeline_ok": false}}
  ]
}

GET /api/v1/documents/{did}/versions/2/pdf
→ 200 application/pdf（Content-Disposition: inline; filename="paper.mono.r2.pdf"）
→ 404 {"error": {"code": "version_not_found", "message": "…", "detail": {"revision": "…"}}}
```

- `items` **新 → 旧**（清单文件本身是 `revision` 升序）：`revision`、`created_at`（归档时刻，
  UTC ISO8601 毫秒 + `Z`）、`trigger`、`artifact_name`（发布时的**裸**产物名）、`bytes`、
  `sha256_head`（该版本文件的**整文件** sha256：核对「下载到的就是当时那一份」。字段名沿用
  W12 冻结形状；取整文件而不是只取头部，是因为实测同一文档两版编译产物的**前 19MB 逐字节相同**，
  头部指纹区分不了版本。分块读，65MB 约 40ms）。
- `current_revision` / `stale` 与详情端点的 `compile`（§3.2）**同一判据**：当前可下载的那一版
  恒是 `compile.artifact`（`output/` 的最新发布），不是清单里的最新行；`stale = 草稿 revision >
  current_revision`。前端据此高亮「当前版本」并显式提示「有未编译修改」。
- `trigger` ∈ `debounce`（草稿保存后服务端 1.5s 防抖自动编译）/ `manual`（显式
  `POST /jobs {action:"compile"}`）；同一来源也记在 job 记录的 `trigger` 字段（§3.4）。
- `quality` 是**发布时刻**的质量快照（与详情端点同一实现），**只记录不门禁**：
  `check_verdict=needs_fix` / `pipeline_ok=false` 的版本照样归档、照样可下载（前端黄标，
  与 W10 的徽标规则一致）。归档不看质量，编译成功 ≠ 质量通过。
- **保留最近 50 个版本**：超出删最旧的（版本文件 + 清单行）。被删的永远是最旧的，因此
  不会删掉 `current_revision` 指向的那一版（它是最大值）。
- **读边界**：`.bdt-serve/versions/` **不在** W03 的产物白名单里（`GET /artifacts` 清单里
  永远不会出现 versions），版本 PDF 只能经 `…/versions/{r}/pdf` 读：只认清单里有、且在
  `versions/` 目录里的十进制数字名文件；非数字 / 不在清单 / 文件缺失 / 符号链接越界一律
  404 `version_not_found`（同一个码，不泄露存在性）。下载响应是 `inline`（由前端 `<a download>`
  决定是否落盘），服务端给的名字是 `<原产物名去 .pdf>.r<revision>.pdf`。
- **持久化**：清单与版本文件都在盘上，serve 重启后照旧（重启不影响历史）。从没编译成功过
  → `200` + `items: []` + `current_revision: 0`（不是 404）。失败/取消/超时的编译**不归档**：
  上一版仍可下载，历史不多一行。

### 3.8 术语表（已实现 W13）

**一个全局词表**，不属于任何文档：落在 `<store_base>/.bdt-serve/glossary.csv`
（`--root` 模式 = 服务根目录，`--workdir` 模式 = 那个 workdir；两个模式都只有这一份）。
条目是「源词 → 指定译名（可选备注）」，翻译时由**服务端**渲染进提示词的术语约束段。

```json
GET /api/v1/glossary
→ 200 {"entries": [{"source": "attention", "target": "注意力", "note": null}], "count": 1}
   # 没有词表 → {"entries": [], "count": 0}（不是 404）

PUT /api/v1/glossary      # 整表替换：编辑完一次保存，不做行级 patch
  {"entries": [{"source": "attention", "target": "注意力", "note": "可选"}]}
→ 200 与 GET 同形（回显规范化后的结果）
→ 422 {"error": {"code": "glossary_invalid", "message": "第 1 条的 source 不能为空",
        "detail": {"index": 0, "field": "source"}}}

DELETE /api/v1/glossary   # 清空（幂等）→ 200 与 GET 同形（空表）
```

- **形状**：`source`/`target` 必填、`note` 可选（没有备注时响应里是 `null`）。`GET`/`PUT`/
  `DELETE` 的响应**同形**（`entries` + `count`），前端一次解析三处都能用。
- **规范化**（服务端做，`GET` 回来的就是规范形）：`source`/`target` 去空白后必须非空，
  各 ≤200 字符（`note` ≤200）；**同一 `source` 后者覆盖前者**；结果按 `source` 字典序排序。
  `PUT` 校验失败时**盘上一字不改**（先规范化再写）。`PUT {"entries": []}` 与 `DELETE` 同义。
- **CSV 只在两端各自那一侧**：`PUT` 只收 JSON 条目（**不收 CSV 文本**）；CSV 的解析/导出在
  前端（列 `source,target,note`，导入导出都先转成 `entries` 再 PUT）。后端落盘的那份 CSV 与
  CLI（`bdt translate --glossaries <csv>` / `bdt run --glossaries <csv>`）**共用同一个解析器**。
- **注入语义**：`POST /documents/{did}/jobs` 的 `use_glossary`（**缺省 true**，客户端只能给这个
  布尔）决定本次翻译要不要带术语约束。服务端在构造 argv 时把
  `<store_base>/.bdt-serve/glossary.csv` 的**路径**经 `--glossaries` 传给子进程 —— 客户端
  永远拿不到、也传不了这个路径，更传不了词表内容。job 记录回显生效后的 `use_glossary`
  （§3.4）供前端核对。
- **只在翻译阶段注入**：`action=run` 且真的会跑 `translate` 阶段（`from` 缺省 / `parse` /
  `translate`）才注入。`compile`、`check`、`run --from apply|build|check|review|report`、
  以及 `retranslate`（重译候选）**一律不注入**。重译候选走 `translator-repair` 模板、保持
  段落上下文自由 —— 词表约束的是整篇初译，不是重译候选。`use_glossary` 在记录里对非
  `action=run` 的 job 恒为 `false`（要么真生效，要么别声称生效）。
- **空词表不注入**：文件不存在、只有表头、或 `DELETE` 之后，`use_glossary=true` 也不会带
  `--glossaries`（给提示词塞一段没有条目的约束说明毫无收益）。
- **词表变更不回溯**：改词表**不会**自动重翻任何已翻译内容 —— 已经译过的段落保持原样，
  重新跑一次翻译才生效。前端词表视图必须显式提示这一点。
- **错误码**：`glossary_invalid` → 422（条目不合法，`detail.index`/`detail.field` 指到出错的
  第几条/哪个字段）；`forbidden_field` → 422（从 job 提交端口进来的命令类字段，与 §3.4 同源）；
  坏文件（手工改坏）**不报错**，按空词表读（注入少几条 < 整个服务起不来）。

## 4. 前端消费注意

- 事件 `data` 里**没有** `level` 字段：严重级别只能由 `kind` + `data.status`
  （`ok`/`error`/`interrupted`）、`data.returncode`、`data.error_code` 推导；
  推导表放前端 `web/src/events/humanize.ts`。
- 跨 run 合并事件流必须用 `(run_id, seq)`；单 run 内 `seq` 从 1 递增。
- 阶段耗时只信 `run_state.json` / `debug/runs/<run_id>/manifest.json` 的真实时间戳。
- 不支持流式的 translator：显示真实阶段等待，输出到达后再更新段落计数。
- **job 状态的真相在轮询，`job_update` 只是加速键**（W14）：前端收到 `job_update` 就立刻
  invalidate 对应查询（按 `action` 分流），**不**把轮询关掉 —— 活动 job 的兜底轮询 5s、
  空闲 30s。理由：`job_update` 不重放、且“job 建出 run 归档之前”那段没有 SSE（§1.4.1）；
  只靠 SSE 会在丢帧/断线时静默不动。
- **段落完成度只能由详情字段推**（W14 实测）：`translated_count` / `paragraph_count`（§3.1）。
  真实 `bdt run` 的 translate 阶段是**一次整篇子进程调用**，既不产生段落级也不产生 batch 级
  事件（只有 stage_started/artifact_bundle/call_started/call_finished/text_version/
  missing_ids/stage_finished/stage_error），所以**翻译子进程运行中 N 不会跳动**，
  `translated_count` 要等 `apply` 把 `agent/translated.jsonl` 写出来才变。
  前端必须如实标注（例如「已译段落（apply 后更新）」），**不得**做跳动动画或假进度；
  段落级实时需要把 translator 改成流式/分段调用（超出本阶段范围）。

## 验收修复：OpenAI 兼容模型配置（本机后端）

模型与高级脚本共享 profile ID 命名空间（`[a-z0-9-]{1,64}`）。`GET /profiles`
继续返回精确的 `{id,label,has_translator,has_reviewer}` 形状，不新增字段、不返回命令；
模型条目 `has_translator=true, has_reviewer=false`（审校需显式选择，不隐式启用）。
脚本与模型同 ID 写入返回 `409 profile_collision`，环境变量碰撞也拒绝解析，不覆盖。

- `GET /models`：模型元数据数组，按 ID 排序。
- `PUT /models`：整条元数据更新，必填 `id,label,base_url,model`；可选 `api_key`。
  API Key 缺省或 null 保留原值；创建时可省略 key（本地无认证服务）。空 key 拒绝。
  换 key 需显式给非空值；要清除凭据请删除后重建。仅保存，不发送网络请求。
- `GET /models/{id}`：单条元数据；不存在 `404 unknown_model`。
- `DELETE /models/{id}`：删除配置和凭据，204；不存在 404。
- `POST /models/{id}/test`：**可能产生费用**，显式发送 `Reply only OK.` 短生成请求
  (`max_tokens=8`)，成功只返回 `{ok:true}`，不回传模型原文。

元数据为 `{id,label,base_url,model,has_api_key}`，任何响应不含 API Key。
Base URL 应为 API 根（例如 `https://example.com/v1`），后端追加 `/chat/completions`。
只允许 HTTPS，loopback 本地服务可 HTTP；禁止 userinfo/query/fragment/空白。
请求不使用环境代理、不自动重试、不跟随重定向，避免认证转发到其它站点。
错误为脱敏错误信封：`model_invalid` 422、`model_auth` 422、`model_address` 422、
`model_not_found` 422、`model_request` 422、`model_timeout` 504、`model_response` 502。
响应体/异常原文均不作为错误消息回显。

凭据保存在 `<store_base>/.bdt-serve/model-credentials/models.json`，目录 0700、
文件 0600，同目录随机临时文件 + fsync + 原子替换；拒绝符号链接和不安全文件权限。
该目录被 Git 忽略。这是本机文件权限保护，**不是加密**。

`POST /documents/{did}/jobs` 保留必填 `profile`（翻译配置）。当它选择模型时，
新增可选 `reviewer_profile`：另一个或相同模型 ID；缺省/null = AI 审校关闭。
脚本 profile 行为保持不变，不能设置 `reviewer_profile`；compile 不接受该字段。
关闭 AI 审校只跳过 AI，排版/链接等本地检查与报告照常执行；reviewer 状态为
`skipped`，不伪装成 pass，`quality.pipeline_ok` 仍不声称通过 AI 审校。
现有段落候选重译也可选择模型 profile。

唯一命令适配器为 `bdt model-call --store-base <server-store> --model-profile <id>`：
stdin 提示词、stdout 纯文本（为现有 translator/reviewer 子进程协议而非 JSON 信封），
错误仅写安全代码/说明到 stderr。argv 仅含本机路径与 ID，不含 key、URL、模型名。
模型调用最多 60 秒；`bdt run --skip-ai-review` 可显式跳过 AI，脚本默认行为不变。

模型审校是**纯文本审校，不是视觉审校**：内联 document.md、translated.md 和三类
本地检查 JSON（去除配置/凭据字段），不发送 PDF/图片/无关产物；累计超过 1 MB
返回 `model_context_too_large`，不静默截断。需改用高级脚本审校或关闭 AI 审校。
本轮验证只用 mocks；真实模型费用/兼容性需用户另行批准后手动测试。
