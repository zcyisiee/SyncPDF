# Task
W05：pdf.js 矢量预览 + bbox SVG 叠加 + 视图切换

## Objective
在 feat/web-frontend（HEAD 764c79e，W04 壳已可用）实现工作台预览区核心：pdf.js 渲染产物 PDF（mono 首选）+ parse bbox（`pdf_topleft`）与 layout bbox（`pdf_native`）SVG 叠加 + 点击段落选中联动右侧面板占位 + 源/译/对照预览模式。完成后用户在浏览器里能看到**真实 PDF 页面 + 段落框**，这是整个产品的视觉核心。

## Context
必读：
1. `web/src/` 现有代码（尤其 `screens/WorkbenchScreen.tsx` 的 `preview-placeholder`、`stores/ui.ts` 的 `previewMode`、`lib/queries.ts`、`api/types.ts`）。
2. `docs/frontend/api.md` §3.1：`GET /documents/{did}/artifacts`（清单，`name=output/x.no_watermark.zh.mono.pdf` 等）、`GET /documents/{did}/artifacts/{name}`（Range 下载，`Accept-Ranges: bytes`）、`GET /documents/{did}/geometry?kind=parse|layout&page=N`（`coord_system` 字段：parse=`pdf_topleft`，layout=`pdf_native`）、`GET /documents/{did}/pdf`。
3. 真实样本 `tmp/ccs3764-dyn`：21 页、parse entities 420（y 向下 pdf_topleft）、layout paragraphs 420（PDF 原生 y 向上）+ `page_info[].cropbox`。**两套坐标系换算在前端做**（契约红线：服务端不转换）：
   - parse（pdf_topleft）：y_top_down，屏幕同向，直接用；需减 cropbox 原点偏移（若 cropbox origin ≠ 0）。
   - layout（pdf_native）：y 向上原点在左下；屏幕 y = cropbox.y1 - (y + h)。
   - 预览显示的是**产物 PDF**，bbox 来自解析快照：对齐基准是页面渲染像素。pdf.js `page.getViewport({scale})` 返回的 viewport 含 PDF user-space→屏幕的完整变换；**用 `viewport.convertToViewportRectangle` 或手工按 transform 换算**，不要假设 PDF 点 == CSS px（scale 由容器宽决定）。
4. pdf.js 集成方式：`pnpm add pdfjs-dist`（版本固定 4.x 稳定版）。**不引 viewer.html**（那是完整查看器，太重），只用 API：`getDocument` + `page.render` 到 canvas + `page.getViewport`。worker 用 `pdfjs-dist/build/pdf.worker.min.mjs`（`new Worker(new URL(...))` 或 `GlobalWorkerOptions.workerSrc`，选 Vite 友好的后者；CDN 禁用，必须本地打包）。字体：pdf.js 需要 standardFontDataUrl——从 `pdfjs-dist/standard_fonts/` 拷进 public 或 vite 静态服务，cmaps 同理（`cmaps/` 目录，学术 PDF 常见 CJK）。
5. PDF 文档源：`GET /documents/{did}/pdf`（W02 已有，`outputs[].path` 是 workdir 相对路径）。渲染首选 mono 产物（`*.no_watermark.zh.mono.pdf`）；没有 mono 用 dual；都没有 → 预览区显示"无产物 PDF"占位卡（引用 `GET /artifacts` 判断，不造假）。**源 PDF 预览（source 模式）**：`artifacts` 清单里 `source.pdf`（kind=source）存在才可用；不存在 → source 模式按钮 disabled + tooltip 说明。
6. 页码导航：`GET /documents/{did}` 的 `pages` 总数；上一页/下一页/跳页输入；滚动条模式 vs 单页模式——**本任务只做单页模式**（连续滚动 W06 视进度再做，避免范围膨胀）。

## Deliverables
1. `web/src/components/preview/`：
   - `PdfCanvas.tsx`：pdf.js 加载 + 渲染单页（props: url, pageNumber, onViewport）;加载/渲染错误显示 ErrorCard（含重试）；render 任务取消（页切换时 `RenderTask.cancel()`，避免竞态花屏）；devicePixelRatio 缩放清晰渲染。
   - `BboxLayer.tsx`：SVG 绝对定位叠加层（props: boxes, viewport, mode=parse|layout, selectedId, onSelect）。rect 命中测试（pointer-events 按需开/关——拖拽工具 W09 才有，本任务 rect 可点击选中）。选中态 2px accent；hover 态 tint。坐标系换算函数 `pdfToScreen(box, viewport, coordSystem)` 导出为纯函数并单测。
   - `PreviewToolbar.tsx`：源/译/对照三模式切换（SegmentedControl 风格，绑定 store `previewMode`）+ 页码输入 + 上下页 + zoom 显示（只读：显示当前 scale，缩放控件本任务不做——固定适宽）+ bbox 开关（parse/layout/off 三态，默认 parse）。
   - `PreviewArea.tsx`：组合以上 + 适宽计算（ResizeObserver → scale = 容器宽/viewport 宽，clamp 0.5–3）+ `data-od-id="preview-canvas"`。对照模式 = 左右双页（源 | 译，各自 PdfCanvas，同一 bbox 层只在译侧）；源模式 = 单页源 PDF（无 bbox 叠加或只叠加 parse 框——设计上源模式无译文概念，叠加 parse 框即可）。
