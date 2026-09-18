# Task
W08：上传与 profiles——POST /documents、GET/PUT /profiles、envelope 脱敏、前端上传/配置/开始/取消

## Objective
在 feat/web-frontend（HEAD 451d043，W07 job 核心已合入）打通"拖 PDF 进浏览器 → 选配置 → 开始翻译 → 取消"的完整链路：
1. `POST /documents`（multipart 上传 PDF → 建 did，`source.pdf` 落 workdir 根）；
2. `GET /profiles` / `PUT /profiles`（profile 管理，前端只见 id/label，命令永不回传）；
3. **envelope 脱敏**（W07 遗留风险 #1：`GET /jobs/{jid}` 的 envelope 会带出 profile 命令与 debug token）；
4. 前端：文件库拖拽上传（启用 W04 的 disabled 按钮/拖放区）、新文档配置表单（profile/页范围/dual）、开始/取消按钮与 job 状态轮询。

完成后用户可以：拖一个新 PDF 进文件库 → 出现新文档 → 点"开始翻译" → job 跑起来（事件流/时间线随 run 归档出现而活） → 可取消。

## Context
必读：
1. `docs/frontend/api.md` §3.5（POST /documents、GET/PUT /profiles 行）+ §3.4（jobs 契约，已实现）+ §2（错误信封）。
2. W07 代码：`serve/{jobs,runner,profiles,routers/jobs}.py`（JobRunner.submit/cancel、build_job_argv、resolve_profile）、`tests/test_serve_jobs.py`（fixture 模式：stub profile + 副本 workdir）。
3. W04–W06 前端：`web/src/screens/LibraryScreen.tsx`（上传按钮 disabled + tooltip "W08 接入"）、`stores/ui.ts`、`lib/{api,queries}.ts`、`WorkbenchScreen.tsx`（事件流/时间线已吃真数据）。
4. 关键事实：
   - `bdt run <pdf 位置参数> --workdir ... --from parse` 是全新文档的起点；W07 的 `build_job_argv` 目前只在 `--from translate` 及之后省略 pdf——**from=parse 时必须带 `<workdir>/source.pdf`**（扩展 argv 构造，保持服务端唯一来源）。
   - parse 的 MinerU token 从环境变量 `MINERU_API_TOKEN` 读（`parse.py:45`），子进程继承 serve 环境，**客户端永不传**；前端在开始翻译前无法预知 token 是否存在——文档里写清楚"由 serve 进程环境提供"。
   - `source.pdf` 已在 W03 产物白名单（kind=source，根级文件），上传后 W05 的源模式预览立即可用。
   - store 的 did 形状约束见 `serve/store.py`（did 必须单段、不能 `.` 开头——`.bdt-serve` 就是被这条挡住的）；新 did 建议格式 `up-<slug>-<yyyymmdd-hhmmss>`，slug 取 PDF 文件名（仅 `[a-z0-9-]`，截断 40 字符），冲突加序号。
   - W06 的 live 检测吃 events.jsonl：新文档没有 run 前事件面板是空态（正常）；job 开始后 run 归档出现，事件/时间线自然活起来。**前端 job 状态用轮询 `GET /documents/{did}/jobs`（2s，有活动 job 时）**，不扩 SSE（jobs.jsonl 并入 SSE 留给 W14 决定）。
   - 双页对照（dual）：`--dual` flag 已在 argv 构造支持（W07 `build_job_argv` 有 `--dual`）。

## Deliverables

### 后端（`babeldoc_tools/serve/`）
1. `serve/uploads.py`：`save_upload(store, file) -> did`：
   - 校验：`%PDF-` 魔数（读前 5 字节）、大小上限 200MB（超限 → 413 `file_too_large`，detail 带上限与实际大小）、文件名非空；
   - did 生成（上述格式，`store.resolve` 形状预检 + 同名冲突 `-2`/`-3` 递增）；
   - 原子落盘：写 `<root>/<did>/source.pdf`（tmp 文件 + rename）；**不建任何空 agent/ 骨架**（不造假"已有数据"的文档状态）；
   - 上传后 W02 列表自然出现新文档（available 部分缺 → 列表已容忍）。
