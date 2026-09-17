# web/ · iee Translater 前端

Vite + React 18 + TypeScript + Tailwind + TanStack Query + Zustand。
视觉规范唯一事实来源：`docs/frontend/design-v2/DESIGN.md`（设计令牌照抄在
`tailwind.config.ts` + `src/app/globals.css`）；HTTP 契约唯一事实来源：
`docs/frontend/api.md` 与运行中服务的 `/openapi.json`。

本目录当前范围（W08）：三栏工作台壳 + 设计令牌 + hash 路由 +
**PDF 预览**（pdf.js 渲染产物 PDF、parse/layout 两套 bbox 叠加、源/译/对照三模式、点框选中）+
**进度层**（事件流面板 + 真 SSE 增量 + 阶段时间线真耗时 + 运行中状态）+
**上传与任务**（文件库拖/选上传 PDF → 新文档、工作台「开始翻译」配置卡、运行中/失败卡与取消）。
**不做**：连续滚动、缩放控件、bbox 拖拽（W09）、译文覆盖层、段落属性面板（W10）、
profile 编辑器（W08 只有 GET /profiles 的下拉，写入接口在后端）；这些区域渲染带 `data-od-id`
的占位并写明接入任务。

## 开发工作流（两个终端）

```bash
# 终端 A：后端（真实数据；--root 下每个子目录 = 一个文档 workdir）
PATH="$PWD/.venv/bin:$PATH" bdt serve --root tmp --port 8787
#   若 macOS 把 editable .pth 标了 hidden 导致 import 失败：
#   chflags nohidden .venv/lib/python3.12/site-packages/_editable_impl_babeldoc_agent.pth

# 终端 B：前端 dev server（/api 由 Vite 代理到 127.0.0.1:8787；dev 前会同步 pdf.js 静态资源）
cd web && pnpm install && pnpm dev      # http://localhost:5173/#/library
#   PDF 预览：http://localhost:5173/#/d/<did>/progress（<did> 是 tmp/ 下的 workdir 名）
```

代理端口默认 8787，可用 `BDT_SERVE_PORT=<port> pnpm dev` 覆盖（`vite.config.ts`）。
serve 不注册 CORS，所以前端必须走这个同源代理（`docs/frontend/api.md` §1）。

## 命令

| 命令 | 作用 |
|---|---|
| `pnpm dev` | dev server（含 /api 代理）；先同步 pdf.js 静态资源 |
| `pnpm build` | 同步 pdf.js 静态资源 + `tsc --noEmit` + `vite build` → `web/dist`（W15 才接到 `babeldoc_tools/web_dist/`） |
| `pnpm typecheck` | `tsc --noEmit` |
| `pnpm lint` | eslint flat config + typescript-eslint，`--max-warnings 0` |
| `pnpm test` | Vitest（jsdom + @testing-library/react），不起真后端 |
| `pnpm e2e` | Playwright 真浏览器用例（`e2e/preview.spec.ts`）：起真 serve + 真 dev server |
| `pnpm sync:pdfjs` | 单独把 pdf.js worker/cmaps/standard_fonts 复制进 `public/pdfjs/` |
| `pnpm gen:api` | 从运行中的 serve 拉 `/openapi.json` 生成 `src/api/schema.d.ts` |

`pnpm e2e` 首次运行需要 `pnpm exec playwright install chromium`。

## 目录

```
web/
  src/
    api/        schema.d.ts（openapi-typescript 生成）+ types.ts（具名别名）
    app/        App.tsx（hash 路由分发）+ globals.css（设计令牌 CSS 变量）
    components/ icons.tsx · ui/（Button/Chip/StatusBadge/ScrollArea/ErrorCard/Tooltip）
                shell/（Topbar/IconRail/ScreenFrame/Gutter/ViewRail/InspectorPanel/Timeline）
                events/（EventStreamPanel · EventRow · useEventWindow · useEventStream · useTimelineStages）
                preview/（PdfCanvas · BboxLayer · PreviewToolbar · PreviewArea）
    lib/        api.ts（/api/v1 + 错误信封 + POST/PUT/multipart）· queries.ts（含 W08 上传/job/profiles）
                uploads.ts（客户端预检：魔数 + 200MB）· jobs.ts（活动判据/轮询/from 判断/页码形状）
                preview.ts（产物选择 + geometry 解析）· events.ts（SSE 帧/URL/live/分组/窗口合并）
                timeline.ts（阶段合并 + 条宽）· pdf.ts（pdf.js worker/cmap/字体配置）
                humanize.ts · routing.ts · cn.ts
    components/jobs/  StartJobCard（开始翻译配置卡）· ActiveJobCard（运行中/失败/取消卡）
    screens/    LibraryScreen / DocumentCard / WorkbenchScreen / PlaceholderScreen
    stores/     ui.ts（三栏宽度 + 分隔条 + 屏/预览模式 + 预览页码/bbox 图层/选中段落）
  scripts/      sync-pdfjs-assets.mjs（把 pdf.js 静态资源复制进 public/pdfjs/）
  e2e/          Playwright 真浏览器用例（preview.spec.ts · progress.spec.ts · upload.spec.ts）
                fixtures/sample.pdf（602 字节最小合法 PDF）· fixtures/sleep-translator.sh（长睡 stub）
  tests/        Vitest 用例（api / store / routing / 文件库屏+上传 / job 卡与 hooks / 工作台壳 /
                预览坐标与组件 / 事件流与时间线）
  public/pdfjs/ pdf.js worker + cmaps + standard_fonts（生成物，.gitignore，不入库）
  tmp-smoke/    本地冒烟截图与日志（.gitignore，不入库）
```

