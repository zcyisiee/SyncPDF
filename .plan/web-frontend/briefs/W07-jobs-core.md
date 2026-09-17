# Task
W07：job 核心——持久化调度、受控子进程、取消/重启、进程树测试

## Objective
在 feat/web-frontend（HEAD 4f64953，只读层 W01–W03 + 前端 W04–W06 已合入）实现 `docs/frontend/api.md` §3.4 的 jobs 核心：`POST /documents/{did}/jobs`、`GET /jobs/{jid}`、`POST /jobs/{jid}/cancel`、`GET /documents/{did}/jobs`。job 以**子进程**跑 `bdt run`（单入口 CLI），进程组取消、同文档串行、跨文档限流、状态持久化与服务重启恢复。这是"浏览器里点开始→翻译跑起来→可取消"的后端心脏。**本任务不做**：上传（W08）、profiles UI（W08）、compile/retranslate 动作（W09/W11）、job 事件并入 SSE 流（W08 决定）。

## Context
必读：
1. `docs/frontend/api.md` §3.4（**契约冻结**：202 响应形状、action 四值、profile 只接 id、409 document_busy、取消终止进程组、状态持久化）与 §2（错误信封）。
2. `.plan/web-frontend/EXECUTION.md` 纠偏第 5/8 条（job 持久化/串行/限流/进程组/锁/重启核实进程身份；HTTP 不收 shell 命令或密钥）。
3. `babeldoc_tools/serve/`（W01–W03 全部：store 路径约束、错误信封、routers 工厂模式、workdir 只读层）。新路由照 `routers/documents.py` 的工厂模式。
4. `bdt run` CLI 形状（`babeldoc_tools/__main__.py` 289–360 行 + `babeldoc_tools/run.py::run_pipeline`）：`--workdir`、位置参数 pdf、`--from <stage>`、`--markdown self`（自译不调外部命令）、`--translator`/`--reviewer`（shell 命令，**服务端从 profile 解析后才传**，客户端永远不给）、`--pages`、`--dual`、`--lang-in/out`、`--timeout`。stdout 末尾一行 JSON 信封（`{ok,data}|{ok:false,error}`）。
5. 关键已知行为：无 `--reviewer` 时 review 停在 `waiting_for_reviewer` 退出码 1（诚实失败，不是崩溃）；`--from translate --markdown self` 在已有 parse 产物的 workdir 上可无网络跑通 translate→apply→build（build 用本地 latex）；parse 需 MinerU（网络），**测试与冒烟都用 `--from translate` 起步的已有 workdir**。

