# web/ · iee Translater 前端

Vite + React 18 + TypeScript + Tailwind + TanStack Query + Zustand。
视觉规范唯一事实来源：`docs/frontend/design-v2/DESIGN.md`（设计令牌照抄在
`tailwind.config.ts` + `src/app/globals.css`）；HTTP 契约唯一事实来源：
`docs/frontend/api.md` 与运行中服务的 `/openapi.json`。

本目录当前范围（W05）：三栏工作台壳 + 设计令牌 + 文件库屏（真数据）+ hash 路由 +
**PDF 预览**（pdf.js 渲染产物 PDF、parse/layout 两套 bbox 叠加、源/译/对照三模式、点框选中）。
**不做**：连续滚动、缩放控件、bbox 拖拽（W09）、译文覆盖层（W06）、事件流/时间线真数据（W06）、
上传（W08）；这些区域渲染带 `data-od-id` 的占位并写明接入任务。

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
                preview/（PdfCanvas · BboxLayer · PreviewToolbar · PreviewArea）
    lib/        api.ts（/api/v1 + 错误信封）· queries.ts · preview.ts（产物选择 + geometry 解析）
                pdf.ts（pdf.js worker/cmap/字体配置）· humanize.ts · routing.ts · cn.ts
    screens/    LibraryScreen / DocumentCard / WorkbenchScreen / PlaceholderScreen
    stores/     ui.ts（三栏宽度 + 分隔条 + 屏/预览模式 + 预览页码/bbox 图层/选中段落）
  scripts/      sync-pdfjs-assets.mjs（把 pdf.js 静态资源复制进 public/pdfjs/）
  e2e/          Playwright 真浏览器用例（preview.spec.ts）
  tests/        Vitest 用例（api / store / routing / 文件库屏 / 工作台壳 / 预览坐标与组件 / App 路由）
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
  420 段），缺失时用例会失败并提示；重产物用例读 `tmp/e2e-2602-02908v2-20260917`
  （65MB mono / 84MB dual），fixture 不在时自动 skip。
- 断言：canvas 真有文字像素、bbox 数量 == 服务端实体数、框内有文字像素、parse/layout 两套换算
  落到同一矩形（±0.6px）、点框选中联动右侧面板、切换模式不重建译侧画布、产物响应含 `206`、
  全程无外网请求（禁 CDN）。
- 截图落在 `web/tmp-smoke/`（`e2e-preview.png` / `-layout` / `-compare` / `-selected` /
  `-heavy-range`），性能数字以 `[perf]` / `[range]` 打到 stdout。

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
- 全站唯一动效是 running 圆点脉冲（`.pulse-dot`），只加在真实 `running` 状态上。