## PDF 预览（W05）

- **pdf.js 本地打包，禁止 CDN**：`scripts/sync-pdfjs-assets.mjs` 从 `node_modules/pdfjs-dist`
  复制 `pdf.worker.min.mjs` / `cmaps/` / `standard_fonts/` 到 `public/pdfjs/`（该目录在
  `.gitignore`，由 `pnpm dev` / `pnpm build` / `pnpm e2e` 自动同步；升级 pdfjs-dist 后靠
  版本戳 `.pdfjs-dist-version` 强制刷新）。`src/lib/pdf.ts` 是唯一配置点，worker 走
  `GlobalWorkerOptions.workerSrc`（同源）。
- **Range 而不是整文件下载**：预览 URL 是 `/api/v1/documents/{did}/artifacts/{name}`
  （`Accept-Ranges: bytes`）。pdf.js 自己发 `Range` 请求按需取字节——不要先 `fetch` 成 blob。
  pdf.js 用 `/^https?:/i.test(url)` 判断是否走 Range，所以 `loadPdfDocument` 会先把 URL
  解析成绝对地址（相对路径会被判成非 HTTP，从而整文件流式下载）。65MB mono 实测首页只取到
  ~0.6MB（见 `e2e/preview.spec.ts` 第二个用例）。
- **两套坐标系在前端换算**（契约红线：服务端不转换）：`pdfToScreen(box, viewport, coordSystem,
  cropbox?)`（`src/components/preview/BboxLayer.tsx`）先把输入坐标还原成 PDF user space
  （`pdf_topleft`：`y_up = cropbox.y1 - y`；`pdf_native`：`y_up = cropbox.y0 + y`），再交给
  pdf.js viewport 的 `convertToViewportRectangle`（含 scale/rotation/viewBox 偏移），**不假设
  「PDF 点 == CSS px」**。适宽 scale 与 bbox 层共用同一个 viewport（`PdfCanvas` 报回 scale=1 的
  viewport，父级 `clone({scale})`），所以叠加层与 canvas 像素对齐。
- **产物选择**：mono → dual → 其它 `output/*.pdf`；都没有 → 「无产物 PDF」占位卡（读
  `GET /artifacts` 判断）。原文模式需要 `source.pdf`（kind=source，上传时写入）——没有就禁用
  按钮并给 tooltip；对照模式左侧同样给出明确说明，不拿译文产物冒充原文。
- 视图默认 bbox 图层：progress/layout/check = 识别框（parse），translate = 版面框（layout）；
  用户手动切换后持久化 `ieet.bboxMode`。geometry 404（产物缺失）只显示「该页无解析数据」小条，
  预览照常可用。

### e2e（真浏览器 + 真 serve + 真产物）

```bash
cd web && pnpm e2e        # 自动起 bdt serve --root ../tmp --port 8793 + vite --port 5175
```

- fixture 用本仓库 `tmp/` 下的真实 workdir：主用例读 `tmp/ccs3764-dyn`（21 页、parse/layout 各
  420 段、事件归档 12 条、7 段全 ok），缺失时用例会失败并提示；重产物用例读
  `tmp/e2e-2602-02908v2-20260917`（65MB mono / 84MB dual），fixture 不在时自动 skip。
