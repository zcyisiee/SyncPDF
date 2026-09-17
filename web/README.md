# web/ · iee Translater 前端

Vite + React 18 + TypeScript + Tailwind + TanStack Query + Zustand。
视觉规范唯一事实来源：`docs/frontend/design-v2/DESIGN.md`（设计令牌照抄在
`tailwind.config.ts` + `src/app/globals.css`）；HTTP 契约唯一事实来源：
`docs/frontend/api.md` 与运行中服务的 `/openapi.json`。

本目录当前范围（W13）：三栏工作台壳 + 设计令牌 + hash 路由 +
**PDF 预览**（pdf.js 渲染产物 PDF、parse/layout 两套 bbox 叠加、源/译/对照三模式、点框选中）+
**进度层**（事件流面板 + 真 SSE 增量 + 阶段时间线真耗时 + 运行中状态）+
**上传与任务**（文件库拖/选上传 PDF → 新文档、工作台「开始翻译」配置卡、运行中/失败卡与取消）+
**编辑闭环**（右侧面板段落 tab：改译文 / 调排版参数、bbox 八手柄拖拽、草稿乐观并发写、
自动/手动编译状态条、按修订号下载与质量徽标）+
**重译候选**（段落面板「AI 重译」→ 候选对比（原文/当前译文/候选译文）→ 采用进草稿 / 拒绝）+
**版本归档**（归档视图 `#/d/:did/archive`：全部历史版本（时间/修订/触发原因/质量状态/大小）+
任一版本下载；右侧面板「归档」tab 给同一份数据的摘要 + 「查看全部」）+
**词表**（`#/glossary` 全局术语表：行内编辑/加删行/整表保存、CSV 导入导出、
「开始翻译」卡上的词表开关、图标栏条数徽标）。
**不做**：连续滚动、缩放控件、译文覆盖层、版本回滚（v1 不做）；
未实现的区域渲染带 `data-od-id` 的占位并写明接入任务。

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
| `pnpm e2e` | Playwright 真浏览器用例（`e2e/archive.spec.ts` · `edit.spec.ts` · `glossary.spec.ts` · `preview.spec.ts` · `progress.spec.ts` · `retranslate.spec.ts` · `upload.spec.ts`）：起真 serve + 真 dev server |
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
                preview/（PdfCanvas · BboxLayer · PreviewToolbar · PreviewArea · CompileBar · DownloadButton）
                edit/（ParagraphEditor · BboxEditor · CandidatePanel（W11 重译候选））
                archive/（ArchiveView 版本列表 · ArchiveSummary 归档摘要（W12））
    lib/        api.ts（/api/v1 + 错误信封 + PATCH/POST/PUT/DELETE/multipart）· queries.ts（含 W08 上传/job/profiles、W10 草稿/段落、W11 候选、W12 版本、W13 词表）
                uploads.ts（客户端预检：魔数 + 200MB）· jobs.ts（活动判据/轮询/from 判断/页码形状）
                preview.ts（产物选择 + geometry 解析）· events.ts（SSE 帧/URL/live/分组/窗口合并）
                draft.ts（草稿解析/排版范围校验/PATCH 构造）· download.ts（下载与质量徽标判定表）
                versions.ts（版本归档的 URL/徽标/摘要判定表，复用 download.ts）
                glossary.ts（词表行编辑模型 + CSV 解析/导出 + 行级校验，与后端 §3.8 同口径）
                timeline.ts（阶段合并 + 条宽）· pdf.ts（pdf.js worker/cmap/字体配置）
                humanize.ts · routing.ts · cn.ts
    components/jobs/  StartJobCard（开始翻译配置卡，含 W13 词表开关）· ActiveJobCard（运行中/失败/取消卡）
    screens/    LibraryScreen / DocumentCard / GlossaryScreen（W13 全局词表）/ WorkbenchScreen / PlaceholderScreen
    stores/     ui.ts（三栏宽度 + 分隔条 + 屏/预览模式 + 预览页码/bbox 图层/选中段落/重译 profile）
  scripts/      sync-pdfjs-assets.mjs（把 pdf.js 静态资源复制进 public/pdfjs/）
  e2e/          Playwright 真浏览器用例（archive.spec.ts · edit.spec.ts · glossary.spec.ts · preview.spec.ts · progress.spec.ts · upload.spec.ts）
                fixtures/sample.pdf（602 字节最小合法 PDF）· fixtures/sleep-translator.sh（长睡 stub）
                fixtures/glossary-translator.sh（W13 离线 stub：读干提示词、回显原文）
  tests/        Vitest 用例（api / store / routing / 文件库屏+上传 / job 卡与 hooks / 工作台壳 /
                预览坐标与组件 / 编辑坐标与组件（W10 编辑、W11 候选面板、W12 归档视图、W13 词表）/ 事件流与时间线）
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
- `edit.spec.ts`（W10）**不改基线**：setup 用 `cp -Rc`（clonefile，秒级）把
  `tmp/e2e-2602-02908v2-20260917` 克隆成 `tmp/w10-edit-<时间戳>/`（每次新 did），
  serve root 仍是仓库 `tmp/`（既有用例与 W08 上传都靠它）；草稿/编译产物都落在副本里。
  该用例会跑一次**真编译**（实测 ~3.1 分钟，`test.setTimeout(300_000)`）：改译文 → 保存 →
  断言 `running`（编辑禁用）→ 等 `ok` → `compile.revision/stale`/下载按钮断言 → 拖 bbox 手柄 →
  断言草稿 `layout.box` 变化 → 第二次编译只断言进 `running` 后取消（不让 e2e 跑十分钟）。
