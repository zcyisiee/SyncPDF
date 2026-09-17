# web/ · iee Translater 前端

Vite + React 18 + TypeScript + Tailwind + TanStack Query + Zustand。
视觉规范唯一事实来源：`docs/frontend/design-v2/DESIGN.md`（设计令牌照抄在
`tailwind.config.ts` + `src/app/globals.css`）；HTTP 契约唯一事实来源：
`docs/frontend/api.md` 与运行中服务的 `/openapi.json`。

本目录当前范围（W04）：三栏工作台壳 + 设计令牌 + 文件库屏（真数据）+ hash 路由。
**不做**：PDF 预览（W05）、事件流/时间线真数据（W06）、上传（W08）。这些区域都渲染带
`data-od-id` 的占位并写明接入任务。

## 开发工作流（两个终端）

```bash
# 终端 A：后端（真实数据；--root 下每个子目录 = 一个文档 workdir）
PATH="$PWD/.venv/bin:$PATH" bdt serve --root tmp --port 8787
#   若 macOS 把 editable .pth 标了 hidden 导致 import 失败：
#   chflags nohidden .venv/lib/python3.12/site-packages/_editable_impl_babeldoc_agent.pth

# 终端 B：前端 dev server（/api 由 Vite 代理到 127.0.0.1:8787）
cd web && pnpm install && pnpm dev      # http://localhost:5173/#/library
```

代理端口默认 8787，可用 `BDT_SERVE_PORT=<port> pnpm dev` 覆盖（`vite.config.ts`）。
serve 不注册 CORS，所以前端必须走这个同源代理（`docs/frontend/api.md` §1）。

## 命令

| 命令 | 作用 |
|---|---|
| `pnpm dev` | dev server（含 /api 代理） |
| `pnpm build` | `tsc --noEmit` + `vite build` → `web/dist`（W15 才接到 `babeldoc_tools/web_dist/`） |
| `pnpm typecheck` | `tsc --noEmit` |
| `pnpm lint` | eslint flat config + typescript-eslint，`--max-warnings 0` |
| `pnpm test` | Vitest（jsdom + @testing-library/react），不起真后端 |
| `pnpm e2e` | Playwright 骨架（`playwright.config.ts`）；用例从 W05 起进 `e2e/` |
| `pnpm gen:api` | 从运行中的 serve 拉 `/openapi.json` 生成 `src/api/schema.d.ts` |

`pnpm e2e` 首次运行需要 `pnpm exec playwright install chromium`（本机已装 Chrome 时可用
`channel: 'chrome'`）。

## 目录

```
web/
  src/
    api/        schema.d.ts（openapi-typescript 生成）+ types.ts（具名别名）
    app/        App.tsx（hash 路由分发）+ globals.css（设计令牌 CSS 变量）
    components/ icons.tsx · ui/（Button/Chip/StatusBadge/ScrollArea/ErrorCard/Tooltip）
                shell/（Topbar/IconRail/ScreenFrame/Gutter/ViewRail/InspectorPanel/Timeline）
    lib/        api.ts（/api/v1 + 错误信封）· queries.ts · humanize.ts · routing.ts · cn.ts
    screens/    LibraryScreen / DocumentCard / WorkbenchScreen / PlaceholderScreen
    stores/     ui.ts（三栏宽度 + 分隔条拖拽 + 屏/预览模式，localStorage 持久化）
  tests/        Vitest 用例（api / store / routing / 文件库屏 / 工作台壳 / App 路由）
  tmp-smoke/    本地冒烟截图与日志（.gitignore，不入库）
```

## 设计与契约约束（改动时别忘）

- 令牌只从 `tailwind.config.ts` / `src/app/globals.css` 取，禁止新增灰阶或第二个强调色；
  `--accent` 每屏可见使用 ≤ 2 处（DESIGN.md §1.2）。
- 工作台栅格由 CSS 变量驱动：`--vrw`（220，160–320）/ `--inspw`（360，280–560）/
  `--tlh`（96，72–160），持久化键 `ieet.vrw` / `ieet.inspw` / `ieet.tlh` /
  `ieet.inspCollapsed` / `ieet.screen`（§8.2）。
- 所有区块带 `data-od-id`（kebab-case，§7.9）；占位区同样带，供后续任务定位替换点。
- 计数为 `null` 表示产物缺失，显示 `—`，不要当 0（`docs/frontend/api.md` §3.1）。
- 全站唯一动效是 running 圆点脉冲（`.pulse-dot`），只加在真实 `running` 状态上。
