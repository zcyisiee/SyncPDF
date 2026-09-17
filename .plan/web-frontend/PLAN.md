# Web 前端接入后端 · 任务规划（feat/web-frontend）

> 设计稿已完成（`docs/frontend/design-v1|v2/`）。本文回答：**后端逻辑怎么接、按什么顺序、每步怎么验收**。
> 进度追踪表见 §7，每完成一格打 `x`。

---

## 1. 结论：怎么接

**后端不需要重写业务逻辑，只需要"读现有产物 + 包一层 HTTP"。**
管线 `parse → translate → apply → build → check → review → report` 已经把一切写进磁盘：

- 每个阶段结束后原子写 JSON 产物到 `<workdir>/agent/`
- 全过程事件流式追加到 `<workdir>/debug/runs/<run_id>/events.jsonl`（**seq 单调递增**，已有 `read_events(after_seq)`）

所以接入 = ① 读文件 → ② 变 JSON → ③ 推 SSE。真正的新逻辑只有两块：**任务编排（子进程 + 取消）** 和 **草稿/编译**。

### 三条架构决策

| # | 决策 | 理由 |
|---|---|---|
| **A1** | `bdt serve` 作为**第 9 个子命令**接入（不是并行入口） | 守卫测试 `tests/test_single_entry.py::test_cli_help_lists_eight_subcommands` 管这件事；该测试需同步改名/加 `serve` |
| **A2** | 任务在**子进程**里跑：`python -m babeldoc_tools.serve.worker --job <job.json>`，直调 `run.run_pipeline` 等 Python 函数（不 shell out 到 `bdt`） | 取消 = 杀进程（管线里是分钟级模型调用，协作式取消做不到）；崩溃隔离；内存可回收；避免 GIL 争用 |
| **A3** | **`events.jsonl` 就是进度通道**，不新造 IPC | 已有 seq 单调 + 原子写 + 断点续读；server 只需 tail 文件 → SSE。前端断线用 `Last-Event-ID` 续传 |

### 关键推论

- **A2 + A3 合起来 = 任务进度天然可恢复**：server 重启不丢进度，因为进度在磁盘上，不在内存里。
- Web 层**不需要**改 `run.py` / `translate.py` 的核心逻辑（M6 流式进度除外）。
- `agent/run_state.json`（阶段状态）+ `debug/runs/<id>/manifest.json`（**每阶段真实 started_at/finished_at**）= 底部时间线的真数据源，不用自己计时。

---

## 2. 后端现状盘点（前端要什么 → 已有什么）

| 前端需求 | 已有产物 | 形状 | 缺口 |
|---|---|---|---|
| 预览 bbox（译文） | `agent/layout_geometry.json` | `paragraphs[]`: `id/page/layout_label/src_box/layout_box/rendered_box/scale/font_scale/n_lines/text/n_chars` + `page_info[]`: `cropbox` | 无（只差坐标换算） |
| 预览 bbox（原文/识别） | `debug/runs/<id>/snapshots/parse/paragraphs.json` | `{version, coord_system, entities[], relations[]}` | `entities[]` 字段待确认（M1 第一步探明） |
| 段落面板：原文/译文 | `agent/document.md` + `agent/anchors.json` + `agent/translated.jsonl` | jsonl: `{"id":"P01-003","target":"…"}` | 需与 geometry 按 `id` join |
| 排版参数回显 | `layout_geometry.json` 的 `overrides` / `font_scale` / `scale` / `optimal_scale` | — | 无 |
| 进度条 / 阶段时间线 | `agent/run_state.json` + `debug/runs/<id>/manifest.json` | `stages.{parse,translate,…}.{status,started_at,finished_at,events}` | 无 |
| 事件流 | `debug/runs/<id>/events.jsonl` | `{seq, at, stage, kind, data}`；**单 run 实测 3758 条**，40+ kind | ⚠️ 需要 kind→人话映射表；需要服务端过滤+分页（不能全推给前端） |
| 检查视图 | `agent/review_verdict.json`（`verdict/blockers[]/warnings[]{code,sev,id,page,count,samples,hint}`）+ `layout_lint.json` + `link_audit.json` | — | 三组分类（结构审查/排版 lint/链接审计）需组装 |
| 归档/下载 | `output/*.mono.pdf` / `*.dual.pdf` / `agent/translated.md` / `FINAL_REPORT.md` | — | 需 Range 支持（pdf.js 要求） |
| 词表 | `babeldoc/glossary.py` | — | CRUD + 命中计数（数 `document.md`） |
| 文档元信息 | `agent/run_state.json` 的 `pdf` / `config` / `quality` / `inputs` | — | 页数/段数/模型需组装 |