2. `lib/queries.ts` 扩展：`useArtifacts(did)`、`useGeometry(did, kind, page)`（staleTime 60s，key 含全部参数）。geometry 404（snapshot/geometry_unavailable）→ query 置空不是 error（UI 显示"该页无解析数据"小条，预览仍可用）。
3. `stores/ui.ts` 扩展：`previewPage: number`（不持久化，did 变化重置为 1）、`bboxMode: 'parse'|'layout'|'off'`（持久化 `ieet.bboxMode`）、`selectedParagraphId: string|null`。选中联动：点击 bbox → 选中 + 右侧面板占位显示选中段落 id（真内容 W10）。
4. 视图路由接入：`#/d/:did/progress|layout|translate|check` 四个视图的预览区都用 `PreviewArea`（差异只在右侧面板与 bbox 默认模式：progress=parse、layout=parse、translate=layout、check=parse + 检查标记 W12）。本任务只保证四视图预览可用 + bbox 默认模式正确。
5. 测试（Vitest）：
   - `pdfToScreen` 纯函数：pdf_topleft 与 pdf_native 两套换算正确性（含 cropbox 偏移、viewport scale、rotation=0 假设）。
   - `BboxLayer`：渲染 rect 数量/选中态样式/点击回调（jsdom 下 SVG 可渲染）。
   - `PreviewToolbar`：模式切换回调/disabled 态。
   - `useArtifacts`/`useGeometry` 的 404 降级（QueryClient + mock fetch，参考现有 tests/helpers.tsx）。
   - pdf.js 本体渲染不在 jsdom 单测范围（无 canvas），由 Playwright 真浏览器冒烟覆盖。
6. Playwright 首个真用例 `web/e2e/preview.spec.ts`（接 W04 骨架）：起真 serve（`bdt serve --root tmp --port 8788`，webServer 或 globalSetup 自行设计，fixture 用现有 tmp/ 真数据，选 21 页的 `ccs3764-dyn`）：打开 `#/d/ccs3764-dyn/progress` → canvas 出现且非空白（截图对比像素 > 阈值或 `canvas.toDataURL` 长度）→ bbox rect 数量 > 0 → 点击一个 rect → `selectedParagraphId` 反映在右侧面板 → 截图存 `web/tmp-smoke/e2e-preview.png`。**用例必须本地可重复**（README 写清启动方式）。
7. `web/README.md` 更新：pdf.js worker/字体/cmap 的本地打包说明、e2e 运行方式。

## Constraints
- 只改 `web/`（src/tests/e2e/package.json/pnpm-lock/README）。不改 Python、不改 `docs/frontend/api.md`（契约已冻结；若实现中发现契约问题，停下报告）。
- pdf.js worker/字体/cmap 必须本地打包（`pdfjs-dist` 包内文件），**禁止 CDN/外网运行时依赖**。
- 不做：连续滚动、缩放控件、bbox 拖拽编辑（W09）、译文覆盖层（W06 若需要再加）、双页同步滚动。
- rotation：样本 PDF 均 rotation=0；viewport 变换按 pdf.js 标准 API 走即可，不自行处理 rotation 特例（若遇 rotation≠0 的 PDF 显示警告条）。
- 产物 PDF 84MB 级（dual）也要能预览：Range 请求 + pdf.js 按需加载天然支持，但**不要**一次性 fetch 全文件（`getDocument({url})` 自动 Range，不要手动 fetch 成 blob 再喂）。
- 性能基线：21 页 PDF 首页渲染 < 2s（本地）、页切换 < 500ms（M 系列 SSD）；Playwright 断言 timeout 默认。
- `pnpm typecheck && pnpm lint --max-warnings 0 && pnpm test && pnpm build` 全绿；e2e 单独跑通过。
- rg 限定路径 + timeout；不委派；不 commit/push；证据（截图/DOM 断言输出）存 `web/tmp-smoke/`。

## Validation
```
cd web && pnpm typecheck && pnpm lint && pnpm test && pnpm build && pnpm e2e
```
手动冒烟（写进报告）：`bdt serve --root tmp --port 8787` + `pnpm dev`，浏览器开 `#/d/ccs3764-dyn/progress`：canvas 有字、bbox 框对齐段落（肉眼）、切换 layout 模式框仍对齐（换算正确）、对照模式双页、点框选中。截图 3–4 张存 tmp-smoke。`.pth` hidden 问题照 W04 处理。

## Report back
改动清单、pdfToScreen 换算说明（两坐标系各一段伪代码级描述）、四项验证+e2e 结果、冒烟截图路径、性能实测（首页渲染/页切换耗时）、偏差清单、遗留风险。绑定 output 回传。最多两轮修复。