2. `routers/documents.py` 加 `POST /documents`（multipart，`python-multipart` 已是 web extra）：201 `{did, bytes, source: "source.pdf"}`；写端点守卫白名单加这一条。
3. `routers/profiles.py`：`GET /profiles` → `[{id, label, has_translator, has_reviewer}]`（label = id 人性化或 profiles.json 可选 label 字段；**命令字符串永不出现**）；`PUT /profiles` body `{id, label?, translator_script?, reviewer_script?}`：
   - `translator_script`/`reviewer_script` 是**脚本路径引用**不是命令字符串：必须匹配 `^scripts/[A-Za-z0-9._/-]+$` 且解析后存在于 `<repo>/scripts/` 或 `<store_base>/scripts/` 白名单目录内（符号链接越界拒绝）——含 shell 元字符（空格引号分号 `$` 等）直接 422 `forbidden_field`；
   - 写回 `.bdt-serve/profiles.json`（原子写）；新建/更新/删除（script 字段置 null = 删该字段，整个 id 空了 = 删 profile）；
   - 200 回 `{id, ...}`（同 GET 形状，无命令）。
4. **envelope 脱敏**（W07 风险 #1）：`runner.py` 存 envelope 前过 `sanitize_envelope(text, profile) -> str`：
   - `data.config.translator/reviewer` 的命令字符串 → 替换为 profile id（`"<profile:echo-t>"`）；
   - `data.debug.url` → 去掉（保留 `run_id`/`manifest`，url 带 token）；
   - 实现为对 JSON 文本的结构化改写（解析 → 改 dict → dumps；解析失败时兜底正则替换命令子串 + 截断），纯函数 + 单测（含含密钥样式命令如 `sk-xxx` 的替换验证）。
   - **历史记录不回填**（已有的 jobs.jsonl 不动，只管新 job）。
5. `docs/frontend/api.md`：§3.5 两行标已实现（W08）；§3.4 envelope 字段说明补一句"已脱敏（translator/reviewer → profile id，debug.url 移除）"；§2 错误码表补 `413 file_too_large`、`422 script_path_forbidden`。

### 前端（`web/src/`）
6. `lib/queries.ts`：`useProfiles()`、`useJobs(did)`（轮询：有 queued/running 时 2s，否则 30s）、`useUploadMutation()`、`useCreateJobMutation(did)`、`useCancelJobMutation(did)`。
7. `screens/LibraryScreen.tsx`：启用上传（拖放区 + 文件选择，multiple 但逐个串行 POST）；上传中行内进度（spinner + 文件名，不搞假百分比）；失败显示错误卡（413 提示文件过大）；成功后卡片列表刷新，**不自动跳转**（用户自己点进去）。
8. `components/jobs/StartJobCard.tsx`（工作台进度视图顶部，仅当文档无活动 job 且有 `source.pdf` 或已有产物时显示）：
   - 表单：profile 下拉（GET /profiles）、页范围输入（占位 "1-3,5 全部留空"）、dual 开关；
   - "开始翻译" → POST job（action=run，from：**自动判断**——有 parse 产物（详情 available）则默认 translate，无则 parse；给个 from 高级下拉允许改）→ 成功后进入轮询态；
   - 提交前确认：from=parse 时提示"首次翻译需要 MinerU（由服务环境提供 token），耗时较长"。