**结论：M1 的 80% 工作量是"读文件 + 拼 JSON + 缓存失效"，不是业务逻辑。**

---

## 3. 存储布局（v1 决策）

沿用 workdir 事实，**不做 projects 层**：

```
<bdt serve --root 指定的目录>/
  <did>/                       # 一个 workdir = 一个文档
    agent/                     # 阶段产物（已存在）
    output/                    # mono/dual PDF
    debug/runs/<run_id>/       # 事件 + 快照（已存在）
    source.pdf                 # 上传时写入
    draft.json                 # 新增：草稿
    candidates.json            # 新增：AI 重译候选
    versions/v1/...            # 新增：版本快照
```

- `did` 用目录名（如 `ccs3764-dyn`）或 ULID；URL 保留 `/documents/{did}/…`，**多用户时再加 `/projects/{pid}/` 前缀即可**，不用改前端路由结构。
- `bdt serve --workdir <dir>` = `--root <dir 的父目录>` + 只服务该文档的语法糖。
- 上传落 `<root>/<新 did>/source.pdf`。

---

## 4. 里程碑

### M0 · 契约冻结（0.5 天）

**目标**：接口形状定死，前后端可并行开工。

**产出**
- `docs/frontend/api.md`：全部端点的请求/响应形状（含错误码、分页、SSE 事件）
- `pyproject.toml` 加 `[project.optional-dependencies] web = ["fastapi>=0.115", "uvicorn[standard]>=0.32"]`
- `web/src/api/schema.d.ts` 生成链路（`openapi-typescript`，从运行中的 `bdt serve` 拉 OpenAPI）

**验收**：`bdt serve --help` 打印端点清单；`docs/frontend/api.md` 覆盖 §2 表格全部行。

**风险**：无。

---

### M1 · `bdt serve` 只读骨架（3–4 天）

**目标**：`bdt serve` 指向一个真实 workdir，前端能拿到全部只读数据。

**产出**
```
babeldoc_tools/serve/
  __init__.py  app.py  cli.py  worker.py
  store.py        # workdir 解析 / mtime 缓存 / 路径白名单（防目录穿越）
  schemas.py      # pydantic 响应模型
  routers/
    documents.py  geometry.py  paragraphs.py
    events.py     artifacts.py  check.py  providers.py
```
端点（全部 GET，只读）：
```
GET /api/v1/health
GET /api/v1/documents                              文档列表
GET /api/v1/documents/{did}                        元信息
GET /api/v1/documents/{did}/stage-state            阶段状态 + 真实耗时
GET /api/v1/documents/{did}/geometry?kind=parse|layout&page=N
GET /api/v1/documents/{did}/paragraphs?page=N      含原文/译文/排版参数（join 后）
GET /api/v1/documents/{did}/events?after_seq&stage&level&limit
GET /api/v1/documents/{did}/events/stream?after_seq   SSE（Last-Event-ID 续传）
GET /api/v1/documents/{did}/artifacts
GET /api/v1/documents/{did}/artifacts/{name}       支持 Range
GET /api/v1/documents/{did}/check
```
外加：`bdt serve` 子命令（`--root/--workdir/--host/--port/--open`）；同步更新 `tests/test_single_entry.py` 的子命令断言（8 → 9，测试改名）。

