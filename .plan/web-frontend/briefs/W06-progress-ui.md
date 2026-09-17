# Task
W06：事件流面板 + SSE 接入 + 时间线真数据 + 运行中状态

## Objective
在 feat/web-frontend（HEAD 0586f76，W05 预览可用）实现进度视图的实时层：右侧面板事件流（SSE 订阅 `GET /documents/{did}/events/stream` + 初始分页回填）、底部时间线真数据（7 阶段耗时条，`GET /stage-state` + 事件驱动增量）、文档运行中状态（running 徽标 + 脉冲 + 自动刷新列表/详情）。完成后：对一个已完成 run，事件流显示真实事件时间线，时间线显示真实阶段耗时；对一个进行中的 run（W07 之前可用手工 tail 模拟），SSE 增量推送实时到达。

## Context
必读：
1. `web/src/` 现有代码：`InspectorPanel`（占位要替换）、`Timeline`（静态占位要替换）、`stores/ui.ts`、`lib/queries.ts`、`lib/humanize.ts`（最小版，要扩事件 kind）。
2. `docs/frontend/api.md` §1.3（分页：`{run_id, events, next_after_seq, has_more}`，游标是扫描位置）、§1.4（SSE：`event: <kind>` / `id: <run_id>:<seq>` / `data: <事件 JSON>`；15s 心跳 `: ping`；断线续传 Last-Event-ID 或 `?after_seq=`；**换 run 必须带新 ?run_id=**；404 events_unavailable 走 JSON 信封不是 SSE 帧）。
3. 事件真实形状（`{seq, at, stage, kind, data}`）：单 run 可达 3758 条 / 44 种 kind（样本 `tmp/e2e-2602-02908v2-20260917` 最新 run）。kind 分布大头：`candidate_evaluated`/`call_started`/`call_finished`/`canonical_writeback`/`cache_miss`/`cache_write`/`candidate_selected`/`compile_*`；阶段骨架：`stage_started`/`stage_finished`（各 stage 一对）；杂项：`replay_notice`/`text_version`/`anchor_repair`/`artifact_bundle` 等。
4. `GET /documents/{did}/stage-state`（W02）：`stages[]` `{stage, status, at, duration_s, timing_source}`（status ∈ ok|failed|not_run；at 是开始时间）。事件里 `stage_started/stage_finished` 的 `data` 含补充信息。时间线以 stage-state 为**基线**（有精确 duration_s），事件流增量只用于 running 中阶段的"已进行 Xs"计数（本地时钟差值，`at` 字段算起）。
5. `docs/frontend/design-v2/DESIGN.md` §4.6 事件流（等宽、seq 右对齐、kind 徽标色、最多保留 N 条 + 溢出滚动、空态）、§4.7 时间线（7 段、进行中脉冲、失败红、未开始虚线、两行结构：段名+耗时，§8.3 变更记录：默认收起为 72–96px 单行展开 160px？以 §8.3 文字为准——**读设计文档定案**，不确定就在报告里列出你的解读）。humanize 文案口径见 §2.3/§4。
6. SSE 客户端注意：浏览器原生 `EventSource` 只支持 GET + 自动重连（重连会带 Last-Event-ID 头，正好）。**用原生 EventSource**，不用 fetch-stream 也不引库。错误处理：`onerror`（serve 挂了）→ 显示"连接断开，重试中"状态行（EventSource 自动重连，不需要手动 backoff）；readyState 区分 CONNECTING/OPEN/CLOSED。404 的情况：EventSource 对非 200 会触发 onerror 并停——**先发一次 fetch HEAD/GET 判断** run 存在（其实直接用分页接口首拉，404 → 整个事件区显示"该文档没有 run 归档"，不建 EventSource）。
7. 性能与 UX 红线：3758 条全渲染 DOM 会卡。事件面板是**尾部窗口**：默认保留最近 200 条 + "载入更早"按钮（翻 `?after_seq=` 分页，每次 500）；SSE 增量 prepend 时若用户正滚动在底部则自动跟随，否则显示"↓ 新事件"浮标（点击跳底）。kind 过滤下拉（全部/阶段/调用/缓存/候选/编译/其他 分组）——**过滤只影响显示，不动游标**（后端语义如此，前端也要自己过滤显示层）。
8. 运行中状态（为 W07 铺路但本任务只做只读侧）：文档详情 `stage_summary` 任何阶段 `running`（W02 stage-state 无 running 值——**用事件判断**：最新 run 的最后事件是 `stage_started` 无配对 `stage_finished`，或最后事件 stage 未到 report）→ 顶栏/视图栏徽标显示"翻译中"脉冲；同时 `useDocument` 的 refetchInterval 切到 2s（否则 5s/无自动）。列表页同理（`useDocuments` refetchInterval 3s，有任一 running 时）。这个判断逻辑抽成纯函数 `isRunLive(lastEvent)` 导出单测。

