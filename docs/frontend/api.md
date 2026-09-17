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
| 405 | `method_not_allowed` | 方法不允许（带 `Allow` 头） |
| 409 | `revision_conflict`（计划） | 草稿乐观并发失败，`detail.current_revision` 给最新值 |
| 409 | `document_busy`（计划） | 同文档已有活动 job |
| 422 | `validation_error` | 请求体/参数校验失败，`detail.errors` |
| 500 | `internal_error` / `invalid_root` | 服务内部错误；启动时根目录非法 |
| 503 | `root_missing` | 运行期根目录不可用（health 也不谎报 `ok`） |

CLI 层（非 HTTP，仍是 `bdt` 的单行 JSON 信封）：`web_extra_missing`（缺
fastapi/uvicorn，message 里给安装命令）、`invalid_port`、`port_unavailable`。

### 1.3 分页 / 游标

- 事件分页：`?after_seq=<int>&limit=<int>`，响应 `{"events": [...], "next_after_seq": <int>, "has_more": <bool>}`。
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

### 1.5 归档下载

- `GET /documents/{did}/artifacts`：产物清单（`name`、`size`、`mtime`、`revision`、`kind`）。
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

W01 **只**有这两个端点；没有写端点、没有假 stub。校验（越界/符号链接/`did` 规则）已在
服务端 resolver 实现并被测试覆盖；W02 起这些规则经 §3.1 的六个只读文档端点暴露。

## 3. 契约词汇（字段冻结；端点在后续任务实现）

### 3.1 只读文档端点（前六个已实现：W02；其余待实现：W03）

| 方法 | 路径 | 说明 | 状态 |
|---|---|---|---|
| GET | `/api/v1/documents` | 文档列表（`did`、页数/段数、阶段状态、最近活动时间） | 已实现（W02） |
| GET | `/api/v1/documents/{did}` | 元信息：`pdf`、`config`、页数/段数、质量与编译状态 | 已实现（W02） |
| GET | `/api/v1/documents/{did}/stage-state` | 7 阶段状态 + **真实** `started_at`/`finished_at`/耗时 | 已实现（W02） |
| GET | `/api/v1/documents/{did}/paragraphs` | 段落面板数据（原文/译文/排版参数 join） | 已实现（W02） |
| GET | `/api/v1/documents/{did}/geometry` | `?kind=parse\|layout&page=N`，bbox（`pdf_topleft`） | 已实现（W02） |
| GET | `/api/v1/documents/{did}/check` | 结构审查/排版 lint/链接审计三组组装 | 已实现（W02） |
| GET | `/api/v1/documents/{did}/events` | 分页事件（§1.3） | 未实现（W03） |
| GET | `/api/v1/documents/{did}/events/stream` | SSE（§1.4） | 未实现（W03） |
| GET | `/api/v1/documents/{did}/artifacts[/{name}]` | 清单 + 白名单 Range 下载 | 未实现（W03） |

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
- `compile.status` ∈ `none` | `running` | `ok` | `failed`；`compile.revision` = 编译时捕获的草稿
  revision；`compile.stale = (draft.revision > compile.revision)`。
- 下载/展示必须带 `artifact.revision` 与 `stale`：失败或取消不得破坏上一份可下载 PDF，
  旧 PDF 不标成最新（`stale: true` 时前端显式提示）。

### 3.3 草稿与 revision（未实现：W09）

`<did>/draft.json`：

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

- `target`：该段译文（覆盖 `agent/translated.jsonl` 的同 id 值）。
- `layout`：段落排版覆盖，键名与取值范围必须与
  `babeldoc/tools/agent/layout_overrides.py` 一致 —— `scale_cap` 0.1–5.0、
  `font_scale` 0.2–5.0、`line_skip` 0.8–3.0、`box_scale` 0.3–5.0；
  `box` 是 `[x, y, x2, y2]`，**PDF 坐标 y 向上**（与几何端点的 `pdf_topleft` 相反，
  前端拖拽时必须换算，禁止直接透传屏幕坐标）。
- `revision`：单调递增整数，从 1 开始，每次成功写入 +1；不因重启回退。
- 乐观并发：写请求带 `base_revision`；不匹配 → `409 revision_conflict`
  （`detail.current_revision`）。两标签页冲突由前端提示刷新/重试。
- 草稿保存触发服务端 1.5s 防抖编译；浏览器断开不丢编译。

### 3.4 jobs（未实现：W07 / W08 / W09 / W11）

```json
POST /api/v1/documents/{did}/jobs
{
  "action": "run",
  "from": "build",
  "paragraph_ids": ["P05-002"],
  "feedback": "术语不统一",
  "scope": "full",
  "pages": [3, 4],
  "profile": "deepseek-flash",
  "base_revision": 7
}
→ 202 {"job_id": "j_01H...", "status": "queued", "action": "run"}
```

- `action` ∈ `run` | `retranslate` | `compile` | `check`（固定四值）。
- `from` 只对 `action=run` 有效，取值 = §3.1 的 7 个阶段。
- `paragraph_ids` / `feedback` 只对 `retranslate` 有效；候选**不得**直接改当前译文
  （`retranslate_ids` 会合并进 `translated.md`，必须走隔离副本，采用后才写入草稿）。
- `scope` / `pages` 只对 `compile` 有效；v1 页级编译按已批准设计**回退全量**，
  响应里返回 `requested_scope` / `effective_scope` / `downgrade_reason`。
- `profile` 只接受 provider profile id；**不接受**客户端任意命令、密钥或 shell 字符串。
- 同文档同时最多 1 个活动 job（冲突 `409 document_busy`）；跨文档并发但全局限流。
- `GET /api/v1/jobs/{jid}`、`POST /api/v1/jobs/{jid}/cancel`、`GET /api/v1/documents/{did}/jobs`。
- 取消：终止整个进程组（连带 translator 孙进程），清理后才释放文档锁；明确不自动重跑收费调用。
- job 状态持久化（重启后核对进程身份）；不凭孤立 PID 发信号。

### 3.5 其余端点（未实现，形状在各自 brief 冻结）

| 路径（前缀 `/api/v1`） | 说明 | 任务 |
|---|---|---|
| `POST /documents` | multipart 上传 PDF → 建 did | W08 |
| `GET/PUT /profiles` | provider profiles（仅名字对前端可见） | W08 |
| `GET/PATCH/DELETE /documents/{did}/draft` | 草稿读写（§3.3） | W09 |
| `GET /documents/{did}/candidates`、`POST /documents/{did}/candidates/{cid}/accept\|discard` | 重译候选（生成不合并） | W11 |
| `GET/POST /documents/{did}/versions`、`POST /documents/{did}/versions/{vid}/rollback` | 版本归档与回滚 | W12 |
| `/glossary...` | 词表 CRUD（全局 + 文档级）、CSV、命中计数 | W13 |

上表**尚未实现**：调用它们会得到 `404 not_found`（统一错误信封），不要在前端把
`404` 当成业务错误处理。

## 4. 前端消费注意

- 事件 `data` 里**没有** `level` 字段：严重级别只能由 `kind` + `data.status`
  （`ok`/`error`/`interrupted`）、`data.returncode`、`data.error_code` 推导；
  推导表放前端 `web/src/events/humanize.ts`。
- 跨 run 合并事件流必须用 `(run_id, seq)`；单 run 内 `seq` 从 1 递增。
- 阶段耗时只信 `run_state.json` / `debug/runs/<run_id>/manifest.json` 的真实时间戳。
- 不支持流式的 translator：显示真实阶段等待，输出到达后再更新段落计数。
