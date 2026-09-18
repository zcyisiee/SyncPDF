# Task
W15：集成验收——静态资源打包 + 全回归 + 端到端浏览器验收（真浏览器截图/下载验证）+ 使用说明

## Objective
feat/web-frontend 的收官任务（前置 W01–W14 已全部合入，HEAD `42be311`）。目标：
1. serve 内嵌前端静态资源（`pnpm build` 产物由 `bdt serve` 直接伺服，单进程自包含，开发模式 Vite 代理保留）。
2. 全量回归（后端 + 前端 + e2e）。
3. **完整用户旅程的真浏览器端到端验收 spec**（`integration.spec.ts`）：上传 → 选 profile/页范围/dual/词表开关 → 跑 stub 翻译 job → 实时进度（事件流 + 时间线 + job_update 推送）→ 编辑段落译文 → 调整 bbox → 候选重译与采用 → 编译（可用既有 fake/快速 build 夹具，**不跑真 LaTeX build**）→ 版本归档视图 → **下载 PDF 并字节级校验**。每一步截图落盘。
4. 使用说明（README）。

**真实 LaTeX build 不在本任务范围**（W09/W10/W12 e2e 已各自覆盖编译链路；真 build ~180s 已在冒烟中验证过）。本任务验收的是「前端交互链路的完整性与正确性」。

## Context
必读（先完整读再动手）：
1. `docs/frontend/api.md` —— API 契约单一来源（§3 全部已实现）。
2. `babeldoc_tools/serve/app.py`、`serve/cli.py` —— app 组装、lifespan、静态伺服现状（W01 起 serve 只挂 /api/v1；可能已有简单静态伺服，核实）。
3. `web/vite.config.ts`、`web/playwright.config.ts` —— 现有 build/dev proxy/e2e 服务器配置（e2e 目前 webServer 起 dev server 还是 preview？核实后决定静态伺服实现方式）。
4. 既有 e2e（`web/e2e/*.spec.ts`，16 例）：fixtures（stub translator、自建 workdir、`cp -Rc` clonefile 秒级副本）、`upload.spec.ts`（上传全链）、`edit.spec.ts`（编辑+真编译 175s）、`retranslate.spec.ts`、`glossary.spec.ts`、`archive.spec.ts`、`streaming.spec.ts`（SSE 时序）。**W15 不重复它们已覆盖的单点，写一条贯穿全旅程的 spec**。
5. 既有夹具复用：`web/e2e/fixtures/`（candidate-translator.sh、sleep-translator.sh、上传用 sample PDF 等）；`web/e2e/utils/`（workdir 构建 helper）。
6. W09 编译语义：compile = 隔离副本 + `bdt run --from apply`；e2e 里 edit.spec.ts 怎么做「快速假 build」核实后复用同一机制。
7. W12 版本：发布即归档 hardlink、versions.json 清单、`?r=` 下载参数。
8. macOS 无 GNU timeout；bash 每段 ≤240s 分段跑。

## Deliverables
1. **静态资源内嵌**（后端）：
   - `bdt serve` 伺服 `web/dist/`（存在时）；SPA fallback（非 /api、非 /docs 路径 → index.html）；`Cache-Control` 合理（index.html no-cache，带 hash 的 assets 长缓存）。
   - dist 不存在时行为：静默跳过（只伺服 API，stdout banner 不变），不报错。
   - `web/dist` 不入库（.gitignore 核实）。
   - OpenAPI/docs 路由不受影响；CORS 语义不变（仍不注册 CORSMiddleware）。
2. **集成 spec**（`web/e2e/integration.spec.ts`，1–2 条 test）：
   - 用真 `bdt serve`（静态模式：先 `pnpm build`，e2e webServer 起 serve 而非 dev server——若改动 playwright config 需保持既有 16 例不破）。
   - 旅程：浏览器打开首页 → 拖拽/选择上传 PDF → 文档出现在库 → 打开 workbench → 配置 job（profile/页范围/dual/词表开关）→ 提交 → **断言**：事件流出现条目、时间线有 live 段、ActiveJobCard 显示运行中、job_update 推送驱动状态切换（不等轮询间隔）→ job 成功后预览出现产物 → 点段编辑译文保存（revision 前进）→ bbox 拖拽调整 + 数值调整保存 → 候选重译 → 采用（草稿 target 变化）→ 触发编译（快速假 build）→ 编译成功版本归档出现新 rN → **下载最新版 PDF，与磁盘上归档文件做 sha256 字节级比对** → 下载一个历史版本同样比对。
   - 每个阶段 `page.screenshot()` 落盘到测试产物目录（Playwright trace/screenshot 已有机制，指定 path 前缀即可）。
   - 断言优先用 `data-od-id`（既有约定）。
3. **全量回归**：后端 pytest 全量、ruff、`git diff --check`；前端五项 + e2e 全套（既有 16 + 新增）。
4. **README**（根 + web/）：静态伺服说明（`bdt serve` 单进程自包含）、截图目录说明、集成验收跑法。

## Constraints
- 后端只动 `babeldoc_tools/serve/`（静态伺服）+ 测试；前端只动 `web/`；文档 README/api.md。
- **不改任何 W01–W14 已验收的业务语义**（job/draft/compile/candidates/versions/glossary/SSE）；静态伺服是纯新增。
- 不跑真 LaTeX build、不调真实 LLM；不引入新依赖（前端零新包，后端用 FastAPI/Starlette 自带 StaticFiles 或等价手写路由）。
- 上传的 sample PDF 用既有夹具或最小自造（<50KB）；整体 spec 运行时间目标 <90s（不含既有 spec）。
- 探查纪律：rg 限定 `babeldoc/ babeldoc_tools/ web/ tests/`；禁止 `find /`；子进程一律 `capture_output=True, timeout=`。
- macOS 无 GNU timeout；bash 工具 timeout ≤240s 分段；不委派、不 commit/push（主控提交）。
- 实现与契约冲突 → 停下报告，不自行改契约。

## Validation
```
cd web && pnpm typecheck && pnpm lint && pnpm test && pnpm build
cd .. && PATH="$PWD/.venv/bin:$PATH" PYTHONPATH="$PWD" .venv/bin/python -m pytest tests/ -q -p no:cacheprovider --basetemp="tmp/pytest-W15-$(date +%Y%m%d-%H%M%S)"
.venv/bin/ruff check babeldoc_tools/serve tests/
cd web && pnpm e2e
git diff --check
```
外加：静态模式手工冒烟——`pnpm build` 后起 `bdt serve --root <tmp>`，curl 首页/静态资产/SPA fallback/带 hash 资产的缓存头，核对 stdout banner 仍是单行 JSON；dist 移走后 serve 正常只剩 API。

## Report back
静态伺服实现要点与缓存策略；集成 spec 的旅程覆盖清单（每步断言什么）；下载字节级比对结果；截图清单（路径）；全量回归数字；偏差清单；遗留风险。绑定 output 回传。最多两轮修复。