## Deliverables
1. `web/src/lib/events.ts`：SSE 管理（纯逻辑部分）：`parseSseId(id) → {runId, seq}`、`eventSourceUrl(did, runId, afterSeq)`、`isRunLive(events)`（最后事件 stage_started 未配对 finished，或最后 stage ≠ report/check 未配对——用简单可靠规则：最后一条事件的 kind 不是 stage_finished 或其 stage != report 就算 live，**注释写明规则与局限**）；事件分组 `kindGroup(kind)`（stage/call/cache/candidate/compile/other）。
2. `web/src/lib/queries.ts` 扩展：`useEvents(did, {afterSeq, limit, kind, stage})`（分页拉取，manual 模式用于"载入更早"）；`useStageState(did)`（含 refetchInterval 动态：live 2s 否则 0）。
3. `web/src/components/events/`：
   - `EventStreamPanel.tsx`：右侧面板进度视图的默认 tab（W10 会加段落 tab，本任务单 tab 即可，但面板结构留 tab 位）。首拉最新 200 条（`after_seq` 从尾部倒推：先拉 `?limit=2000` 取尾 200？**不行**——分页只支持正向。方案：先 `GET /events?limit=1` 不行……看后端：`after_seq` 必须。正解：`GET /events?limit=2000` 循环翻页到 has_more=false 太贵。**用 `?limit=2000` 单次拉取后取尾部 200 条**（单 run ≤3758 条，最多两次请求），报告里注明此权衡与 500 上限不符时（>2000 条/页）的翻页实现。SSE 增量 push 进窗口）。渲染：等宽行 `seq at stage kind + data 摘要`（data 摘要：智能截取 80 字符，JSON 压平显示键=值 2–3 个）。kind 徽标色按分组。虚拟化不做（200 条上限 DOM 可控）。
   - `useEventStream.ts`（hook）：封装 EventSource 生命周期（did/runId 变化重建；after_seq 起点=首拉尾部的 next_after_seq-1；unmount 关闭；断线状态行状态）。**处理心跳**：EventSource 的 `:` 注释行不会触发 onmessage——天然忽略，无需代码。
   - 时间线数据 hook `useTimelineStages(did)`：合并 stage-state（基线）+ 最新事件（live 阶段 + 本地已进行秒数，1s interval ticker）。
4. `web/src/components/shell/Timeline.tsx` 重写：真数据版。7 段横向条：完成段实心（pass 色）+ 耗时文本；running 段 run 色 + 脉冲 + 计数秒；failed 段 err 色；not_run 虚线灰。宽度按 duration 加权（无 duration 的 live 段给 2% 最小宽）。总时长 chip。点击段 → 跳转对应视图（parse→layout、translate→translate、apply→translate、build→progress?、check→check、review→check、report→progress——映射表写死并注释理由，检查/审校都在检查视图）。**data-od-id 保持** `timeline-stage-<stage>`。
5. `InspectorPanel.tsx`：进度视图下渲染 EventStreamPanel；其他视图保持现占位（W10/W12 接）。顶栏运行中徽标接入 `isRunLive`。
6. `humanize.ts` 扩展：44 种 kind 的中文短标签（≤4 字符优先，如 call_started→调用开始、cache_hit→缓存命中、candidate_evaluated→候选评估、compile_requests→编译请求…）；stage 已有；`formatDuration(s)`（<1s→"刚启动"、<60→"Xs"、<3600→"Xm Ys"、否则"Xh Ym"）。
7. 测试：
   - `events.test.ts`：parseSseId/isRunLive/kindGroup 纯函数（isRunLive 至少 4 断言：最后 stage_finished+report→false、stage_started 无配对→true、中间事件→true、空→false）。
   - `timeline-stages.test.ts`：stage-state + live 事件合并逻辑纯函数化后单测（ok/failed/not_run/live 各态 + 宽度加权 + 视图映射）。
   - `event-stream-panel.test.tsx`：渲染 200 条窗口/过滤显示/空态/断线状态行（mock EventSource——jsdom 无原生实现，测试注入 stub）。useEventStream 的 EventSource 交互（连接/关闭/重建）用 stub 类覆盖。
   - `humanize.test.ts`：kind 标签 + formatDuration 边界。
8. Playwright e2e `e2e/progress.spec.ts` 1 用例：真 serve（复用 preview.spec 的 webServer 配置）：打开已完成文档 `#/d/ccs3764-dyn/progress` → 事件面板 ≥10 行真实事件、时间线 7 段全 ok 且总时长 chip 有值、无 console error。SSE 实时增量（进行中 run）e2e 不做（W07 有真 job 后在 W15 集成验收覆盖）；但**手工模拟验证**（写进报告）：脚本向 `tmp/` 某测试 workdir 的最新 run events.jsonl 追加一行（注意合法 seq+换行结尾），浏览器 SSE 在 1s 内收到新行。
9. `web/README.md` 更新（事件流/时间线/SSE 说明）。

## Constraints
- 只改 `web/`。不改 Python / api.md / .plan。
- 不做：job 启动/取消（W07）、上传（W08）、段落编辑（W10）。时间线点击跳转的映射表允许简化但要有注释。
- EventSource 原生，禁库；SSE URL 注意走 Vite 代理路径（`/api/v1/...` 相对路径，EventSource 同源代理下可用）。
- 事件 data 原样保留在内存（面板显示摘要），detail 展开（点击行展开完整 JSON <pre>，最多 20 行高）。
- 3758 条 run 的面板首屏渲染 < 300ms（只渲染窗口 200 条，天然满足）。
- `pnpm typecheck && pnpm lint && pnpm test && pnpm build && pnpm e2e` 全绿。
- rg 限定路径 + timeout；不委派；不 commit/push；证据存 `web/tmp-smoke/`。

## Validation
```
cd web && pnpm typecheck && pnpm lint && pnpm test && pnpm build && pnpm e2e
```
手工冒烟（写进报告）：serve+dev 下打开 `#/d/ccs3764-dyn/progress` 截图（事件面板+时间线）；向某 workdir events.jsonl 追加事件验证 SSE 实时到达（脚本+时间戳证据）；`#/library` 徽标状态截图。`.pth` hidden 照旧处理。

## Report back
改动清单、首拉窗口策略说明与实测请求数、SSE 手工模拟证据（追加→到达延迟）、时间线映射表、四项+e2e 结果、截图路径、偏差清单、遗留风险。绑定 output 回传。最多两轮修复。