- 断言：canvas 真有文字像素、bbox 数量 == 服务端实体数、框内有文字像素、parse/layout 两套换算
  落到同一矩形（±0.6px）、点框选中联动右侧面板、切换模式不重建译侧画布、产物响应含 `206`、
  全程无外网请求（禁 CDN）；`progress.spec.ts` 再断言事件面板行数 == 服务端本页条数、最新在上、
  SSE 连上（`data-status=open`）、时间线 7 段 `data-state=ok` 且耗时/总用时与 `stage-state` 同口径、
  无 console error；`upload.spec.ts` 覆盖 W08 的上传→开始→取消（见下）。
- `retranslate.spec.ts`（W11）自建**完全离线**的 fixture：`tmp/w11-candidates-<时间戳>/` 里放一份
  602 字节的合法 PDF（根下 `source.pdf` + `output/paper.mono.pdf`，预览要能渲染）+ 最小
  `agent/{anchors,translated.md,translated.jsonl,layout_geometry}`（段落面板与 bbox 图层有数据），
  `tmp/.bdt-serve/profiles.json` 里写两条 stub profile（translator = 仓库 `e2e/fixtures/
  candidate-translator.sh` + 候选正文，**不联网**），跑完还原。用例顺序：点段 → AI 重译 →
  断言候选 `pending` + **`agent/translated.md`/旧 PDF 的 sha256 不变、`GET /paragraphs` 仍是基线、
  草稿仍是 r0** → 采用（草稿 r1 + target=候选 + 译文框变文本）→ 断言 1.5s 防抖编译 job 出现后取消
  （不跑真 build）→ 再生成一条并拒绝（草稿仍是 r1）。
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
  `-heavy-range` / `e2e-progress*.png` / `e2e-edit-*.png`），性能数字以 `[perf]` / `[range]` 打到 stdout。

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
  dual 开关、**词表开关**（`use_glossary`，缺省开；词表为空时禁用并提示「词表为空」，
  条数从 `GET /glossary` 读）、高级折叠里的「起点阶段」；`from` 默认值 = **有 parse 产物 → translate，否则 parse**
  （判据 `stage_summary.parse === 'ok'` 或 `available.anchors`/`parse_snapshot`）。
- 提交 `POST /documents/{did}/jobs`，body 只有 `action`/`from`/`pages`/`dual`/`profile`/`use_glossary`
  —— translator/reviewer 命令由**服务端**从 profile 解析，前端连字段都没有；词表也只有这个布尔，
  词表内容与注入用的文件路径全在服务端（docs/frontend/api.md §3.8）。
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

