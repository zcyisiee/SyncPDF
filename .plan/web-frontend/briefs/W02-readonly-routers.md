# Task
W02：只读文档端点（列表 / 详情 / 阶段状态 / 段落 / 几何 / 检查）

## Objective
在 feat/web-frontend（HEAD a576db4，W01 已合入）上实现 `docs/frontend/api.md` §3.1 前六个 GET 端点，数据来自真实 workdir 产物，形状与 api.md 一致。前端消费前的第一次真数据接入。

## Context
必读：AGENTS.md、`.plan/web-frontend/EXECUTION.md`、`docs/frontend/api.md`（契约，已冻结）、W01 代码 `babeldoc_tools/serve/{app,store,schemas,cli}.py`（复用 DocumentStore / 错误信封 / TestClient 测试模式）。

真实数据参考（已由主控探明，可直接用于测试 fixture 生成）：
- `tmp/ccs3764-dyn/`：完整真实 workdir。`agent/run_state.json` 的 stages 每段只有 `{status, ok, at, duration_s}`，**没有** started_at/finished_at；真实起止时间在 `debug/runs/<run_id>/manifest.json` 的 `stages`。快照 `debug/runs/20260916T132829Z-000183/snapshots/parse/paragraphs.json`：`entities[]` = `{id, kind:"paragraph", label, page, box:{x0,y0,x1,y1}, attrs:{unicode}}`，420 条；`page-frames.json` 存在。
- **数量差异**：`agent/anchors.json` 206 rows（`id/page/layout_label/canonical/markdown/anchors`）；`agent/translated.jsonl` 420 行 `{id,target}`；`agent/layout_geometry.json` 420 paragraphs（`id/page/layout_label/src_box/layout_box/rendered_box/scale/font_scale/.../text/n_chars/n_lines` + `page_info[]{page,cropbox}` + `pages[]`）。join 必须以段落 id 为键做左连接并容忍缺侧，绝不能假设数量相等。
- **两套坐标系**：`layout_geometry` 的 box 是 PDF 原生 y 向上（src_box `[66.585, 672.353, ...]`，y 是页面底部往上）；`snapshots/parse/paragraphs.json` 的 box 是 pdf_topleft（y 向下）。几何端点必须统一标注 `coord_system` 字段（返回 `pdf_topleft` 或 `pdf_native`），**不要静默转换**，转换归前端做（契约 §3.1 已写明）。
- 检查三源：`agent/review_verdict.json`（`verdict/blockers[]/warnings[]{code,sev,id,page,count,samples,hint}`）、`agent/layout_lint.json`（`counts/findings/metrics/summary`）、`agent/link_audit.json`（`summary/findings/anchors_verified/...`）。

## Deliverables
1. `babeldoc_tools/serve/routers/documents.py`（或按 W01 风格拆分）：实现
   - `GET /api/v1/documents` — 列表：`[{did, title?, pages?, paragraph_count?, translated_count?, stage_summary, updated_at}]`。did 来自 store.list_dids()；title 取 run_state 或 PDF 元信息可得则取，不可得为 null，不造假。
   - `GET /api/v1/documents/{did}` — 元信息（api.md §3.2 的 quality/compile 结构中，**只实现能从现有产物推导的字段**；compile 部分返回 `status:"none"` 占位语义，revision 用 0——W03/W09 才有真实编译产物，这里不造假值）。
   - `GET /api/v1/documents/{did}/stage-state` — 7 阶段状态。优先合并 `debug/runs/<最新 run_id>/manifest.json` 的真实起止时间与 run_state 的 status/duration；manifest 缺失时用 run_state 的 `at`/`duration_s` 并标注 `timing_source: "run_state"|"manifest"`。**取最新 run**：run_id 是可排序的时间戳格式（`20260916T132829Z-000183`），目录名排序取最大。
   - `GET /api/v1/documents/{did}/paragraphs` — join 产物：以 anchors 或 geometry 的 id 集为底（选完整的一侧），左连接 translated.jsonl / layout_geometry / parse 快照（可选 fallback：快照缺失时该段 geometry 字段为 null）。支持 `?page=N` 过滤。每段返回 `{id, page, layout_label, source, target?, geometry?{layout_box, rendered_box, src_box, scale, font_scale, ...}, layout_status}`。
   - `GET /api/v1/documents/{did}/geometry?kind=parse|layout&page=N` — kind=parse 读最新 run 快照 entities（返回时加 `coord_system:"pdf_topleft"`）；kind=layout 读 layout_geometry paragraphs（`coord_system:"pdf_native"`，并附 `page_info` cropbox 供前端换算）。page 过滤；无快照的旧 workdir 返回 parse kind 的 404 语义错误码 `snapshot_unavailable`（不要空数组假成功）。
   - `GET /api/v1/documents/{did}/check` — 组装三源：`{review_verdict, layout_lint, link_audit}` 原样透传（不裁剪字段），加 `available: {review:bool, lint:bool, link:bool}` 标志（旧 workdir 可能缺某项）。
