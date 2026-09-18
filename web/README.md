# Web 工作台

React + TypeScript + Vite 前端，消费 `bdt serve`。系统地图见根目录 [ARCHITECTURE.md](../ARCHITECTURE.md)，接口和交互边界集中维护在 [HTTP 参考](../docs/reference/http-api.md)，不再维护任务轮次记录副本。

## 开发与构建

先在仓库根安装 web extra 并启动后端：

```bash
uv sync --extra web
mkdir -p tmp/library
PATH="$PWD/.venv/bin:$PATH" bdt serve --root tmp/library --port 8787
```

另一个终端从本目录执行：

```bash
pnpm install
pnpm dev
```

Vite 默认代理后端 8787 端口，可用 `BDT_SERVE_PORT=<端口> pnpm dev` 覆盖。后端不注册 CORS。`pnpm build` 生成 `web/dist`，由 `bdt serve` 同源提供；它不是第二个产品服务入口。

## 去哪里改

| 责任 | 位置 |
|---|---|
| 页面、工作台与编辑组件 | `src/screens/`、`src/components/` |
| 请求、缓存、草稿、局部编译与导出 | `src/lib/queries.ts` 及相关 lib 模块 |
| 持久事件订阅 | `src/lib/usePersistentEvents.ts`；与旧诊断事件流并存 |
| OpenAPI 类型 | `src/api/schema.d.ts`；后端启动后运行 `pnpm gen:api` |
| 视觉令牌 | `src/app/globals.css`、`tailwind.config.ts` |
| 本地 PDF.js 资源 | `scripts/sync-pdfjs-assets.mjs`；dev/build 时同步 |

保存草稿不自动编译。编辑、局部预览、全量兼容编译、最终导出有各自 revision 与状态；不要把旧 PDF 显示成新结果，不把质量未通过显示成完成。细节看 HTTP 参考与后端执行函数。

## 验证

`pnpm typecheck`、`pnpm lint`、`pnpm test` 分别检查类型、风格与单元行为；涉及界面/交互时再运行适用的 `pnpm e2e`。完整命令定义在 `package.json`。截图、日志、翻译验收产物保存在仓库根 `tmp/`；不要提交 `dist/` 或 `node_modules/`。