## 编辑闭环（W10）：点段改译文 / 拖 bbox / 草稿与编译状态

服务端真源在 `docs/frontend/api.md` §3.2/§3.3：草稿 `PATCH {base_revision, paragraphs}`（乐观并发 +
1.5s 服务端防抖编译），`compile` 字段描述最近一次成功发布的产物。前端只做**显示与换算**。

### 右侧面板（段落 | 事件流双 tab）

- 进度视图仍是事件流在前（W06 语义不变）；翻译 / 识别 / 检查视图段落编辑器在前、事件流退为次要 tab；
  顶部「已选中段落」一行在任何 tab 都可见（W05 的选中联动）。
- `ParagraphEditor`（`components/edit/`）：译文 = **草稿覆盖优先于 `GET /paragraphs` 基线**，
  有覆盖时显示「草稿已修改」+「恢复基线」（PATCH `target: null`）；排版四个数值覆盖
  （`scale_cap` 0.1–5 / `font_scale` 0.2–5 / `line_skip` 0.8–3 / `box_scale` 0.3–5，与
  `layout_overrides.PARAGRAPH_FLOAT_KEYS` 同范围）超范围时输入框旁给提示且**不发 PATCH**。
- 保存：本地 1.5s 防抖（加载/失焦立即存，Cmd+S 也立即），无改动不发请求；`layout` 是**整对象替换**
  （服务端语义），所以补丁会带上草稿里已有的 `box` 与不认识的键，不静默丢数据。
- 冲突分支按错误码：409 `revision_conflict` → 提示 + 「刷新草稿」；409 `document_busy` →
  「编译中，稍后再试」；422 `draft_invalid` → 错误条给服务端逐条 `detail.errors`。
- **编辑禁用**：`compile.status=running` 或该文档有活动 job → textarea `readonly` + 参数输入禁用
  （服务端也会 409 挡）。

### bbox 拖拽（`components/edit/BboxEditor.tsx`）

- 只对**选中段**且**版面框（`pdf_native`）+ 译文侧**生效：8 个手柄（四角 + 四边中点）+ 框体平移，
  Pointer Events + `setPointerCapture`（与分隔条同一套拖拽模式）；拖拽中在整块视口上盖一层命中层
  挡住底层 rect（不会误选别的段），ESC 取消本次拖拽。
- 拖拽只更新屏幕矩形（预览反馈），**松手**才做 `screenToPdfBox` 逆变换并 PATCH 草稿的 `layout.box`。
- **换算红线**：`screenToPdfBox(rect, viewport, coordSystem, cropbox)` 必须经 viewport 逆变换。
  pdf.js 4.10 的 `PageViewport` 只暴露**点级**逆变换 `convertToPdfPoint`（内部即
  `Util.applyInverseTransform(this.transform)`，没有 `convertToPdfRectangle`），所以逆变换 = 矩形两个
  对角点各调一次 + min/max 归一，再反 cropbox 偏移与 y 翻转（`pdf_native` 减 `cropbox` 原点，
  `pdf_topleft` 用 `cropbox.y1 - y`）。**绝不假设 scale=1、绝不直接减 cropbox**；与 `pdfToScreen`
  的 roundtrip（含 scale≠1、cropbox 原点≠0、rotation 90/180/270）是 `tests/edit-coords.test.ts`。

### 编译状态条与下载（`CompileBar` / `DownloadButton`）

- 状态条：`running` → 「编译中…」+ 脉冲 + 编辑禁用；`failed` → 错误条 + `error_code`
  （来自 `GET /documents/{did}/jobs` 里最近一条 `action=compile`，详情 `compile` 字段不带错误码）
  + 重试；`stale` → 「草稿比当前 PDF 新（PDF 修订 r{n}，草稿 r{m}）」+ 手动编译；`ok` → 「已更新到 r{n}」。
- 手动编译 = `POST /jobs {action:"compile", scope:"full", base_revision:<草稿 revision>}`；
  活动 job 期间按钮禁用（服务端会 409）。详情在 `running`/`stale` 时按 2s 轮询（`useDocument` 的
  `refetchWhen`），编译一落定自动停。
