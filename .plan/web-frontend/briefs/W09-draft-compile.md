# Task
W09：草稿与编译——draft CRUD、revision 乐观并发、compile job、隔离编译原子发布、1.5s 防抖

## Objective
在 feat/web-frontend（W08 已合入）实现 `docs/frontend/api.md` §3.3/§3.4 的写路径核心：`GET/PATCH/DELETE /documents/{did}/draft`（草稿 = 译文/排版覆盖，单调 revision + base_revision 乐观并发）+ `action=compile` 的 job（隔离工作目录编译，成功后原子发布 PDF，失败不破坏上一版）+ 草稿保存后 1.5s 服务端防抖自动编译。这是"改译文 → 重新编译 → 下载新 PDF"的后端心脏。

## Context
必读：
1. `docs/frontend/api.md` §3.2（compile 字段语义：status ∈ none|running|ok|failed、compile.revision、stale=(draft.revision>compile.revision)、下载必须带 artifact.revision）+ §3.3（draft.json 形状、layout 键名与范围、box 是 PDF y 向上坐标）+ §3.4（compile job 的 scope/pages 回退语义）。
2. `.plan/web-frontend/EXECUTION.md` 纠偏 #6/#7（全文）。
3. W07 代码：`serve/{jobs,runner,routers/jobs}.py`（JobRunner 扩展 compile action 的挂点）、W08 的 sanitize。
4. **编译链事实**（主控已核实）：
   - draft `target` 覆盖译文：`translate.merge_translated_markdown(workdir, {id: (body, label)})` 按 sheet 顺序重排合并进 `agent/translated.md`（body = 完整段落译文文本，label 从 anchors.json 取）；
   - draft `layout` 覆盖排版：`babeldoc/tools/agent/layout_overrides.py` 的 `OVERRIDES_FILE = "layout_overrides.json"`（段落级 scale_cap/font_scale/line_skip/box_scale/box + 页级 font_scale，含范围校验与 restore 快照机制，`load_overrides`/`to_config_hook`）——draft layout 字段名与范围**必须**与之一致，写文件用其校验逻辑；
   - 编译 = `apply`（`apply_translation` 写回 IR）+ `build`（`layout.build_pdf`），即 `bdt run --from apply`（apply+build+check?）——**compile 只跑到 build**（check/review 是质量门禁不是编译；compile 成功不置 pipeline_ok=true）。子进程方案：复用 W07 runner，argv `python -m babeldoc_tools run --workdir <隔离副本> --from apply`。注意：`--from apply` 的 stale 检查（`STALE_INPUTS`：apply 依赖 document_md+translated_md）在隔离副本里自己跑自己的，无干扰；
   - `build_pdf` 读 `state.pkl`（IR 状态）——隔离副本必须带 agent/ 全套产物（state.pkl、document.md、anchors.json、sheet.jsonl、translated.md、layout_overrides.json、layout_geometry.json 等）+ 源 PDF 路径引用。**用 `cp -R` 整个 workdir（不含 debug/、output/ 可选）**，简单可靠优先；产物 PDF 数十 MB 内可接受。
   - 原子发布：build 成功（exit 0 且信封 ok）→ 把副本 `output/*.pdf` mv 回真 workdir `output/`（mv 跨目录同分区原子）→ 记 `compile_revision`；失败/取消 → 副本删除，真 workdir 不动。
5. 防抖（EXECUTION.md #7）：PATCH draft 成功后启动 1.5s 定时器（每个 did 一个，`asyncio.Task` + `loop.call_later` 或 `asyncio.sleep` 任务）；到点若无活动 compile job（queued/running）则自动 POST compile（scope=full）；期间再 PATCH 重置计时器；serve 重启定时器丢失 = 下次 PATCH 再触发（可接受，写进注释）；浏览器断开不影响（纯服务端）。**防抖触发日志记 stderr**（stdout 单行约定不动）。
6. W02 的 `compile` 字段目前是诚实占位（status=none/revision=0）——本任务把它接真：读 `<workdir>/.bdt-serve/compile.json`（本任务新增的编译状态文件：`{revision, status, artifact: {name, bytes}, finished_at, error_code}`），views.py 的 `_compile_state` 换真源。
7. 乐观并发：`PATCH /draft` body 带 `base_revision`；与当前不匹配 → 409 `revision_conflict`（detail.current_revision）。**活动 compile 期间 PATCH**：按 EXECUTION.md「活动任务期间编辑只读」→ 409 `document_busy`（复用现有错误码，detail 指向 job_id）。DELETE /draft → 清空覆盖（revision 不回退，继续 +1）。
8. draft.json 落盘位置：`<workdir>/.bdt-serve/draft.json`（与 jobs 同级，不进 agent/ 产物区）。

