# ieeTranslater Web 前端 — 架构与协议初稿（v0.1，历史）

> ⚠️ **历史协议：不要据本文实现。** 本文是 2026-09 的初稿，其中
> `/projects/{pid}/documents/{did}` 前缀、模块清单与部分端点形状已被修订。
>
> - 现行 HTTP 契约：`docs/frontend/api.md`（+ 运行中 `bdt serve` 的 `/openapi.json`）
> - 范围 / 顺序 / 验收：`.plan/web-frontend/PLAN.md` 与 `.plan/web-frontend/EXECUTION.md`
>   （EXECUTION.md 优先于 PLAN.md 与本文的旧结论）
>
> 本文只保留为决策记录与设计背景。

## 快速开始（现行）

```bash
# 装 web extra（不装也能用 parse/translate/.../run）
uv pip install --python .venv/bin/python "fastapi>=0.115" "uvicorn[standard]>=0.32" "python-multipart>=0.0.9"

# 启动只读服务（stdout 打印真实端口；docs 页在 /docs）
PATH="$PWD/.venv/bin:$PATH" bdt serve --root tmp            # 枚举 tmp/<did>/
PATH="$PWD/.venv/bin:$PATH" bdt serve --workdir tmp/ccs3764-dyn   # 只公开这一个文档
curl -sS http://127.0.0.1:<port>/api/v1/health
```

完整约定见 `docs/frontend/api.md`。

---

> 分支 `feat/web-frontend`。本文是与用户多轮对齐后的**决策记录 + 接口协议初稿**，
> 供后续迭代修订。设计稿见 OpenDesign 项目「ieeTranslater 前端交互设计」。
> 已决策项标 ✅，待定项标 ❔。

## 0. 一句话

用户在浏览器上传 PDF → 选模型/词表/页范围 → 云端跑 `parse→translate→apply→build→check→report`
→ 前端实时显示段落级进度、事件流、时间线 → 用户在译文 PDF 上点段落改译文/拖 bbox/AI 重译
→ 保存即自动重编译（工作版）→ 满意后「存为版本」/「归档」下载。

## 1. 已决策