- 断言：canvas 真有文字像素、bbox 数量 == 服务端实体数、框内有文字像素、parse/layout 两套换算
  落到同一矩形（±0.6px）、点框选中联动右侧面板、切换模式不重建译侧画布、产物响应含 `206`、
  全程无外网请求（禁 CDN）；`progress.spec.ts` 再断言事件面板行数 == 服务端本页条数、最新在上、
  SSE 连上（`data-status=open`）、时间线 7 段 `data-state=ok` 且耗时/总用时与 `stage-state` 同口径、
  无 console error；`upload.spec.ts` 覆盖 W08 的上传→开始→取消（见下）。
- **W08 `upload.spec.ts` 的副作用**（都在 `tmp/`，本仓库不入库）：
  - 上传用仓库里的 fixture `web/e2e/fixtures/sample.pdf`（602 字节、正确 xref 的最小合法 PDF），
    每次跑都会在 `tmp/` 下留一个 `up-sample-<时间戳>` 文档（证据，可手工删）；
  - 取消用例每次用新 did `tmp/w08-e2e-cancel-<时间戳>/`（`job` 历史按 did 存在
    `.bdt-serve/jobs/*.json`，固定 did 会让第二次跑先看到上一次的 canceled job）；
  - 预置 `sleep-t` profile 指向 `web/e2e/fixtures/sleep-translator.sh`（长睡 stub）：
    写入 `tmp/.bdt-serve/profiles.json` 前会**备份**，`afterAll` 还原（原本不存在就删掉）；
  - `afterAll` 取消活动 job + `bdt debug --stop` 停查看器，不留 running 子进程/端口。
- `from=parse` 的用例**诚实断言两种环境分支**：有 MinerU token（本机默认）→ 断言 job 进 `running`；
  没有 token/MinerU 不可用 → 断言 `failed` 且 `error_code` 非空。分支写进 annotation（`[w08]`）。
- 截图落在 `web/tmp-smoke/`（`e2e-preview.png` / `-layout` / `-compare` / `-selected` /
  `-heavy-range` / `e2e-progress*.png`），性能数字以 `[perf]` / `[range]` 打到 stdout。

## 进度层（W06）：事件流 + 时间线 + SSE

### 事件流面板（右侧面板「进度」tab）

- **尾部窗口**：默认只渲染最近 **200** 条（§4.6；3758 条的 run 全量渲染 DOM 会卡）。窗口是
  `lib/events.ts` 的纯函数合并出来的：首拉窗口（query） + SSE 增量 + 「载入更早」页，按 `seq` 去重、
  升序存储、显示时反转成**最新在上**。
- **首拉策略**：分页接口只能正向翻（只有 `after_seq`，没有 before/desc 参数），所以拿“尾部”
  必须从 0 扫到 `has_more=false`：`fetchEventTail` 循环 `?limit=2000`（服务端上限）并只留尾部 200。
  实测：12 条的 run = **1 次**；3758 条的 run = **2 次**（`after_seq=0` + `after_seq=2000`）。
  单 run 超过 `EVENTS_TAIL_MAX_PAGES × 2000` 条时窗口不是真正的尾部（已知边界，实跑未见）。
- **「载入更早」每次 500 条**：游标 = `最旧 seq - 1 - 2000`，一次请求扫回来后只留 `seq < 最旧 seq`
  的尾 500 条并入，窗口上限放宽到 `200 + 已载入条数`（不奉承已载入的历史）。
- **kind 分组过滤**（全部/阶段/调用/缓存/候选/编译/其他）**只影响显示**，不动游标、不改窗口。
- 点行展开完整 JSON（`<pre>` 最高 220px，§4.6）；`seq` 右对齐、时间戳是归档里的 UTC 时刻（`title`
  给完整 ISO）。

### SSE（原生 `EventSource`，禁库）

- 地址：`/api/v1/documents/{did}/events/stream?run_id=<run>&after_seq=<尾部扫过的最大 seq>`，
  **固定带 run_id**（换 run 必须换它，否则续传游标指向另一个 run）；相对路径走同源代理。
- `event: <kind>` 是**命名分发**，`onmessage` 只收默认类型 → `useEventStream` 会为
  `humanize.EVENT_KINDS` 里的每个 kind 单独 `addEventListener`（**新 kind 必须补表**，否则收不到
  那个 kind 的事件）。
- 断线：`onerror` + `readyState` 区分「连接断开，重试中」（CONNECTING，浏览器自己重连并带
  `Last-Event-ID`）与「连接已关闭」（CLOSED）；不写 backoff。心跳 `: ping` 是注释行，天然忽略。
- `404 events_unavailable`（没有 run 归档）**不是 SSE 帧**：先用分页接口首拉，归档不存在就不建
  EventSource，面板显示空态；真错误（500/网络）显示错误文案 + 重试。

### 时间线（stage-state 基线 + 事件 live 段）

