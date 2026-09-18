# Task
W03：事件分页 + SSE 流 + 产物白名单 Range 下载

## Objective
在 feat/web-frontend（HEAD 198455a，W02 已合入）实现 `docs/frontend/api.md` §1.3/§1.4/§1.5 与 §3.1 后三行：`GET /documents/{did}/events`（分页）、`GET /documents/{did}/events/stream`（SSE 实时流）、`GET /documents/{did}/artifacts` + `GET /documents/{did}/artifacts/{name}`（清单 + Range 下载）。这是 pdf.js 预览与实时进度条的硬前提。

## Context
必读：AGENTS.md、`.plan/web-frontend/EXECUTION.md`、`docs/frontend/api.md`（§1.3 分页游标 / §1.4 SSE / §1.5 归档下载，契约已冻结）、W01/W02 代码（`serve/{store,workdir,views,schemas,app}.py`、`serve/routers/documents.py`、测试风格）。

事件事实（主控已用真实 run 探明）：
- `events.jsonl` 每行 `{seq, at, stage, kind, data}`；`seq` 从 1 递增、**只在单 run 内**；`at` 是带 `+00:00` 的 UTC ISO。
- 真实体量：单 run 3758 条 / 44 种 kind（最大 run 见 `tmp/e2e-2602-02908v2-20260917/debug/runs/20260917T125056Z-0001e6/`，可做人工核对样本，不进测试）；`tmp/ccs3764-dyn/.../000183/` 只有 12 条（重放 run，正常）。
- **必须复用** `babeldoc.debug_recorder.read_events(run_dir, after_seq)`：它已处理半行（未以换行结尾的尾行不发布）、损坏行跳过、廉价 seq 前缀跳过。不要自己重写解析。
- run_id 目录序即时间序（W02 的 `WorkdirReader._run_ids` 已实现新→旧排序）。

产物下载事实：
- workdir 内可下载产物：`source.pdf`（如存在）、`output/*.pdf`（mono/dual）、`agent/translated.md`、`FINAL_REPORT.md`、`agent/*.json`（几何/检查等，前端可能要拉）。**白名单目录**：`output/`、`agent/`、workdir 根下的单文件（`FINAL_REPORT.md`、`source.pdf`）。`debug/` 只允许 `events.jsonl` 不允许 artifacts 二进制（体积大且无前端用途——本任务不做 debug 产物下载）。
- **Range 必须**：pdf.js 按 Range 请求 PDF；返回 `Accept-Ranges: bytes`、206 + `Content-Range`，`HEAD` 也要支持。用 Starlette `FileResponse`（已内置 Range 支持）或手写，二选一，但要测试 206/416/多段可不测多段。
- 注意 W02 `_pdf_output` 已实现"路径在 workdir 内"检查，可复用/借鉴，但 artifacts 白名单逻辑独立放 `serve/artifacts.py`（新模块）。

## Deliverables
1. `babeldoc_tools/serve/routers/events.py`：
   - `GET /documents/{did}/events?after_seq=&limit=&stage=&kind=` — 分页。默认 limit=500（上限 2000）。响应 `{events: [...], next_after_seq, has_more}`。**next_after_seq 是扫描位置不是匹配位置**（即使过滤后本页 0 条也要推进，否则过滤条件卡死轮询——契约 §1.3 红线）。`stage`/`kind` 过滤在前端轮询语义下必须仍能推进游标。
   - 没有任何 run（`debug/runs` 不存在或空）→ 404 `events_unavailable`（与 W02 的 404 语义一致，不返回空 events 假成功）。
   - 选择哪个 run：默认最新 run；`?run_id=` 可指定（校验 RUN_ID_RE 形状，不存在 → 404）。
   - `GET /documents/{did}/events/stream?after_seq=&run_id=` — SSE。`text/event-stream`，每条 `event: <kind>\nid: <run_id>:<seq>\ndata: <json>`；`Last-Event-ID` 头解析 `<run_id>:<seq>` 续传。**轮询 tail**：每 0.5s 调 `read_events(run_dir, after_seq)` 推增量；job 生命周期事件 W07 才有，本任务只转发 events.jsonl 的真实事件。客户端断开要停（Starlette StreamingResponse 的 `request.is_disconnected` 或 asyncio 取消）。**心跳**：15s 无事件发一行 `: ping\n\n` 注释行防代理超时。SSE 测试用 httpx 流式读（TestClient 支持 `stream=True`... 实际上 TestClient 对 streaming 支持有限——可用 `client.stream` 或直接测 generator 函数单元，不必强行起真服务器；至少单测 generator 产出的帧格式）。