| # | 决策 | 结论 |
|---|---|---|
| 1 | 前端栈 | ✅ React 18 + TypeScript + Vite；UI：shadcn/ui + Tailwind（主题按 kami 纸感 token 定制）；状态：TanStack Query（服务端状态）+ Zustand（编辑器/UI 状态）；PDF：pdf.js 矢量渲染 + SVG 叠加层 |
| 2 | 后端 | ✅ FastAPI，以 **`bdt serve`** 子命令暴露（保持单入口）；OpenAPI → `openapi-typescript` 生成 `web/src/api/schema.d.ts`，接口单一事实来源 |
| 3 | 部署 | ✅ v1 单用户本地版；所有资源 URL 带 `/projects/{pid}/documents/{did}` 维度，不做登录；上云时加认证层不改协议 |
| 4 | 预览 | ✅ pdf.js 矢量渲染 + 段落/版面 bbox SVG 叠加层；三模式：原文 / 译文 / 左右对照（同步滚动缩放、跨侧段落高亮） |
| 5 | 视觉 | ✅ kami 纸感：底色 `#f5f4ed`、单一墨蓝强调 `#1B365D`、衬线标题；**事件流/时间线/表格/状态用中性无衬线+等宽工程风**；状态色 running=琥珀 `#B7791F`、pass=墨绿 `#2F6B4F`、error=朱红 `#A63D2F`、preview=墨蓝虚线 |
| 6 | 设计稿 | ✅ OpenDesign 本机生成（Claude Code agent），5 屏 |
| 7 | 编辑能力 v1 | ✅ 手改译文 + 排版覆盖（**预览上直接拖拉 bbox** + 数值面板）+ 单段 AI 重译（候选并排，点采用才进草稿） |
| 8 | 进度粒度 | ✅ 段落级：`bdt translate` 边读 translator stdout 边解析已闭合段落 id，发 `paragraph_done`；预览逐段变色 |
| 9 | 导航 | ✅ 两级：最左 56px 图标栏（文件库/词表/设置）+ 选中文档后 220px 视图栏（进度/识别/翻译/检查/归档） |
| 10 | 模型选择 | ✅ 后端 provider profiles 注册表（`~/.config/bdt/providers.toml`），前端只见名字；translator 子进程协议不变 |
| 11 | 词表 | ✅ 全局 + 文档级两层「术语→译法」表，翻译时注入 prompt；前端 CRUD |
| 12 | 重编译范围 | ✅ 目标：只重排受影响页（`scope=page`，结果标「预览」角标；溢出自动升级全量并提示）；「存为版本」前必做全量。**v1 后端先实现全量，接口已定 scope 字段** |
| 13 | 保存策略 | ✅ 保存自动触发编译（防抖 1.5s），只更新「工作版」；「存为版本」/归档才生成 v1,v2… |
| 14 | 版本 | ✅ 每版存 `translated.md + overrides.json + mono/dual PDF + check.json`；可查看/回滚 |
| 15 | 并发 | ✅ 每文档至多 1 个活动任务（冲突操作禁用、可取消）；跨文档并行，全局上限可配 |
| 16 | 上传流程 | ✅ 上传 → 配置弹层（模型/词表/页范围/dual）→ 一口气跑完全链路 |
| 17 | 任务中编辑 | ✅ 只读；编辑按钮灰掉并提示；可取消任务 |
| 18 | 识别视图 | ✅ v1 只读（层开关、属性、图例），「修正识别」预留 v2 接口 |
| 19 | 时间线 | ✅ 7 阶段按真实耗时比例分段；当前阶段秒表；translate 段内嵌 n/N；总用时；历史平均淡色「预计」；点击段过滤事件流；error 打红点 |
| 20 | 失败 | ✅ 阶段级错误卡：人话 + 动作（从 X 重试 / 换模型重试 / 看日志）= `POST /jobs {from: X}`；check 不通过不是失败，在「检查」视图列问题并可跳到段落编辑 |
| 21 | 事件流 | ✅ 默认人话摘要（kind→文案模板表在前端 `web/src/events/humanize.ts`），可展开原始 JSON，按阶段/级别过滤 |
| 22 | 仓库 | ✅ 同仓 `web/`（pnpm）；`pnpm build` 输出到 `babeldoc_tools/web_dist/`（入 git）；`bdt serve` 托管；开发时 Vite 代理 `/api` |
| 23 | 流式段落契约 | ✅ translator 协议**不变**：持续往 stdout 写纯文本译文片段；`bdt translate` 边读边扫段落锚点，出现下一锚点即视上一段完成 → `paragraph_done`。wrapper 脚本负责把各 CLI 流式事件（agy `stream-json` / pi `--mode json`）拆成纯文本增量；不支持流式的命令天然兼容（进度一次跳满） |
| 24 | 开发顺序 | ✅ ① `bdt serve` 骨架 + 只读接口（文档/几何/事件 SSE）→ ② `web/` 脚手架 + 三栏壳 + pdf.js 预览 + 事件流/时间线（接真数据）→ ③ jobs（run/cancel）+ 上传 + 配置弹层 → ④ 草稿/自动编译/bbox 拖拽 → ⑤ 候选重译/版本/归档 → ⑥ 词表/流式段落进度。①② 可并行（前端先 mock） |
| 25 | 旧 debug 查看器 | ✅ 保留，直到新前端覆盖识别/编译/检查/事件能力后删除；届时 `bdt debug` 成为 `bdt serve --open <workdir>` 别名 |
| 26 | 词表命中 | ✅ 统计术语在原文 `document.md` 出现次数（解析后即可算）；check 阶段「译文未按词表」警告留 v2 |
| 27 | 验收底线 | ✅ Vitest（humanize / 坐标换算 / store）+ Playwright 冒烟（`bdt serve` 指向 `tmp/` 真实 workdir，截图落 `tmp/`）+ `tsc --noEmit` + eslint；Python 侧 pytest 覆盖 serve 路由 |

## 2. 前端结构

