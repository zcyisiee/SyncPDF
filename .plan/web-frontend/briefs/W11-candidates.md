# Task
W11：候选重译——生成/采用分离（AI 局部重译候选 + 人工采用）

## Objective
在 feat/web-frontend（W10 已合入）实现「对不满意段落让 AI 重新翻译，但候选未采用前绝不改入正文」的完整闭环：后端 `POST /documents/{did}/paragraphs/{pid}/retranslate`（生成候选，不改任何正文产物）+ `POST .../candidates/{cid}/adopt`（人工采用：写进草稿 target，走 W09 防抖编译）+ 拒绝/忽略 + 候选列表；前端段落编辑面板「AI 重译」按钮 + 候选对比区（原文/当前译文/候选译文）+ 采用/拒绝。

## Context
必读：
1. `docs/frontend/api.md` §3.4 retranslate/adopt 契约段（如与实现冲突，停下报告）；`babeldoc_tools/translate.py` 的 `retranslate_blocks`（支持按 id 补译/重译：替换译文 → 合并回 translated.md；**这是"采用"时可复用的底层**，但候选**生成**必须走独立路径不改 translated.md）。
2. W09 代码：`serve/{draft,compile,jobs,runner}.py`——候选生成是一个 job（排队/取消/并发限制全部复用 JobRunner），采用是同步请求（写 draft target + 防抖编译）。
3. 候选生成机制（主控已核实）：`retranslate_blocks(workdir, ids, translator)` 在内存中跑 translator（LLM 调用），返回新译文但**不写盘**——需核实其确切签名与是否落盘；若它直接改 translated.md，则候选生成需包一层：在**临时副本**（参照 compile 的隔离方案，但只 copy agent/ 必要文件）里跑，取回译文后丢弃副本。核对后选最小侵入方案，报告里写明选型。
4. 候选存储：`<workdir>/.bdt-serve/candidates.json`（结构：`{next_id, items: [{id, pid, source, baseline_target, candidate_target, status: pending|adopted|rejected, model_label, created_at, adopted_at}]}`）。采用 = status→adopted + **写 draft target = candidate_target**（走 W09 防抖）；拒绝 = status→rejected。同 pid 可多候选，最新的 pending 在前。
5. 翻译调用安全：候选生成的 translator 只能来自 profile（W08 白名单解析，客户端永不传命令）；profile 未配置 → 422 `profile_missing`（或契约已有码）。无 reviewer 也可以生成候选（重译是 translator 职能）。
6. 候选生成 job 记录：`action=retranslate`、from_stage=null、profile=所用 translator profile id、envelope 记录 pid 与候选 id；job 成功后候选 status 仍 pending，正文/草稿/compiled PDF 一概不动。
7. 前端（W10 的 ParagraphEditor 扩展）：
   - 「AI 重译」按钮 → 选 profile（或用上次）→ 提交（活动中禁用，ActiveJobCard 显示）
   - 候选区（面板下方）：每条候选 = 原文摘要 / 当前译文（若已改显示草稿版） / 候选译文 + 「采用」「拒绝」；采用后译文框立即变候选文本（本地态） + 草稿已修改标记；被拒候选折叠
   - 候选未采用前：预览/PDF/编译均不含候选内容（服务端保证 + 前端不本地替换正文显示以外的区域）
8. API 端点（新增，先在 api.md 落契约再实现）：
   - `POST /documents/{did}/paragraphs/{pid}/retranslate` `{profile}` → 202 `{candidate_id, job_id}`（或契约形状）
   - `GET /documents/{did}/paragraphs/{pid}/candidates` → 候选列表（含 status）
   - `POST /documents/{did}/paragraphs/{pid}/candidates/{cid}/adopt` → 200 `{revision}`（draft PATCH 的返回）
   - `POST .../candidates/{cid}/reject` → 200
   - 错误码：pid/cid 不存在 404、活动 job 409 document_busy（retranslate 是 job 所以受排队；adopt/reject 是同步小操作**不**受 busy 限制——但 draft busy 守卫仍适用于 adopt 的写草稿动作）

## Deliverables
1. `babeldoc_tools/serve/candidates.py`：CandidateStore（原子写 + asyncio.Lock，结构见上）；生成路径（决定最小侵入的候选生成方案并实现）；adopt（写 draft target + 触发防抖）。
2. `serve/routers/candidates.py`：四端点 + 写白名单 +3。
3. job 集成：`action=retranslate` 进 JobRunner（JobRecord/profile 校验复用）；生成期间同文档其它 job 正常排队规则不变。
4. `docs/frontend/api.md`：契约段补全（含 candidates.json 字段、状态机、错误码）。
5. 前端：`lib/queries.ts` 候选 hooks；`ParagraphEditor` 重译按钮 + 候选区（对比 + 采用/拒绝 + 状态徽标）；采用后本地态与 W10 草稿标记联动。
6. 测试：后端（stub translator profile：生成不改 translated.md/draft、adopt 写 draft+防抖、reject、并发、404/409、profile 未配置 422、重启后候选仍在）；前端（按钮态/候选渲染/采用联动/拒绝折叠/活动中禁用）。
7. e2e：`e2e/retranslate.spec.ts`（真 serve + stub translator profile——生成快、无 LLM）：点段 → AI 重译 → 候选出现（pending）→ 正文/预览断言未变（GET paragraphs target 仍基线）→ 采用 → 草稿 revision +1 且 target=candidate → （跳过真编译，断言防抖 job 创建即可后取消）→ 拒绝另一条候选。
8. README（serve 段）+ web/README.md 补候选工作流。

## Constraints
- 后端只动 `serve/` + api.md + 测试；前端只动 `web/`；禁改 translate.py 既有签名（复用函数，不改行为）。
- **红线：候选生成绝不改 agent/translated.md、translated.jsonl、draft.json、output/**（只写 candidates.json）；采用才通过 draft 通道。测试必须断言这些文件的 mtime/内容不变。
- adopt 的写草稿走 DraftStore 正常路径（revision+1 + 防抖），不绕过乐观并发（adopt 不需要 base_revision——服务端自己是唯一写者之一，冲突概率低，直接以当前 revision+1 写；若 draft 正被他人改，写锁串行化足够）。
- 翻译调用只在服务端用 profile 解析；候选里存 model_label（profile id）不存命令。
- e2e 用 stub profile（scripts/ 白名单）；生成路径若依赖 LLM 网络，stub 必须完全离线。
- 验证：pytest serve 全套 + ruff + 前端五项全绿；真实冒烟（stub profile：生成→断言零副作用→采用→draft 变化→防抖 job 出现→取消）。
- macOS 无 GNU timeout；bash 工具 timeout 参数 ≤240s 分段；不委派、不 commit/push。

## Validation
```
PATH="$PWD/.venv/bin:$PATH" PYTHONPATH="$PWD" .venv/bin/python -m pytest tests/ -q -p no:cacheprovider --basetemp="tmp/pytest-W11-$(date +%Y%m%d-%H%M%S)"（serve 相关全部）
.venv/bin/ruff check babeldoc_tools/serve tests/
cd web && pnpm typecheck && pnpm lint && pnpm test && pnpm build && pnpm e2e
git diff --check
```

## Report back
改动清单、候选生成方案选型（retranslate_blocks 复用 or 副本隔离，为什么）、零副作用证据（正文文件 mtime/sha 前后对比）、采用链路证据（draft/防抖）、e2e 时序、验证结果、偏差清单、遗留风险。绑定 output 回传。最多两轮修复。