**验收**
- `pytest tests/test_serve_*.py`：路由形状、目录穿越被拒、Range 正确、事件分页无重复/无丢失
- 守卫测试全过：`pytest tests/test_single_entry.py`
- 手动：`bdt serve --workdir tmp/ccs3764-dyn` → 每个端点 curl 一遍；SSE 在 `bdt run` 期间实时出事件
- `bdt debug` 仍工作（回归）

**风险**：低。唯一未知是 `snapshots/parse/paragraphs.json` 的 `entities[]` 字段（开工第一步探明）。

---

### M2 · Web 脚手架 + 三栏壳 + 真数据预览（4–5 天）  ⚡ 可与 M1 并行

**目标**：`bdt serve` 打开浏览器，看到**真 PDF + 真 bbox + 真事件流 + 真时间线**。

**产出**
- `web/`：pnpm + Vite + React 18 + TS + Tailwind + shadcn/ui + TanStack Query + Zustand + pdf.js
- design-v2 token → `tailwind.config.ts` + `src/app/globals.css`（照抄 DESIGN.md §1/§2/§6）
- `web/src/`：`app/`、`api/`（生成类型 + 查询 hooks）、`components/{shell,preview,events,timeline,panel}`、`stores/`、`lib/{coord,humanize}`
- 三栏壳 + 底部时间线（组件化复刻 v2，栏宽拖拽 + localStorage）
- pdf.js 渲染（走 M1 artifacts Range）+ bbox 叠加（走 M1 geometry）
- 预览三模式：原文 / 译文 / 对照（跨侧同 id 高亮）
- 事件流：真数据 + 40 个 kind 的人话映射 + 阶段/级别过滤 + 原始 JSON 展开；**虚拟列表**（单 run 3758 条）
- 时间线：`manifest.json` 真实耗时比例 + 阶段点击过滤
- `bdt serve` 托管 `babeldoc_tools/web_dist/`（`pnpm build` 产物，入 git）

**验收**
- `pnpm test`（Vitest）：坐标换算（PDF pt ↔ 屏幕 px，含 cropbox/rotation/zoom）、humanize 映射全覆盖、store
- `pnpm exec tsc --noEmit` + eslint 干净
- `pnpm exec playwright test`：指向 `tmp/` 真实 workdir，断言 PDF 页已渲染、bbox 数 == 期望、事件行数 > 0；截图落 `tmp/`
- **人工**：与 `docs/frontend/design-v2/index.html` 并排比对，视觉一致

**风险**
- ⚠️ **坐标系**：PDF cropbox 原点在左下、屏幕在左上，加 rotation/zoom 易错 → 单测覆盖
- ⚠️ **事件量**：3758 条 → 虚拟列表 + 服务端过滤（M1 已预留 `limit`）
- pdf.js worker 在 Vite 下的路径配置

---

### M3 · 任务编排（run/cancel）+ 上传 + 配置（4–5 天）

**目标**：从前端完整跑一次真翻译，事件流实时增长，能取消。

**产出**
- `POST /api/v1/documents`（multipart 上传 PDF）→ 建 did
- `POST /api/v1/documents/{did}/jobs` `{action: run|translate|apply|build|check, from, config}` → `{job_id}`
- `GET /api/v1/jobs/{jid}` / `POST /api/v1/jobs/{jid}/cancel`
- 队列：**单文档同时 1 个任务**（冲突 409）；跨文档并发；全局上限可配
- `serve/worker.py`：子进程入口，`start_new_session=True` 建**进程组**；取消 = `SIGTERM` 进程组 → 超时 `SIGKILL`（**必须连带杀掉 translator 孙进程**）
- server 侧 tail `events.jsonl` → SSE 推 `job_queued/started/finished/canceled` 合成事件
- provider profiles：`~/.config/bdt/providers.toml` 读写，前端只见名字
- 前端：文件库屏（列表/上传/拖放）、配置弹层（模型/词表/页范围/dual）、进度屏接真数据、取消按钮