- 下载：**只有 `compile.artifact` 存在才启用**（没有产物 → 禁用 + tooltip 说明原因）；`stale`/`failed`
  时旧 PDF 仍可下载，但显式标「比草稿旧」/「编译失败 · 仍是 r{n}」；文件落盘名加 `r{artifact.revision}`
  后缀（**只改前端名**，服务端产物名不动），URL 带 `?r=<revision>` 保证拿到该修订的字节、
  也让预览在编译后重新取字节（同名产物会被原地替换，不带参数的话 pdf.js 还显示旧 PDF）。
- 质量徽标：只有 `quality.pipeline_ok=true` 才绿；`check.verdict=needs_fix`（或 reviewer needs_fix）
  一律黄标「检查未通过」；其余中性（「待人工审查」/「检查不可用」）——编译成功 ≠ 质量通过。

## 重译候选（W11）：AI 重译 → 采用 / 拒绝

服务端真源在 `docs/frontend/api.md` §3.6。前端只做两件事：把 profile **id** 发过去（命令由服务端解析）、
把候选与当前译文并排显示：

- `CandidatePanel`（`components/edit/CandidatePanel.tsx`，挂在 `ParagraphEditor` 下半部分）：
  「AI 重译」按钮 + profile 下拉（**只列有 translator 的 profile**；选过的记在 `uiStore.retranslateProfile`，
  会话内记住 = 「或用上次」）+ 候选对比区。
- 三条数据口径（hooks 在 `lib/queries.ts`）：
  - 生成 = `POST …/paragraphs/{pid}/retranslate {profile}`（job，`useRetranslateMutation`）：
    成功后只让候选列表/ jobs / stage-state 失效 —— **草稿不失效**（候选没采用就不改译文，这是 W11 的语义）；
  - 采用 = `POST …/candidates/{cid}/adopt`（`useAdoptCandidateMutation`）：服务端写草稿（`revision+1`）
    并触发防抖编译，返回体就是**新草稿** → 直接 `setQueryData(draft)`，所以译文框**立刻**变候选文本、
    顶部出现「草稿已修改」（不会本地替掉正文，路径与服务端一致）；
  - 拒绝 = `POST …/candidates/{cid}/reject`：只改候选状态（不动草稿），因此**活动 job 期间也允许**，
    列表刷新后该候选折进「已决定的候选（N）」（`<details>`）。
- 候选列表自限轮询：只要有 `pending` 且 `candidate_target=null`（那条还在生成）就 2s 取一次，
  都生成完就停 —— 候选行在提交时就出现（服务端发号），译文是 job 结束才填上的。
- 禁用口径：有活动 job / 编译中 → 生成与采用禁用（服务端 409 `document_busy`）；
  生成中的候选（`candidate_target=null`）采用禁用（服务端 409 `candidate_not_ready`）。
- 候选**不影响**预览/详情/下载：采用之前那些接口看不到候选内容（服务端保证），前端也不本地替换。
- `archive.spec.ts`（W12）**不跑真编译**：fixture 直接按 §3.7 的落盘形状造出「已编译过两次」的
  `tmp/w12-versions-<时间戳>/`（`versions.json` + `versions/{1,2}.pdf` + `compile.json`（当前 r2）+
  `draft.json`（r3 → stale））与一个空态文档 `tmp/w12-empty-<时间戳>/`；断言清单新 → 旧、
  当前版本高亮、stale 提示条、r1 下载的 href/落盘名与实际字节、非数字/不在清单 → 404
  `version_not_found`、`GET /artifacts` 不混入 versions、空态引导。归档**写入**（发布即归档、
  50 上限、失败/取消不归档、trigger 值）由后端 pytest 用 stub 编译路径覆盖（见 W12 报告）。
- `glossary.spec.ts`（W13）**不跑真翻译**（translator 是离线 stub）：“开始翻译”那条用例用 stub
  translator 从 translate 起跑，再取 `GET /artifacts/agent/prompt.md` 断言它含「术语约束（词表）」
  段与词条，并用 `use_glossary:false` 的第二次 job 做对比 —— 这是“服务端真把词表注入了翻译提示词”
  的端到端证据。词表的 CRUD / CSV 导入导出 / 开关禁用逻辑也在同一个 spec 里。
  它的副作用（共享的 `tmp/.bdt-serve/glossary.csv` 与 `profiles.json`、自建 workdir、debug 查看器）
  在用例前后备份/还原。