2. `schemas.py` 扩展对应响应模型（只建模已实现字段；不强求全量 pydantic 校验产物内部结构——产物是信任边界内的本地产物，用 dict 字段承载）。
3. `tests/test_serve_documents.py`：fixture 用代码构造最小 workdir（仿 test_serve_store 的 _make_workdir，写最小 run_state/anchors/translated.jsonl/layout_geometry），覆盖：正常路径、缺产物降级（缺 translated.jsonl 时 target=null 而非报错）、数量不一致 join、page 过滤、coord_system 标注、kind=parse 无快照 404、stage-state 的 manifest fallback。**不读 tmp/ccs3764-dyn 做测试**（保持测试自包含），但实现时可用它人工核对一次并把结果写进报告。
4. OpenAPI 中新端点自动出现（FastAPI 路由声明即可）；`test_openapi_has_no_write_routes` 仍须通过（全是 GET）。

## Constraints
- 只新增/修改：`babeldoc_tools/serve/`（routers 拆分 + schemas）、`tests/test_serve_documents.py`、必要时 `docs/frontend/api.md` 的**已实现标记**更新（把 §3.1 表格中 W02 六行改为"已实现"，不得改契约字段含义）。
- 禁止修改：translate/run/layout/parse/debug_* 核心模块、W01 的 store 安全边界、CLI。
- 不实现：events/SSE/artifacts 下载（W03）、jobs/上传（W07/W08）、草稿（W09）。不为它们加路由。
- 大文件读取注意：document.md 可能数百 KB，paragraphs 端点不要把整篇 markdown 拼进响应（source 字段按需：默认返回 canonical 文本，markdown 全文留给后续 artifacts 端点）。
- 读取产物时容忍缺失/损坏 JSON（read_json default 语义），不因单个产物损坏 500 整个端点；损坏时该字段 null + `available` 标志。
- 查找文件用 rg 限定路径 + timeout，禁止全仓递归 grep。
- 不委派、不 commit/push、不改 .plan 下文档（主控维护）。

## Validation
```
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest tests/test_serve_documents.py tests/test_serve_app.py tests/test_serve_store.py tests/test_single_entry.py -q --basetemp="tmp/pytest-W02-$(date +%Y%m%d-%H%M%S)"
.venv/bin/ruff check babeldoc_tools/serve tests/test_serve_documents.py
git diff --check
```
另做一次真实核对：启动 `bdt serve --root tmp`，curl 六个端点（用 ccs3764-dyn 的 did），确认非 200 的只有预期缺失项；结果（含响应截断样本）写进报告。启动冒烟 stdout 必须只有一行 banner JSON（W01 已修，勿回归）。

## Report back
报告含：改动文件清单、六端点在真实 workdir 上的 curl 摘要（每端点 ≤3 行样本）、数量不一致 join 的实测行为、发现的产物形状偏差（若有，与 Context 描述不符之处）、验证命令输出摘要、遗留风险。绑定 output 路径回传。最多两轮修复；产物形状与 Context 严重不符时停下来报告，不要自行改契约。