**验收**
- `pytest`：job 生命周期、409 冲突、**取消后进程真的没了**（`ps` 断言无残留 translator）、server 重启后能读到进行中任务的进度
- Playwright：上传 → 配置 → 开始 → 事件流增长 → 取消 → 状态 canceled
- 真实跑一次：留证据在 `tmp/`，截图 + 事件条数 + 总耗时

**风险**：⚠️⚠️ **取消语义是全局最易错点**（孙进程逃逸 → 僵尸模型调用烧钱）。先写取消的测试，再写实现。

---

### M4 · 草稿 + 自动编译 + bbox 拖拽（4–6 天，含 1 天 spike）

**目标**：改译文 → 预览更新；拖 bbox 改版面。

**前置 spike（先做，1 天）**
`layout.build_pdf` 能否**只重排指定页**？产出 `.plan/web-frontend/SPIKE-page-rebuild.md`。
- 可行 → 页级快速重排 + 「预览」角标（Q20）
- 不可行 → **降级为全量 build**，前端仍标「预览」+ 提示「已升级为全量编译」；页级增量挪到 v1.2

**产出**
- `draft.json` 读写：`{paragraphs:{P05-002:{target, layout:{scale_cap,font_scale,line_skip,box_scale,box}}}, updated_at}`
- `GET/PATCH/DELETE /documents/{did}/draft`
- `POST /jobs {action: compile, scope: page|full, pages:[…]}`；溢出检测 → 自动升级全量 + 前端提示
- 前端：段落面板编辑（占位符校验）、防抖自动编译、改动计数、预览角标、bbox 拖拽 → `layout-set`

**验收**
- 改 1 段 → 预览更新且版本号不变；`ir_overrides.box_changed` 正确递增
- 拖 bbox → 重排后 `rendered_box` 变化符合预期
- Playwright：编辑 → 防抖 → 预览角标出现 → 全量编译后可存版本

**风险**：⚠️⚠️ 页级增量重排是**最大未知**（spike 兜底）；bbox 拖拽单位换算。

---

### M5 · 候选重译 + 版本 + 归档（3–4 天）

**目标**：AI 重译候选、版本管理与归档。

**产出**
- `POST /jobs {action: retranslate, paragraph_ids, feedback, provider}` → 候选**不入译文**（Q16）
- `candidates.json`；`POST /candidates/{cid}/accept|discard`
- 版本：`POST /documents/{did}/versions`（要求 full build 通过 + check 无 blocker）、`GET /versions`、`POST /versions/{v}/rollback`
- 归档：`POST /documents/{did}/archive`；归档屏版本列表 + 四种产物下载
- 前端：候选卡并排（当前 vs 候选）、采用/丢弃、版本列表

**验收**：pytest（版本创建门禁、回滚正确）+ Playwright 全流程。

**风险**：低（复用 `translate.retranslate_ids`）。

---

### M6 · 词表 + 流式段落级进度（3–5 天）

**目标**：词表 CRUD + 翻译时逐段进度。

**产出**
- 词表 CRUD（封装 `babeldoc/glossary.py`）+ 命中计数（数 `document.md`）+ CSV 导入导出
- 词表注入 translate（对齐 `translate.py` 模板的注入位置/格式）
- 流式：translator wrapper 脚本把 CLI 流式事件拆成纯文本增量写 stdout（**协议不变**，Q33）；`translate.py` 边读边扫段落锚点 → `paragraph_done` 事件（下一个锚点出现 = 上一段完成）
- 前端：词表屏；时间线 translate 段内嵌 `n/N` 实时增长

**验收**
- 真实模型跑一次，事件流里 `paragraph_done` 数量 == 段落数，且时间序单调
- 词表命中数与人工抽检一致

**风险**：⚠️ 需真实模型跑（成本/时间）；wrapper 脚本对不支持流式的 CLI 要优雅降级。

---

## 5. 依赖关系与并行