```
web/
  src/
    app/            路由（/library, /glossary, /settings, /d/:did/{progress,layout,translate,check,archive}）
    api/            openapi 生成类型 + fetch 客户端 + SSE 订阅
    store/          Zustand：editor（选中段落/草稿/拖拽中 bbox）、ui（面板宽度/预览模式）
    features/
      library/      上传、文档列表、配置弹层
      workbench/    三栏壳：视图栏 · 预览 · 右侧面板 · 底部时间线
      preview/      PdfCanvas（pdf.js）· OverlayLayer（SVG bbox，支持拖拽/缩放手柄）· 对照同步
      progress/     Timeline · 阶段状态 · 段落进度着色
      events/       EventFeed · humanize.ts（kind→文案）
      translate/    ParagraphPanel（原文/译文编辑/排版参数/AI 候选）
      check/        问题列表 → 跳转段落
      archive/      版本列表 · 下载 · 归档
      glossary/     术语表 CRUD
    components/ui/  shadcn 组件（kami 主题）
  tailwind.config.ts  kami token
```

**核心不变量**：前端不持有业务真值。所有可编辑内容（译文、overrides）以后端草稿为准；前端乐观更新后以 SSE 回推的 `draft_saved` 事件确认。

## 3. 后端：`bdt serve`

```
bdt serve [--host 127.0.0.1] [--port 0] [--root ~/.local/share/bdt] [--open] [--dev]
```

- `babeldoc_tools/serve/`：`app.py`（FastAPI 装配）、`routers/{documents,jobs,drafts,versions,glossary,providers,events}.py`、`jobs.py`（任务队列，每文档一把锁 = 复用 `debug/write.lock` 语义）、`store.py`（文件系统布局，见 §5）。
- 任务执行：在**子进程**中调用现有 `run.run_pipeline` / `translate.retranslate_blocks` / `layout.build_pdf` 等 Python 函数（不 shell 出 `bdt`，但走同一实现），`debug_recorder` 事件直接进 events.jsonl 并广播到 SSE。
- 单入口守卫：`tests/test_single_entry.py` 需允许 `babeldoc_tools/serve/` 包，因为它仍只经 `bdt serve` 暴露。

## 4. HTTP/SSE 协议（初稿）

前缀 `/api/v1`。单用户版 `pid` 固定 `default`，路径仍保留。错误统一 `{ "error": { "code", "message", "detail"? } }`。

### 4.1 文档

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/projects/{pid}/documents` | multipart 上传 PDF → `{id, title, pages, status:"uploaded"}` |
| GET | `/projects/{pid}/documents?status=active\|archived` | 列表（含 `active_job`、`latest_version`、`check_verdict`） |
| GET | `/projects/{pid}/documents/{did}` | 详情：`stages{}`（每阶段状态/起止时间）、`paragraph_count`、`translated_count`、`draft.dirty_count` |
| DELETE | `/projects/{pid}/documents/{did}` | 删除 |
| POST | `/…/{did}/archive` | 标记归档（要求无 dirty 草稿且存在版本） |
| GET | `/…/{did}/files/{kind}` | `source.pdf` / `mono.pdf` / `dual.pdf` / `working/mono.pdf` / `versions/{v}/mono.pdf`（Range 支持，供 pdf.js） |
| GET | `/…/{did}/geometry?target=working\|v3&page=1` | 段落 bbox（原文页坐标 + 译文页坐标，含 `paragraph_id`、`status: translated\|pending\|preview\|edited`）|
| GET | `/…/{did}/layout?page=1` | 识别层：layout 块/段落/字符/行内公式（对应 snapshots/parse/*） |
| GET | `/…/{did}/paragraphs/{para_id}` | `{source, translation, draft?, overrides?, candidates[], check_issues[]}` |

### 4.2 任务（用户命令 → 后端动作的唯一通道）

```
POST /…/{did}/jobs
{
  "action": "run" | "retranslate" | "compile" | "check",
  "from":   "parse"|"translate"|"apply"|"build"|"check"|"review"|"report",  // run 用
  "scope":  "full" | "page",          // compile 用；v1 后端接受 page 但降级为 full 并在 job.notes 注明
  "pages":  [3,4],                    // scope=page
  "paragraph_ids": ["P05-002"],       // retranslate 用
  "feedback": "补全句末成分",           // retranslate 用
  "config": { "provider": "agy/gemini-flash", "glossary_ids": ["g1"], "page_range": "1-8", "dual": true }
}
→ 201 { "job_id", "status": "queued" }   | 409 { error.code: "job_active", active_job_id }
```

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/…/{did}/jobs/{jid}` | `{status: queued\|running\|succeeded\|failed\|canceled, stage, started_at, finished_at, error?{code,message,retry_from,suggestions[]}, notes[]}` |
| POST | `/…/{did}/jobs/{jid}/cancel` | 取消（SIGTERM 子进程，阶段标 canceled） |
| GET | `/…/{did}/jobs?limit=20` | 历史任务（供时间线历史平均） |