## 版本归档（W12）：历史版本列表 + 任一版本下载

服务端真源在 `docs/frontend/api.md` §3.7；四块的 UI 位置：

- **归档视图** `#/d/:did/archive`（`components/archive/ArchiveView.tsx`，占满预览区）：版本列表
  新 → 旧，每行 = r 徽标 + 「当前版本」标记 + 触发原因（自动/手动）+ 质量徽标 + 相对时间/大小 +
  下载按钮（`…/versions/<r>/pdf` + `download=<名>.r<r>.pdf`）。头部说明「保留最近 50 个版本」。
- **当前版本高亮**：判据是服务端的 `current_revision`（= `compile.artifact` 那一版，不是清单第一行）；
  `stale=true` 时页面顶部给提示条「草稿有未编译修改：当前可下载的是 r{n}，比草稿旧」。
- **空态**：从没编译成功过 → 引导卡 + 「去翻译视图开始编辑」（不是错误，也不报 404）。
- **右侧面板**：非进度视图的 tab 集是「段落 | 事件流 | 归档」（归档视图下是「归档 | 事件流」，
  默认归档）。归档 tab = 摘要（版本数 + 最新一版 + `stale` 提示 + 直达下载）+ 「查看全部」链接；
  与归档视图共用 `useVersions` 的同一个 query key，切 tab 不会多打请求。
- **轮询**：`compile` 未落定（running / stale）时 2s 一次（编译一发布就多一版），落定自停。
- **主下载不变**：W10 的下载按钮仍指向 `output/<artifact.name>`（§3.2 主路径）；只在它旁边加了
  一个「历史版本」文本链接跳归档视图。质量徽标复用 `lib/download.ts::qualityBadge`
  （版本快照只有 `check_verdict`/`pipeline_ok`，映射后走同一份判定表）：**needs_fix 不禁用下载**
  （质量只记录不门禁，黄标），与 W10 规则一致。

## 词表（W13）：全局术语表（`#/glossary`）

服务端真源在 `docs/frontend/api.md` §3.8（`GET/PUT/DELETE /api/v1/glossary`，**一个全局词表**，
落在 `<store_base>/.bdt-serve/glossary.csv`）。四块 UI：

- **词表屏**（`screens/GlossaryScreen.tsx`）：表格三列（source / target / note）行内编辑 +
  添加行/删行 + 保存/放弃。保存是**整表替换**（`PUT`，不做行级 patch）：所以「保存」只在真有
  改动且行级校验通过时可用，「放弃」把本地草稿丢掉回到服务端那一份。行级错误（只填了一边 /
  超 200 字符）就地标在行上（`lib/glossary.ts::validateGlossaryRows`，与后端同一口径），
  服务端仍是最终权威（`422 glossary_invalid` 带 `detail.index`/`detail.field`）。
- **CSV 导入/导出在浏览器里**（后端 `PUT` 只收 JSON 条目）：导入走隐藏的 file input，
  RFC 4180 解析 + 表头校验（缺 `source`/`target` → 就地报错，不动现有表），导入结果只是本地草稿，
  要再点保存；导出用 `Blob` + `download` 落盘 `glossary.csv`，内容是**屏幕上的表**（含未保存的编辑）。
- **不回溯提示条**（`data-od-id="glossary-no-retro"`，常驻）：词表变更不会自动重翻任何已翻译内容，
  重新跑一次翻译才生效 —— 这是 brief 的一致性红线，不允许只写在文档里。
- **注入入口**：「开始翻译」卡上的`使用词表`开关（`use_glossary`，缺省开）；词表为空时禁用并写
  「（词表为空）」。客户端只给这个布尔，**词表内容与注入用的文件路径全在服务端**；
  图标栏「词表」项带**条数徽标**（有词表时才显示，拿不到数据就不冒充 0 条）。

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
