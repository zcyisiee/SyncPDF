# Web 前端执行修订与验收清单

2026-09-17。分支：`feat/web-frontend`。本文件修订 PLAN.md 和 docs/frontend/README.md 的历史冲突；最终 HTTP 形状由 docs/frontend/api.md 与 FastAPI OpenAPI 定义。

## 用户确认

- 本次完成 M0–M6 全部能力后统一验收，不在上传/翻译阶段提前收尾。
- 已成功编译的 PDF 可下载；必须标注质量状态、编译修订号与当前草稿差异。待审查/检查失败不等于 pipeline 成功。
- 继续现有 React/TS/Vite/Tailwind/shadcn、TanStack Query/Zustand、pdf.js、FastAPI 方案与 v2 设计。
- 单用户本地服务先落地；云端认证、租户隔离、对象存储/任务队列不在本次，但存储和调度须有清晰模块边界。默认 loopback、同源访问。
- 实施由 deepseek/deepseek-flash:high 叶子 worker 执行，主控逐项审 diff、跑验证、验收。

## 必须纠正的架构细节

1. 沿用最新 PLAN.md 的 `<root>/<did>` 布局和 `/api/v1/documents/{did}` 路由，不再同时实现 projects 前缀。旧 README 明确标记历史协议，链接最终 api.md。
2. 单入口仍为 bdt；复用当前唯一 `babeldoc_tools` 包，在内部增加 serve 子包。不得增加对外 CLI 或放宽单入口守卫。
3. `retranslate_ids` 当前会合并 translated.md，不能直接用它生成待采用候选。需分离生成与应用，或用隔离任务工作副本；未采用不得改当前译文。
4. debug seq 只在 run 内递增。事件需要 run 身份和可续传游标；job 生命周期事件需持久化，不能由每个 SSE 连接临时生成。分页过滤即使无匹配事件也应推进扫描游标，处理半行、重复与断线。
5. job 必须持久化、同文档串行、跨文档限流；取消清理整个进程组，持有锁直到退出。重启核实进程身份，不凭孤立 PID 发信号，也不自动重跑收费调用。
6. 草稿单调 revision + 请求乐观并发校验。编译捕获固定 revision，在隔离工作目录产出并成功后原子发布。失败/取消不能破坏上一份可下载 PDF。下载链接关联 artifact revision，前端不把旧 PDF 标成最新。
7. 草稿保存触发服务端 1.5s 防抖，浏览器断开不丢编译。活动任务期间编辑只读；两标签页冲突返回 409 并提供刷新/重试提示。
8. 保留现有 check/reviewer 质量门禁；provider 配置需覆盖 translator 和 reviewer，不用伪造 pass 把全流程跑绿。HTTP 只接 profile id，不接受客户端任意 shell 命令或密钥。
9. PDF 预览采用真实 PDF + pdf.js + SVG。DESIGN.md 中自然流 HTML 是原型表现，不适用于真实 PDF bbox。debug 坐标已明确为 pdf_topleft，不能再次翻转 y；layout 来源要分别核实，转换测试覆盖 cropbox/rotation/zoom 与选择页映射。
10. 页级编译先按已批准设计回退全量，返回 requested/effective scope 和原因；不花一天研究优化才开始编辑闭环。
11. PDF.js 高清指按缩放/设备像素比渲染 PDF；不承诺扫描 PDF 原有文字的清晰度可超越源文件。
12. 不编造进度百分比/ETA。事件、阶段时间来自实际执行；不支持流式的 translator 显示真实阶段等待，输出到达后再更新段落计数。流式结果仅是进度，最终锚点/占位符校验仍必需。

## 小任务顺序

每项单独 brief、单写者、最多两轮本地修复；环境/协议阻塞立即汇报，不无限尝试。后续 brief 在前置验收后再具体化，避免巨型一次性委派。

- [ ] W01 服务骨架：web extra、CLI、store 路径约束、health、基础 API 契约。
- [ ] W02 只读文档：列表、详情、段落、几何、阶段与检查。
- [ ] W03 事件和文件：分页/SSE、run 游标、白名单下载/Range。
- [ ] W04 前端基础：依赖、生成类型链路、v2 三栏/可拖分隔条、导航/文件库只读。
- [ ] W05 PDF 预览：真实 PDF、bbox、坐标/页映射、原文/译文/对照、同步与高亮。
- [ ] W06 进度 UI：事件虚拟列表/过滤/JSON、真实动态时间线、错误与断线状态。
- [ ] W07 job 核心：持久化调度、受控子进程、取消/重启与进程树测试。
- [ ] W08 上传与 profiles：PDF 校验/限制、受控 run 配置、前端拖拽/配置/开始/取消。
- [ ] W09 草稿与编译：revision、校验、服务端防抖、隔离编译与原子产物发布。
- [ ] W10 编辑 UI：点段修改、bbox 拖拽/数值、自动和手动编译、旧版本提示。
- [ ] W11 候选重译：生成不合并、采用/丢弃、对应 UI 与未选段不变测试。
- [ ] W12 版本归档：版本门禁、快照/回滚、下载/归档 UI。
- [ ] W13 词表：全局/文档 CRUD、CSV、命中计数、prompt 注入与 UI。
- [ ] W14 逐段进度：流式读取与 wrapper 适配、最终段 EOF、失败/重试去重、UI。
- [ ] W15 集成验收：打包静态资源、全回归、真实模型浏览器 E2E、截图/下载验证与使用说明。

## 最终验收

用户从浏览器拖拽 PDF → 选 provider/词表/页范围/dual → 全 pipeline；可看真实 PDF+bbox、事件、阶段和动态耗时；可以取消、重试并明确失败原因。任务结束后手改译文、调整 bbox、选段重译，候选经采用才写入草稿；自动/手动编译更新 PDF；检查问题可跳段修正；存版本、回滚和归档可用；下载 PDF 的内容对应显示的编译修订。真实模型与模拟测试必须分别报告，不以 mock E2E 冒充真实成功。

验证产物全部保留在仓库 tmp/。Python 使用 `.venv/bin/python -m pytest --basetemp=tmp/pytest-<唯一时间>`，PATH 包含 `.venv/bin`。改动 Python 文件跑 `.venv/bin/ruff check`；前端跑 Vitest、tsc、eslint、Playwright；提交前 `git diff --check`。主控亲自复核差异与必要验证后才能勾选任务。