**动作语义**
- `run`：等价 `bdt run --from`；`from` 缺省 = parse。
- `retranslate`：等价 `bdt translate --ids --feedback`；结果**不写回**，进入 `candidates`（见 4.3）。
- `compile`：把草稿（译文改动 + overrides）执行 `apply → build → check`，产出**工作版**；`scope=page` 为增量目标。
- `check`：只跑 check。

### 4.3 草稿与候选（编辑闭环）

| 方法 | 路径 | 说明 |
|---|---|---|
| PUT | `/…/{did}/draft/paragraphs/{para_id}` | `{translation?: string, overrides?: {scale_cap?, font_scale?, line_skip?, box_scale?, box?: [x,y,x2,y2]}}` → `{dirty_count}`；服务端校验占位符/锚点（复用 apply 校验），失败 422 带 `violations[]`。**保存即触发防抖 compile（服务端防抖，1.5s）** |
| DELETE | `/…/{did}/draft/paragraphs/{para_id}` | 丢弃该段草稿 |
| GET | `/…/{did}/draft` | `{dirty: [{para_id, fields, at}], compiling: bool, last_compile_job}` |
| POST | `/…/{did}/draft/discard` | 丢弃全部草稿，回到最新版本 |
| POST | `/…/{did}/paragraphs/{para_id}/candidates/{cid}/accept` | 采用候选 → 写入草稿 |
| DELETE | `/…/{did}/paragraphs/{para_id}/candidates/{cid}` | 丢弃候选 |

> **已实现形状（W11，以 `docs/frontend/api.md` §3.6 为准）**：生成是
> `POST /documents/{did}/paragraphs/{pid}/retranslate`（体里只有 `profile` id，job `action=retranslate`，
> 跑在隔离副本里，只写 `<did>/.bdt-serve/candidates.json`）；列表是
> `GET /documents/{did}/paragraphs/{pid}/candidates`；采用/拒绝是 `POST …/candidates/{cid}/adopt|reject`
> （采用返回新草稿，拒绝只改状态）。本节的 `accept`/`DELETE` 命名与 `PUT /draft/paragraphs/{id}`
> 都被 §3.3/§3.6 的 `PATCH /draft` + `adopt` 取代：草稿只有一个写入口，候选只在采用时进去。

bbox 拖拽：前端在 SVG 层拖动手柄 → 换算为 PDF 坐标 → `PUT draft … {overrides:{box}}`。拖拽过程中本地即时绘制；松手才发请求。