- 基线是 `GET /stage-state`（真实测 `duration_s`）；事件流只用来给**基线还没定论**的阶段算
  「已进行 Xs」（本地时钟差值，1s ticker；非 live 时不跑）。条宽 = 该段耗时 / 最长已完成段
  （同一线性标尺，§8.3），未开始段固定 64px 虚线不参与比例，0s/刚起步给 2% 最小可见宽。
- 点击整列 → 跳该阶段对应视图（映射表 `lib/timeline.ts::STAGE_VIEWS`，注释里写了理由：
  parse→识别、translate/apply→翻译、build/report→进度、check/review→检查）。
- **`isRunLive(events)` 的规则与局限**：末条不是 `stage_finished`，或它的 stage 不是 `report` → live。
  局限：归档被截断的 run（legacy replay 只到 `check`，而 `run_state` 里 `review`/`report` 有真耗时）
  会被误报；失败/中断的 run（末条 `stage_error`）也算 live。因此**徽标与轮询的「运行中」一律用
  基线裁决后的 `hasLiveSegment(segments)`**（`ok`/`failed` 的基线永远赢），`isRunLive` 只用来判定
  "事件流是否还在增长"（stage-state 的 2s 轮询）。
- 自动刷新：`useDocument` 只在时间线出现 live 段时 2s 轮询；`useStageState` 在事件流说 live 时 2s；
  列表页 `useDocuments` 在有任一 `running` 阶段时 3s（`hasRunningDocument`）。
- 顶栏/视图栏徽标：时间线判定 live → 「翻译中」+ 脉冲；否则用 `stage_summary` 的
  「已完成 / 进行中 / 失败 / 未运行」。全站唯一动效仍然只加在真运行中的元素上。

### 手工冒烟（SSE 实时性 + 首拉请求数 + 截图）

```bash
# 手工起后端 + 前端（8800/5173 任选）
PATH="$PWD/.venv/bin:$PATH" bdt serve --root tmp --port 8787
cd web && BDT_SERVE_PORT=8787 pnpm dev --host 127.0.0.1 --port 5173

cd web && node tmp-smoke/w06-sse-smoke.mjs   # 真 Chromium，跑完写 tmp-smoke/w06-sse-smoke.txt
```

脚本会向 `tmp/ccs3764-dyn` 最新 run 的 `events.jsonl` **追加一行**合法事件（模拟进行中的 run），
量浏览器收到它的延迟，结束前按备份**还原 fixture**。W06 实测：追加 → 浏览器 534ms 收到（<1s），
首屏到第一行事件 113ms，3758 条的 run 窗口 200 行 / 首屏 117ms / 2 次分页请求，无 console error。
SSE 增量的 e2e 留到 W15（W07 有真 job 之后）。

## 上传与任务（W08）：拖 PDF → 开始翻译 → 取消

### 上传（文件库）

- 入口：文件库的拖放区 + 「上传 PDF」按钮（同一个隐藏 `<input type=file multiple>`）。
  多选时**逐个串行** POST（`POST /api/v1/documents`，multipart，body 只有 `file` 字段）；
  不伪造百分比，只有「正在上传 <文件名>…」一行（沿用全站唯一动效 `pulse-dot`，不引第二个 spinner）。
- 客户端预检（`lib/uploads.ts`，与服务端同一套边界）：`file.size > 200MB` → 「文件过大」，
  非 `.pdf` 扩展名 → 「只接受 .pdf」，再读前 5 字节确认 `%PDF-`（`file.slice(0,5)`，不整文件进内存）。
  服务端仍会独立复核（`413 file_too_large` / `422 invalid_pdf`），前端预检只是省一次往返。
- **did 由服务端生成**（`up-<slug>-<UTC 时间戳>`，同名冲突递增 `-2`/`-3`）——客户端不提供路径、
  目录名或命令；上传只落 `<did>/source.pdf`（不建 `agent/` 骨架）。成功后列表自动刷新，
  **不自动跳转**：用户自己点卡片进工作台（预览的「原文」模式立刻可用，因为 `source.pdf` 是
  W03 白名单里的 `kind=source`）。
- `bdt serve --workdir <dir>`（只公开一个 workdir）**不支持上传**：`409 upload_not_supported`
  （旁边建的 did 进不了可见范围，宁可不做）。

### 开始翻译（工作台进度视图顶部）

- 显示条件：该文档**没有活动 job**，且清单里已有产物（`source.pdf` 或任何 `agent/*`）。
- 表单：profile 下拉（`GET /profiles`，只见 `id`/`label`）、页码范围（占位 `1-3,5 全部留空`）、
  dual 开关、高级折叠里的「起点阶段」；`from` 默认值 = **有 parse 产物 → translate，否则 parse**
  （判据 `stage_summary.parse === 'ok'` 或 `available.anchors`/`parse_snapshot`）。