## Deliverables
1. `babeldoc_tools/serve/draft.py`：`DraftStore`（per-workdir，load/save 原子写 + `asyncio.Lock`）：
   - `get(did) -> DraftDoc`（revision/updated_at/paragraphs{pid: {target, layout{...}, updated_at}}）
   - `patch(did, base_revision, changes) -> DraftDoc`：changes = `{paragraphs: {pid: {target?, layout?，null=删}}}`；校验 pid 形状（`_ID_RE` 同 layout_overrides）、target 是 str、layout 键范围（复用 layout_overrides 的 PARAGRAPH_FLOAT_KEYS/PARAGRAPH_LIST_KEYS 校验，不复制魔法数字）；revision+1；触发防抖（回调注入，测试里替换）
   - `delete(did)`：清 paragraphs，revision+1，同样触发防抖
   - 冷启动：draft.json 不存在 → revision=0（空草稿）；PATCH 从 0 开始 → revision=1
2. `babeldoc_tools/serve/compile.py`：
   - `CompileService`：`request_compile(did, revision, scope)` → 建 compile job（JobRunner 复用：action=compile 的 argv 特殊构造——见下）；防抖调度（1.5s）；compile 状态文件读写（`.bdt-serve/compile.json`）
   - **隔离编译流程**（在 JobRunner 的 compile 分支或独立 spawn 路径）：
     a. 快照：`cp -R <workdir> <workdir>/.bdt-serve/compile-<job_id>/`（排除 `.bdt-serve`、`debug/`，可用 `shutil.copytree(ignore=)`）；
     b. materialize draft：merge_translated_markdown（target 覆盖）+ 写 layout_overrides.json（layout 覆盖，**先快照原有 overrides 供回滚**）；
     c. spawn `python -m babeldoc_tools run --workdir <隔离目录> --from apply --debug --debug-no-open`（apply+build；不带 translator/reviewer）；
     d. 成功 → `mv` 隔离目录 `output/*.pdf` → `<workdir>/output/`（同名覆盖即原子替换）+ 写 compile.json `{status: ok, revision, artifact}`；失败/取消 → 删隔离目录，compile.json `{status: failed, error_code}`，**上一版 PDF 未动**；
     e. 结束后（无论成败）清理隔离目录。
   - compile job 记录里 `from_stage="apply"`、`profile=null`（compile 不需要 profile）——JobRecord 允许 profile None（改 schema 校验）。
3. `routers/draft.py`：`GET/PATCH/DELETE /documents/{did}/draft`；PATCH body `{base_revision, paragraphs}`；写端点守卫白名单 +3；错误码：`409 revision_conflict`（detail.current_revision）、`409 document_busy`（活动 compile 中）、`422 draft_invalid`（字段/范围）。
4. `routers/jobs.py` + `schemas.py`：`action=compile` 解禁：`scope`（full|pages，**页级编译按已批准设计回退全量**：响应/记录里 `requested_scope`/`effective_scope`/`downgrade_reason`）；`base_revision` 字段（compile 捕获固定 revision——防抖触发的 compile 用当时 draft revision，显式 POST 的可带 base_revision 校验一致性，不匹配 409）。JobRecord 加 `requested_scope/effective_scope/downgrade_reason` 可空字段。
5. W02 详情 `compile` 字段接真（views.py `_compile_state` → compile.json；draft.revision > compile.revision → stale=true）；artifacts 清单**不**加 revision（PDF 与 compile.json 关联由详情给出，api.md §1.5 已注明 W09 落地——本次在 api.md 里把这句改成「revision 由详情 compile.artifact.revision 提供」）。
6. 防抖接线：PATCH/DELETE draft 成功 → `CompileService.schedule(did)`（1.5s 后无活动 compile 则 POST compile full）；**显式 compile job 存在时防抖跳过**（不重复编译）；compile 运行中的 PATCH → 409（编辑只读）。
7. `docs/frontend/api.md`：§3.3 状态行改已实现（W09）；§3.4 compile 解禁说明 + scope 回退语义；§2 错误码表补 `409 revision_conflict`、`422 draft_invalid`。
8. `tests/test_serve_draft.py` + `tests/test_serve_compile.py`（自包含 fixture：**用现有测试的 workdir 构造器 + 真实 build**？build 需要 latex + 完整 IR——重。替代：**编译链用 monkeypatch stub build**（argv 层面替换 run 命令为 stub 脚本：写 output/x.mono.pdf + exit 0/1 可控），隔离/发布/回滚/防抖/并发全是 serve 层逻辑可真测；**真实 build 的 compile 一条**用 `tmp/ccs3764-dyn` 副本在冒烟里验（W07 已证 from=apply 真实可跑，注意副本里 build 会因 input.pdf 缺失失败——**冒烟改用 tmp/e2e-2602-02908v2-20260917 副本**，它的 build 链路完整））：
   - draft：get 空/patch+revision 递增/base_revision 不匹配 409/并发锁/DELETE 后 revision 不回退/非法 layout 值 422（每个键的边界）/target 类型校验
   - compile（stub build）：成功发布（隔离目录创建 → PDF mv 回 → compile.json ok + revision）/失败不破坏上一版（预置旧 PDF，stub exit 1 → 旧 PDF 还在 + status failed）/取消同上/防抖 1.5s 触发（快时钟或等真实 1.6s）/防抖跳过（已有活动 compile）/连续 PATCH 只编译一次（计时器重置）/scope=pages 回退 full + 三字段/显式 compile base_revision 不匹配 409/compile 期间 PATCH 409
   - 详情接真：compile.json → 详情 compile 字段 + stale 计算