9. `components/jobs/ActiveJobCard.tsx`（有活动 job 时替换 StartJobCard）：状态徽标（queued/running）+ 取消按钮（confirm 后 POST cancel）+ 失败/取消后显示 error_code/error_message + "重试"（= 再发一个新 job，同参数）+ interrupted 提示"服务曾重启，请重试"。
10. `WorkbenchScreen.tsx` 接线两个卡片 + jobs 轮询驱动 `useDocuments`/`useDocument` 的 refetch（已有 live 检测吃事件；job 轮询是 run 出现前的过渡信号，两路并存，任一 live 就快轮询）。
11. 测试：`tests/uploads.test.ts`（did slug/冲突/魔数校验纯函数）、`queries` 新 hooks（mock fetch）、`StartJobCard`/`ActiveJobCard` 渲染与交互（mock）、LibraryScreen 上传态。
12. Playwright `e2e/upload.spec.ts`（1–2 用例）：真 serve（复用 webServer）+ 一个小 PDF fixture（**生成**一个最小合法 PDF 放 `web/e2e/fixtures/sample.pdf`，用脚本内联写 `%PDF-` 最小结构，或从 `tmp/` 复制一个真实小 PDF 进 fixture 目录提交——选后者时确认 PDF < 1MB 且无版权问题，`tmp/` 里的论文 PDF 别提交）；上传 → 列表出现新卡 → 进工作台 → 点开始（from=parse）→ job 进入 running（真实 MinerU 不可用则**诚实断言** failed + error_code 非空，两种分支都算通过但要在断言里区分并注明环境）。取消路径：用 stub profile（e2e webServer 的 serve 指向专用 root，预置 sleep-t profile）→ running → 取消 → canceled。
13. `web/README.md` 更新上传/开始翻译/取消工作流 + MinerU token 说明。

## Constraints
- 后端只动 `serve/`（uploads/profiles/routers/runner 脱敏/store 不动安全边界）+ `docs/frontend/api.md` 指定行 + 测试；前端只动 `web/`。
- **红线不变**：客户端永不提供命令字符串（PUT 只收脚本路径引用且白名单校验）；token 只走 serve 环境；envelope 脱敏后才能落盘/返回。
- did 不能 `.` 开头、不能撞 `.bdt-serve`；上传不创建假产物骨架。
- 上传大文件不读进内存（流式写盘，`shutil.copyfileobj` 或分块）。
- 不做：SSE 并入 job 事件（W14）、草稿/编译（W09）、断点续传、多文件并发上传（串行即可）。
- e2e fixture PDF 提交进仓库要小（<1MB）；`tmp/` 的真实论文 PDF 不进 git。
- 验证：后端 pytest 全套（serve 7 文件 + single_entry）+ ruff + `git diff --check`；前端 typecheck/lint/test/build/e2e 全绿。
- macOS 无 GNU `timeout`：限时装在 bash 工具 timeout 参数上；pytest 加 `-p no:cacheprovider`；长命令 ≤ 240s 一段。
- 不委派、不 commit/push、不改 .plan；证据存 `tmp/`（后端）与 `web/tmp-smoke/`（前端）。

## Validation
```
# 后端
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest tests/test_serve_uploads.py tests/test_serve_profiles.py tests/test_serve_jobs.py tests/test_serve_app.py tests/test_serve_store.py tests/test_serve_documents.py tests/test_serve_events.py tests/test_serve_artifacts.py tests/test_single_entry.py -q -p no:cacheprovider --basetemp="tmp/pytest-W08-$(date +%Y%m%d-%H%M%S)"
.venv/bin/ruff check babeldoc_tools/serve tests/test_serve_uploads.py tests/test_serve_profiles.py
# 前端
cd web && pnpm typecheck && pnpm lint && pnpm test && pnpm build && pnpm e2e
```
真实冒烟（写进报告）：`bdt serve --root tmp/w08-smoke-root`（专用副本 root，**不动 tmp/ 现有 workdir**）→ curl 上传一个真实 PDF（从 tmp/ 现有 workdir 的源 PDF 或 `tmp/w07-parent-smoke` 挑一个小的）→ 列表出现 → POST job from=translate + stub echo profile → succeeded/诚实失败 → envelope 里**无命令字符串无 debug url**（grep 验证 `.sh`/`token=` 不出现）→ PUT profiles 建一个引用 `scripts/` 的 → GET 只见 id。前端手动冒烟截图 3 张（拖拽上传/配置表单/取消中）存 web/tmp-smoke/。`.pth` hidden 照旧处理。

## Report back
改动清单、脱敏前后 envelope 对比（同字段摘录）、上传边界测试摘要（魔数/超限/did 冲突）、e2e 两分支（有/无 MinerU token）结果与截图路径、四项+后端验证结果、偏差清单、遗留风险。绑定 output 回传。最多两轮修复。