```
M0 ─┬─→ M1 ─┬─→ M3 ─→ M4 ─→ M5
    │       │
    └───────┴─→ M2（前端可先 mock，M1 完成后切真数据）
                         M6（依赖 M3 的 job 机制）
```

- **关键路径**：M0 → M1 → M3 → M4（→ M6）
- **可并行**：M2 ∥ M1（前端先 mock）；M5 可与 M4 部分并行
- 每个里程碑内部按"先写验收测试/探明未知 → 再实现"

---

## 6. v1 范围建议

| 版本 | 含里程碑 | 交付能力 | 估时 |
|---|---|---|---|
| **v1**（推荐） | M0 + M1 + M2 + M3 | 上传 → 配置 → 翻译 → **实时看进度和事件流** → 预览真 PDF+bbox → 下载产物 | **≈ 12–16 天** |
| v1.1 | + M4 + M5 | 编辑译文 / 拖 bbox / 自动编译 / 版本 / 归档 / 候选重译 | +8–11 天 |
| v1.2 | + M6 | 词表 / 流式段落级进度 | +3–5 天 |

**推荐先做到 v1 就停下来验收**：它是一个完整闭环（能跑、能看、能下载），且不依赖任何未知项（M4 的页级重排、M6 的流式契约都还没验证）。你可以在真实使用几天后再决定 v1.1 的优先级。

---

## 7. 进度追踪

里程碑：

- [ ] **M0** 契约冻结（`docs/frontend/api.md` + `web` extra + 类型生成链路）
- [ ] **M1** `bdt serve` 只读骨架（10 个 GET 端点 + pytest + 守卫测试更新）
- [ ] **M2** `web/` 脚手架 + 三栏壳 + 真数据预览（pdf.js + bbox + 事件流 + 时间线）
- [ ] **M3** 任务 run/cancel + 上传 + 配置弹层（含取消的进程组清理）
- [ ] **M4** 草稿 + 自动编译 + bbox 拖拽（含页级重排 spike）
- [ ] **M5** 候选重译 + 版本 + 归档
- [ ] **M6** 词表 + 流式段落级进度

任务级 brief（每个 brief = 一次可验收的委派）：
- [ ] `.plan/web-frontend/briefs/M1-01-serve-skeleton.md`
- [ ] `.plan/web-frontend/briefs/M1-02-readonly-routers.md`
- [ ] `.plan/web-frontend/briefs/M1-03-events-sse.md`
- [ ] …（每个里程碑开工前拆解）

未知项（开工前必须消掉）：

- [ ] `snapshots/parse/paragraphs.json::entities[]` 字段形状（M1 第一步）
- [ ] 页级增量重排可行性（M4 spike）
- [ ] 取消时 translator 孙进程能否随进程组一起死（M3 先写测试）
- [ ] 词表注入在 `translate.py` 模板里的确切位置/格式（M6）

决策记录：

- [x] 存储不做 projects 层，`did` = workdir 目录名（§3）
- [x] `bdt serve` 第 9 个子命令（A1）
- [x] 任务走子进程直调 Python 函数（A2）
- [x] `events.jsonl` 作为进度通道（A3）
- [ ] v1 范围确认（§6）

---

## 8. 委派与验收流程

每个里程碑拆成 1–3 个 brief，放 `.plan/web-frontend/briefs/<task-id>.md`（`git add -f` 入库），brief 含
`# Task / ## Objective / ## Context / ## Deliverables / ## Constraints / ## Validation / ## Report back`。

- 用 AGENTS.md 里约定的 harness 委派（通常 `pi -p --no-session @brief.md "…"`），一次一个 brief，**只允许 leaf worker，禁止再委派**
- worker 报告成功 ≠ 完成。主控必须：**亲自读 diff** → 跑 `pytest` → 跑 `ruff check` → 手动 curl/截图验证
- 验证产物留 `tmp/`（`--basetemp="tmp/pytest-$(date +%Y%m%d-%H%M%S)"`）；`git diff --check` 干净
- 每个里程碑完成 = 独立中文 commit 在当前分支