### 4.4 版本

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/…/{did}/versions` | `{label?}` → 需 `draft.dirty=0` 且工作版为全量编译；否则 409 `needs_full_compile`（前端据此先发 compile full） |
| GET | `/…/{did}/versions` | `[{v, label, created_at, check_verdict, files:{mono,dual,md,report}}]` |
| POST | `/…/{did}/versions/{v}/restore` | 用该版本 translated.md + overrides 重置草稿并编译 |

### 4.5 事件流（SSE）

`GET /…/{did}/events?since_seq=0` → `text/event-stream`，每条 `event: <kind>`、`id: <seq>`、`data: {seq, at, stage, kind, data}`。

- 直接复用 `debug_recorder` 的 events.jsonl；断线用 `Last-Event-ID` 续。
- **新增 kind**（后端需实现）：
  - `paragraph_done {paragraph_id, index, total}` — translate 阶段流式解析
  - `draft_saved {paragraph_id, dirty_count}` / `draft_discarded`
  - `compile_scope {requested:"page", effective:"full", reason:"overflow"|"unsupported"}`
  - `job_queued/job_started/job_finished/job_canceled {job_id, action}`
  - `version_created {v}`
- 全局流 `GET /projects/{pid}/events`：文档级摘要（供文件库列表刷新）。

### 4.6 词表与 provider

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | `/projects/{pid}/glossaries` | `{id, name, scope:"global"\|"document", document_id?, entry_count}` |
| GET/PUT | `/projects/{pid}/glossaries/{gid}/entries` | 整表读写 `[{term, translation, note?, case_sensitive?}]`；支持 CSV 导入导出 `?format=csv` |
| GET | `/providers` | `[{id:"agy/gemini-flash", label, vendor, tier, default:bool}]`（来自 providers.toml） |

`providers.toml` 形态：
```toml
[profiles."agy/gemini-flash"]
label = "Gemini Flash（快）"
command = "scripts/agy-translator.sh"
env = { AGY_MODEL = "gemini-3.8-flash-low" }
[profiles."pi/deepseek"]
label = "DeepSeek V4 Pro"
command = "scripts/pi-translator.sh"
env = { PI_MODEL = "deepseek/deepseek-v4-pro:low" }
```

## 5. 存储布局（每文档一个 workdir，兼容现有）

```
<root>/projects/default/documents/<did>/
  source.pdf  meta.json
  agent/              ← 现有 workdir 结构（document.md / translated.md / overrides.json / run_state.json …）
  debug/              ← 现有 recorder 事件与快照（events.jsonl 即 SSE 源）
  working/            ← 最近一次 compile 产物（mono/dual/check.json/geometry.json）
  versions/v1/ …      ← translated.md overrides.json mono.pdf dual.pdf check.json report.md
  draft.json          ← {paragraphs:{P05-002:{translation, overrides, at}}}
  candidates.json     ← 采用前的一切都在这里：{next_id, items:[{id, pid, source, baseline_target,
                          candidate_target, status: pending|adopted|rejected, model_label, job_id,
                          created_at, adopted_at}]}（W11；采用前的候选**不**进 translated.md）
```

## 6. 交互流程（关键路径）

1. **上传**：拖入 PDF → 立即建文档并展示首页缩略 → 配置弹层（provider、词表多选、页范围、dual）→「开始翻译」→ `POST jobs {action:run}` → 自动进入该文档「进度」视图。
2. **进度**：预览显示原文 PDF，段落 bbox 灰；`paragraph_done` 到达则该段变淡墨蓝并显示 n/N；build 完成后预览切到工作版译文；右侧事件流滚动；底部时间线。
3. **编辑**：任务结束后进入「翻译」视图；点段 → 右侧面板；改译文 blur 即 PUT；拖 bbox 松手即 PUT；1.5s 后 `compile` 自动跑，顶部条「正在重排（预览）… 3 处改动」；完成后预览刷新（保持滚动位置与选中）。
4. **AI 重译**：选 profile（或用上次）→「AI 重译」→ job retranslate（隔离副本，不写正文）→ 候选卡并排
   （原文 / 当前译文 / 候选译文）→「采用」进草稿（触发防抖编译）或「拒绝」（候选折起）。
5. **检查**：`check.json` 问题按 P0/P1/P2 分组 → 点问题 → 跳到翻译视图对应段并高亮。
6. **归档**：「存为版本」（自动先全量 compile）→ 版本列表 → 下载 → 「归档」→ 文件库分组到已归档。

## 7. 待定（下一轮）

- ❔ 页级增量 build 的实现路径与溢出判定（后端专题）。
- ❔ 对照模式跨侧高亮的几何来源：原文侧用 `snapshots/parse/paragraphs.json`，译文侧用 `typesetting_geometry.json`，同一 `paragraph_id` 关联 — 需确认 id 在两处一致。
- ❔ 词表注入 prompt 的位置与格式（与 `translate.py` 的 prompt 模板对齐）。
- ❔ 设计 token 最终值（等设计稿出来后定）。