## Deliverables
1. `babeldoc_tools/serve/jobs.py`：
   - `JobRecord`（pydantic）：`job_id`（`j_` + ULID 风格单调 id，用标准库 time+os.urandom 自实现，不引新依赖）、`did`、`action`、`status ∈ queued|running|succeeded|failed|canceled|interrupted`、`created_at/started_at/finished_at`（UTC ISO 毫秒 Z，与全站一致）、`from_stage`、`profile`、`pages`、`dual`、`run_id`（子进程 debug recorder 建的 run，事后从 `debug/runs` 最新目录抓取，可空）、`exit_code`、`envelope`（bdt run 的 stdout 信封原文，可空）、`error_code/error_message`（失败摘要）、`pid/pgid`（运行中）、`cancel_requested_at`。
   - `JobRegistry`：持久化在 `<store_base>/.bdt-serve/jobs.jsonl`（append-only 生命周期事件 `job_queued/started/finished/canceled/failed/interrupted`）+ `<store_base>/.bdt-serve/jobs/<jid>.json`（当前状态快照，原子写 tmp+rename）。`store_base` 由 DocumentStore 暴露（root 模式 = root；workdir 模式 = workdir；给 store 加只读属性，不改既有行为）。
   - 内存调度：`asyncio.Lock` 保护的活动表；同文档最多 1 个活动 job（第二个 POST → 409 `document_busy`，detail 带现有 job_id）；全局并发上限 2（超出排队 `queued`，先到先得）；取消后锁立即让给排队者。
   - 重启恢复（`JobRegistry.load()`）：扫 jobs/*.json，`running` 且 pid 不存活（`os.kill(pid,0)` + `/proc` 或 ps 校验进程组身份，macOS 用 `ps -o lstart= -p`；**不凭孤立 pid 发信号**）→ 置 `interrupted` + 记录 `interrupted_reason: server_restart`；`queued` → 置 `interrupted`（不自动重跑收费调用）。
2. `babeldoc_tools/serve/runner.py`：
   - `spawn_job(record, argv) -> subprocess.Popen`：`start_new_session=True`（自有进程组），stdout/stderr PIPE，cwd=workdir。argv 由**服务端构造**：`[sys.executable, '-m', 'babeldoc_tools', 'run', '--workdir', <did路径>, ...]`——注意用 `sys.executable -m babeldoc_tools` 而不是裸 `bdt`（PATH 不可靠；这是模块入口不是第二 console 入口，`tests/test_single_entry.py` 不会拦）。
   - 监控任务：`asyncio.create_task` + `await asyncio.to_thread(proc.wait)`；读 stdout 尾行解析信封（子进程可能输出多行日志到 stderr——stdout 仍单行信封，逐行找最后一个 JSON 行，解析失败 → failed + `envelope_unparsed`）；超时（run 默认 3600s，check 600s；超时 = 取消路径 + `timed_out` 标记）；退出码 0 → succeeded，非 0 → failed（error_code 从信封取，无信封用 `exit_<code>`）。
   - 取消：`cancel(jid)` 幂等。`os.killpg(pgid, SIGTERM)` → 5s 宽限 → `SIGKILL`；等待 `proc.wait()` 返回后才置 canceled/释放文档槽（**锁持到退出**）；pid 已死但 wait 未返回的竞态要处理（等待监控任务收尾）。对 `queued` job 取消 → 直接 canceled。对已终态 job 取消 → 幂等返回当前状态（200，不报错）。
   - **进程树验证是本任务的验收核心**（EXECUTION.md 第 5 条）：测试里 stub translator 脚本要 spawn 一个长命孙子进程（`sleep 300 &`），取消后断言孙子也死了（ps 扫或 pid 存活检查）。
3. `babeldoc_tools/serve/profiles.py`（最小版，W08 扩展点）：
   - `resolve(profile_id) -> Profile | None`：读 `<store_base>/.bdt-serve/profiles.json`（`{id: {translator: "...", reviewer: "..."}}`）；env 覆盖 `BDT_PROFILE_<ID_UPPER>_TRANSLATOR/_REVIEWER`（测试用）。返回命令字符串只喂给 argv 构造，**永不回传给客户端**（响应里 profile 只回 id）。
   - `POST jobs` 校验：`profile` 必填（§3.4 示例有 profile；缺省 → 422）、形状 `[a-z0-9-]{1,64}`、未知 → 422 `unknown_profile`（detail 列出可用 id——注意只列 id 不列命令）。
4. `babeldoc_tools/serve/routers/jobs.py`：
   - `POST /documents/{did}/jobs`：body 校验（action ∈ run|check；`from` 只对 run 有效且 ∈ 7 阶段；`pages` 字符串如 "1-3,5"；retranslate/compile → 422 `action_not_available` + detail `{"phase": "W09/W11"}`，诚实不冒充）；409 document_busy；202 返回 `{job_id, status: "queued", action}` + `Location` 头 `/api/v1/jobs/{jid}`。
   - `GET /jobs/{jid}`：全字段（envelope 可截断到 4KB 存）。
   - `POST /jobs/{jid}/cancel`：202（已运行）或 200（已终态幂等）；不存在的 jid → 404 `job_not_found`。
   - `GET /documents/{did}/jobs`：该文档全部 job，新→旧，`?status=` 过滤。
   - app.py 接线 + `_TOOL_ERROR_STATUS` 增补（document_busy→409、job_not_found→404、unknown_profile/action_not_available→422）。
   - OpenAPI 守卫：W02 的 `methods == {"get"}` 断言会破——**更新该守卫测试**允许 post（jobs 路由白名单化：只允许 `/jobs` 与 `/documents/{did}/jobs` 两条 POST，其余仍必须 get；这是安全边界，不是回归）。
5. `tests/test_serve_jobs.py`（自包含 fixture，不起真网络）：
   - stub translator/reviewer 脚本（shell echo 合法 markdown/JSON）+ fixture workdir（最小 parse 产物：可直接用现有测试 fixture 构造器或复制精简产物集；`--from translate --markdown self` 甚至不需要 translator——但进程树测试需要真调 translator 的路径，用 `--from translate` + stub translator 覆盖）。
   - 用例（≥18）：成功路径全状态流转 queued→running→succeeded + run_id 抓取 + envelope 落盘；同文档 409；跨文档并发 2 上限 + 第 3 个排队 + 完成后出队；取消 running（进程组：stub spawn 孙子 sleep，断言孙死）+ 锁释放 + 幂等取消终态；取消 queued；超时路径（短 timeout stub sleep）；失败路径（stub exit 1 + 信封 error_code 透传）；无信封路径（stdout 乱写）；重启恢复（构造 registry：running+活 pid（当前测试进程）→ 保留 running？**不**——活 pid 但不是本服务 spawn 的也要核实身份：`<store_base>/.bdt-serve/jobs/<jid>.json` 记 `spawn_marker`（argv hash + boot_id = serve 进程启动时间），恢复时 boot_id 不匹配 → interrupted；同 boot_id 且 pid 活 → 保留 running）；排队者不自动跑（server restart）；OpenAPI post 白名单；profiles 解析（文件 + env 覆盖 + 未知 422）；action_not_available。
6. `docs/frontend/api.md` §3.4 状态行改"已实现（W07：run/check；compile→W09、retranslate→W11）"；`GET /documents/{did}/jobs` 补 `?status=` 说明。
7. README/无需前端改动（W08 才接 UI）。**不改 web/**。

## Constraints
- 只新增/修改 `babeldoc_tools/serve/`（+ `tests/`、`docs/frontend/api.md` 状态行、store.py 加 store_base 只读属性）。禁改 run.py/translate.py 核心与 `babeldoc/`。
- 子进程 argv 只由服务端构造；客户端输入只允许：action/from/pages/dual/profile/feedback?（retranslate 的，W11 前不可用）。**不接受** translator/reviewer/timeout/shell 字符串（收到 → 422 `forbidden_field`）。
- 不自动重跑 interrupted/failed 的 job（前端显式重试 = 发新 job）。
- job 事件并入 SSE 流本任务不做（jobs.jsonl 已持久化；W08 决定流形状）。但 `GET /jobs/{jid}` 要能轮询出全状态。
- macOS + Linux 双路径的 killpg 用 `os.killpg`（两个平台都有）；不要依赖 /proc。
- 测试禁全仓递归 grep；单测不得依赖网络、真实 MinerU 或真实模型。
- `PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest <serve 全套 + single_entry> -q --basetemp=tmp/pytest-W07-$(date +%Y%m%d-%H%M%S)` 全绿；ruff；`git diff --check`。
- 不委派、不 commit/push、不改 .plan。

## Validation
```
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest tests/test_serve_jobs.py tests/test_serve_app.py tests/test_serve_store.py tests/test_serve_documents.py tests/test_serve_events.py tests/test_serve_artifacts.py tests/test_single_entry.py -q --basetemp="tmp/pytest-W07-$(date +%Y%m%d-%H%M%S)"
.venv/bin/ruff check babeldoc_tools/serve tests/test_serve_jobs.py
git diff --check
```
真实冒烟（写进报告）：`bdt serve --workdir tmp/ccs3764-dyn`（.pth hidden 照旧先修）+ profiles.json（stub translator/reviewer 脚本放 /tmp）→ `curl POST jobs`（action=run from=translate）→ 轮询 GET /jobs 到 succeeded（或诚实失败并展示 error_code）→ 期间再 POST → 409 → cancel 一个长任务（stub sleep 版 profile）验证 202→canceled + 进程组清干净（ps 验证）。注意冒烟会真实改动 `tmp/ccs3764-dyn` 产物（translate 重跑会改 agent/*.md）——**先 `cp -R tmp/ccs3764-dyn tmp/w07-smoke-wd` 用副本跑**，原 workdir 不动。

## Report back
改动清单、状态机图（文字版）、进程树取消证据（ps 输出摘录）、真实冒烟时序（POST→轮询→终态 envelope 摘要）、恢复逻辑说明（boot_id 方案）、四项验证结果、偏差清单、遗留风险。绑定 output 回传。最多两轮修复。
