# Task
W13：术语表——全局词表 CRUD + 翻译注入 + 前端词表视图

## Objective
在 feat/web-frontend（W12 已合入）实现术语表闭环：全局词表（术语→译文对）的增删改查 + 翻译 job 时注入（`--glossaries` 或环境等价机制，先核实底层 CLI 支持）+ 前端 `#/glossary` 视图（W04 占位路由换真）。用户验收标准「选模型/词表/页范围」中的词表部分落地。

## Context
必读：
1. **底层注入机制核实（开工第一步，主控未预核实）**：`babeldoc_tools/run.py` 的 `--glossaries` 参数（或 `config` 等价物）、格式（BabelDOC 原生支持 CSV/JSON 术语表——查 `babeldoc` 包的 glossary 支持与 `bdt run` 的透传路径）。把确切机制（参数名/文件格式/放置位置）写进报告；若 CLI 完全不支持，停下报告（不得私改 run.py——那是 W09 就定下的边界）。
2. W08 `StartJobCard`（开始翻译卡）：profile/页码/dual/from 的现有形状——词表选择 UI 挂这里（多选）。
3. W04 前端壳：`#/glossary` 路由 + 左侧菜单「词表」项（已存在，占位）。
4. 存储设计（最小闭环）：**一个全局词表**（不做多词表管理，除非核实发现 BabelDOC glossary 文件就是单文件语义）：`<store_base>/.bdt-serve/glossary.csv`（或核实的原生格式）+ API 端点读写。术语对：`{source, target, note?}`，按 source 排序去重（同 source 覆盖 target）。
5. 注入语义：翻译 job（action=run from=parse/translate）提交时若词表非空 → 服务端把词表文件路径以核实出的机制传给子进程；compile/retranslate **不注入**（术语约束翻译阶段，不约束重译候选——重译候选保持段落上下文自由；如核实后发现重译也该注入，报告里说明并停下等裁决）。**翻译 job 的词表注入由服务端做**（客户端只传 `use_glossary: bool`，默认 true；不传文件内容）。
6. 一致性红线：词表变更**不回溯**已翻译内容（不自动重翻）；前端在词表视图明确提示「对已翻译段落无追溯效果，重新翻译生效」。

## Deliverables
1. 后端（机制核实后）：
   - `serve/glossary.py`：词表读写（原子写 + 锁 + 校验：source/target 非空、长度上限、重复 source 处理）
   - 端点：`GET /glossary`（条目列表 + 条数）、`PUT /glossary`（整表替换：`{entries: [{source, target, note?}]}`——编辑器本地编辑后整表保存，简单可靠）、`DELETE /glossary`（清空）。写白名单 +2。
   - job 集成：`JobCreateRequest.use_glossary`（默认 true，仅翻译阶段 job 生效）；argv 构造时注入。
   - api.md §3.x 契约小节（形状/错误码/注入语义/不回溯说明）。
2. 前端：
   - `#/glossary` 视图：表格编辑（source/target/note 三列，行内编辑 + 添加行 + 删除行 + 保存/放弃）、CSV 导入导出（前端解析，`PUT` 整表）、「对已翻译段落无追溯效果」提示条、保存后 invalidate。
   - `StartJobCard`：词表开关（`use_glossary`，默认开，词表为空时禁用 + 提示「词表为空」）。
   - 左侧菜单「词表」项加条数徽标（有词表时）。
3. 测试：后端（CRUD/校验/注入 argv 断言（stub profile + monkeypatch argv 捕获）/不注入 compile/空词表不注入/use_glossary=false 不注入/错误码）；前端（编辑器行为/保存/导入导出/开关联动/空态）。
4. e2e：`e2e/glossary.spec.ts`（真 serve）：词表视图增改保存 → GET 断言 → 开始翻译卡开关存在与禁用逻辑（不真跑翻译——用 API 断言 job argv 或信封里 glossary 痕迹，如机制不外显则断言 use_glossary 透传 + stub 翻译 job 的信封/产物）。
5. README（serve 段）+ web/README.md。

## Constraints
- 后端只动 `serve/` + api.md + 测试；前端只动 `web/`；**禁改 run.py/translate.py**。
- 词表文件位置：`<store_base>/.bdt-serve/` 下（不进任何 workdir，全局一份）；若 BabelDOC 原生机制要求特定路径/格式，以原生为准并在报告说明。
- 注入只在服务端 argv/config 层完成；客户端永远只传布尔。
- CSV 导入导出的解析在前端（`PUT` 走 JSON 整表），后端不收 CSV 文本（避免解析面）。
- 验证：pytest serve 全套 + ruff + 前端五项 + 手工冒烟（真 serve + stub translator：词表两条 → 翻译 job 的 argv/信封含词表痕迹 → 词表为空时不注入）。
- macOS 无 GNU timeout；bash 工具 timeout ≤240s 分段；不委派、不 commit/push。

## Validation
```
PATH="$PWD/.venv/bin:$PATH" PYTHONPATH="$PWD" .venv/bin/python -m pytest tests/test_serve_glossary.py tests/test_serve_jobs.py tests/test_serve_compile.py tests/test_serve_candidates.py tests/test_serve_versions.py tests/test_serve_draft.py tests/test_serve_app.py tests/test_serve_store.py tests/test_serve_documents.py tests/test_serve_events.py tests/test_serve_artifacts.py tests/test_serve_uploads.py tests/test_serve_profiles.py tests/test_single_entry.py -q -p no:cacheprovider --basetemp="tmp/pytest-W13-$(date +%Y%m%d-%H%M%S)"
.venv/bin/ruff check babeldoc_tools/serve tests/
cd web && pnpm typecheck && pnpm lint && pnpm test && pnpm build && pnpm e2e
git diff --check
```

## Report back
底层注入机制核实结果（参数名/格式/证据文件行号）、改动清单、注入 argv 断言证据、不回溯提示的落地、e2e/pytest 结果、偏差清单、遗留风险。绑定 output 回传。最多两轮修复。