- 提交 `POST /documents/{did}/jobs`，body 只有 `action`/`from`/`pages`/`dual`/`profile`
  —— translator/reviewer 命令由**服务端**从 profile 解析，前端连字段都没有。
- **MinerU token 只走 serve 进程环境**（`MINERU_API_TOKEN`）：`from=parse` 时卡片会提示
  「首次翻译需要 MinerU（由服务环境提供 token），耗时较长」，但前端**无法**预知有没有 token，
  所以任务是否成功以服务端返回的 `error_code` 为准（不假装成功）。

### 运行中 / 取消 / 重试

- job 状态轮询 `GET /documents/{did}/jobs`：有 `queued`/`running` 时 2s，否则 30s
  （`lib/jobs.ts`）。它是「run 归档出现之前」唯一的真实信号；run 一出现，事件流/时间线照旧
  由 SSE + `stage-state` 负责 —— 两路并存，任一 live 就快轮询详情/阶段。
- 有活动 job → 顶部换 `ActiveJobCard`：状态徽标（排队中/运行中 · 阶段）+ 「取消」（`window.confirm`
  后 `POST /jobs/{jid}/cancel`，服务端杀整个进程组）。
- 失败/取消/中断 → 同一张卡如实显示 `error_code`/`error_message` + 「重试」；**重试 = 同参数发一个
  新 job**（后端不自动重跑收费调用）；`interrupted` 明确写「服务曾重启，请重试」。
- **信封脱敏**：`GET /jobs/{jid}` 拿到的 `envelope` 里 `data.config.translator/reviewer` 已是
  `<profile:<id>>`、带 token 的 `data.debug.url` 已移除（后端落盘前处理）。

### profiles（谁配命令）

`<store_base>/.bdt-serve/profiles.json`（`--root` 模式 = 服务根目录下的 `.bdt-serve/`）。
写入接口 `PUT /api/v1/profiles` **只接受脚本路径引用** `scripts/<name>`（必须落在
`<store_base>/scripts/` 或仓库 `scripts/` 白名单目录内，越界/符号链接穿越/不存在一律 422），
服务端解析成**绝对路径**再写盘 —— 客户端永远无法塞命令字符串或密钥（`translator`/`api_key`
这类字段收到即 `422 forbidden_field`）。前端目前只用 `GET /profiles` 填下拉，不做 profile 编辑器。

## 设计与契约约束（改动时别忘）

- 令牌只从 `tailwind.config.ts` / `src/app/globals.css` 取，禁止新增灰阶或第二个强调色；
  `--accent` 每屏可见使用 ≤ 2 处（DESIGN.md §1.2）。
- 工作台栅格由 CSS 变量驱动：`--vrw`（220，160–320）/ `--inspw`（360，280–560）/
  `--tlh`（96，72–160），持久化键 `ieet.vrw` / `ieet.inspw` / `ieet.tlh` /
  `ieet.inspCollapsed` / `ieet.screen`（§8.2）+ `ieet.bboxMode`（W05：预览 bbox 图层三态）。
  预览页码（`previewPage`）与选中段落（`selectedParagraphId`）只活在会话里，不持久化。
- 所有区块带 `data-od-id`（kebab-case，§7.9）；占位区同样带，供后续任务定位替换点。
- 计数为 `null` 表示产物缺失，显示 `—`，不要当 0（`docs/frontend/api.md` §3.1）。
- 预览只叠加**已实现**的图层：bbox 的 parse/layout 换算、geometry 的 404 降级、产物缺失占位；
  不造假数据、不引外网依赖（worker/cmap/字形全本地）。
- 事件流用**原生 `EventSource`**（禁库、禁 fetch-stream）：SSE 的 `event: <kind>` 是命名分发，
  kind 清单在 `humanize.EVENT_KINDS`；面板只渲染窗口（默认 200 + 已载入），不做虚拟化。
- 事件不带 level（api.md §4），级别只由 kind + `data.status`/`returncode`/`error_code` 推导
  （`events.ts::eventLevel`）；`data` 原样留在内存，面板只显示摘要。
- 时间线以 `stage-state` 为**基线**（有真实 `duration_s`），事件只补 live 段的本地秒表；
  不编造百分比 / ETA。
- 全站唯一动效是 running 圆点脉冲（`.pulse-dot`），只加在真实 `running` 或时间线 live 段上。