9. README（serve 段）补 draft/compile/防抖说明。

## Constraints
- 后端只动 `serve/` + `docs/frontend/api.md` 指定处 + 测试。**禁改** translate.py/layout.py/layout_overrides.py 的既有函数签名（可 import 复用；需要小工具函数放 serve/ 内）。
- 不动 web/（W10 编辑 UI）。
- 隔离目录命名 `compile-<job_id>` 在 `.bdt-serve/` 下（store 白名单天然排除）；编译结束必须清理（失败也清，保留 compile.json 状态与 job envelope 供诊断）。
- build 失败的 stderr/信封照 W07 透传（error_code）；**编译成功 ≠ 质量通过**（pipeline_ok 不动）。
- draft/compile 的并发锁：per-did（不全局串行化 PATCH）。
- 防抖定时器与 serve 生命周期绑定（shutdown 取消未触发的定时器，日志 stderr）。
- 隔离副本的 `debug/` 不复制（编译的 run 归档会落在隔离目录里被清理——**注意**：`--debug` 会写隔离目录的 debug/runs；这些归档随清理消失是**预期**（编译调试信息在 job envelope 里），报告里注明）。
- 验证：pytest serve 全套 + single_entry + ruff + git diff --check；真实冒烟（tmp/e2e-2602-02908v2-20260917 副本）：PATCH draft 改一段译文 + layout → 防抖 1.5s → compile 跑真 apply+build → 新 PDF（mtime/revision 变化）→ 详情 compile.status=ok revision 对应 → 再 PATCH → stale=true → 下载 PDF 字节非旧版。
- macOS 无 GNU timeout；pytest `-p no:cacheprovider`；bash 工具 timeout 参数 ≤240s 分段。
- 不委派、不 commit/push、不改 .plan；证据存 tmp/。

## Validation
```
PATH="$PWD/.venv/bin:$PATH" .venv/bin/python -m pytest tests/test_serve_draft.py tests/test_serve_compile.py tests/test_serve_jobs.py tests/test_serve_uploads.py tests/test_serve_profiles.py tests/test_serve_app.py tests/test_serve_store.py tests/test_serve_documents.py tests/test_serve_events.py tests/test_serve_artifacts.py tests/test_single_entry.py -q -p no:cacheprovider --basetemp="tmp/pytest-W09-$(date +%Y%m%d-%H%M%S)"
.venv/bin/ruff check babeldoc_tools/serve tests/test_serve_draft.py tests/test_serve_compile.py
git diff --check
```

## Report back
改动清单、隔离编译时序（副本创建→materialize→run→发布/清理）、防抖行为矩阵（PATCH 次数→编译次数）、失败保留旧版证据（前后 PDF mtime/bytes）、真实冒烟（e2e-2602 副本：改译文→编译→新 PDF→stale 流转）、验证结果、偏差清单、遗留风险。绑定 output 回传。最多两轮修复。