2. `babeldoc_tools/serve/artifacts.py` + `serve/routers/artifacts.py`：
   - `GET /documents/{did}/artifacts` — 清单：扫描白名单目录，返回 `[{name, path, kind(pdf|markdown|json|report|source), size, mtime}]`。`name` 是稳定短名（如 `output/mono.pdf` 或 `source.pdf`），前端用它做下载 URL 的 `{name}` 参数。
   - `GET /documents/{did}/artifacts/{name}` — 白名单内才可下载（路径解析后必须在 workdir 内且落在白名单目录/文件集合；否则 404 `artifact_not_found`——不泄露存在性）。支持 Range/HEAD。`Content-Type` 按扩展名（.pdf → application/pdf 等）。name 含 `/`（如 `output/x.pdf`）是正常情况，路由参数用 `{name:path}` 转换器。
   - `..`/绝对路径/符号链接越界 → 拒绝（复用 W01 store 的判定思想，但这是文件级不是目录级）。
3. `schemas.py` 扩展：`EventsPage`、`ArtifactItem`、`ArtifactsResponse`（SSE 帧不走 pydantic）。
4. `tests/test_serve_events.py` + `tests/test_serve_artifacts.py`：自包含 fixture（手写小 events.jsonl 含半行/损坏行、多个 run），覆盖：分页无丢失无重复、过滤后游标仍推进、has_more 边界、半行不发布、SSE 帧格式（`event:`/`id:`/`data:` 三行齐全、id 是 `run_id:seq`）、Last-Event-ID 续传（单测 generator 逻辑）、无 run 404、清单不泄露非白名单、Range 206/416、HEAD、越界 name 拒绝、符号链接越界拒绝。
5. `docs/frontend/api.md`：§3.1 后三行改"已实现（W03）"；如实现与契约有偏差（字段名/默认值），**先停下报告**，经主控确认才改契约。

## Constraints
- 只新增/修改：`babeldoc_tools/serve/`（新 events/artifacts 路由与模块、schemas、app 接线）、两个新测试文件、`docs/frontend/api.md` 的已实现标记。
- 禁止改：translate/run/layout/debug_* 核心、`babeldoc/debug_recorder/`（只 import 复用）、store.py 安全边界（可加方法但不改既有行为）、W02 视图。
- SSE 实现注意：uvicorn 下 StreamingResponse + async generator；tail 循环里读文件是同步 IO，用 `asyncio.to_thread` 或直接在 async 里读（本地 SSD 小文件可接受）；不要每 0.5s 重读全文件（read_events 的 seq 前缀跳过已经廉价，但 3758 条也别每次全 parse——信任 read_events 的实现，不必再优化）。
- 不实现：job 生命周期事件合成（W07）、草稿/编译（W09）。
- 事件 `data` 原样透传不裁剪（前端展开原始 JSON 用）。
- 大产物清单不递归 `debug/` 全树（artifacts 二进制不进白名单）。
- rg 限定路径 + timeout；不委派；不 commit/push；不改 .plan。

## Validation
```
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest tests/test_serve_events.py tests/test_serve_artifacts.py tests/test_serve_documents.py tests/test_serve_app.py tests/test_serve_store.py tests/test_single_entry.py -q --basetemp="tmp/pytest-W03-$(date +%Y%m%d-%H%M%S)"
.venv/bin/ruff check babeldoc_tools/serve tests/test_serve_events.py tests/test_serve_artifacts.py
git diff --check
```
真实核对：`bdt serve --workdir tmp/e2e-2602-02908v2-20260917`，curl：events 分页翻 3 页核对 seq 连续无重叠；`Range: bytes=0-99` 拿 mono PDF 前 100 字节（206）；artifacts 清单不含 debug/ 二进制。SSE 用 curl -N 手测 ≥2 秒确认有心跳或既有事件到达（run 已结束则应立即收到存量事件后转入心跳）。证据（命令+截断输出）写进报告。注意：启动 `bdt` 前先跑 `stat -f '%Sf' .venv/lib/python3.12/site-packages/_editable_impl_babeldoc_agent.pth`，若显示 hidden 则先 `chflags nohidden` 该文件（本机已知问题）再测。

## Report back
报告含：改动清单、分页连续性实测（3 页 seq 无缝）、SSE 帧样例、Range 206 实测、白名单越界测试摘要、验证命令输出、遗留风险。绑定 output 回传。最多两轮修复；与契约冲突时停下报告。
