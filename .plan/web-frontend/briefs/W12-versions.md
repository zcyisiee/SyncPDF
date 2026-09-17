# Task
W12：版本归档——编译产物版本化存档 + 归档视图 + 任一版本下载

## Objective
在 feat/web-frontend（W11 已合入）实现版本历史：每次成功编译的 PDF 自动归档为版本（revision 单调），归档视图列出全部版本（时间/revision/触发原因/质量状态），任一版本可下载。用户验收标准中的「下载编译后的翻译 PDF」升级为「下载任一历史版本」，且明确标注质量状态。

## Context
必读：
1. `docs/frontend/api.md` §3.2 compile 字段（revision/artifact）+ 下载键规则；W09 `serve/compile.py`（发布点 = `settle_compile` 成功分支，归档钩子挂这里）。
2. W03 `serve/artifacts.py` 白名单（`output/*.pdf` 等）——归档目录**不进现有白名单**，走新端点专用读取（防逃逸）。
3. W10 前端 `DownloadButton`/`download.ts` 判定表（归档视图复用质量徽标 + revision 徽标组件）、`InspectorPanel` 归档 tab（W04 占位）。
4. 归档存储：`<workdir>/.bdt-serve/versions/<r>.pdf` + `<workdir>/.bdt-serve/versions.json`（`{items: [{revision, created_at, trigger, artifact_name, bytes, sha256_head, quality: {check_verdict, pipeline_ok}}]}`，revision 升序）。发布成功 = `os.replace` 到版本文件（同一文件即归档，不再另拷）+ 追加 manifest 行。
5. trigger 值：`debounce`（防抖自动）/ `manual`（显式 POST compile）；quality 快照自当时详情数据（`views` 的 quality 读取）——注意质量快照**只记录不门禁**：needs_fix 版本照样可下载（黄标），与 W10 规则一致。
6. 上限：保留最近 50 个版本，超出删最旧（文件 + manifest 行）；`compile.json` 的当前 artifact 指向 output/ 最新发布（不变），versions 是历史。

## Deliverables
1. `serve/versions.py`：`VersionStore`（manifest 原子写 + 锁；`archive(workdir, revision, trigger, quality)` 在发布后调用——**mv 语义核对**：W09 发布是 `os.replace(副本PDF, output/<name>)`，归档需要的是**同一份字节**，实现上先 copy 到 versions/<r>.pdf 再 replace 到 output/（或 replace 后 hardlink/copy，选不破坏原子性的方案并写注释））；`list_versions`；`read_version_pdf(r)`（只允许 `.bdt-serve/versions/` 下数字名文件，404 version_not_found）。
2. 端点：`GET /documents/{did}/versions`（manifest items 倒序 + 当前 compile 上下文：`{current_revision, stale}`）；`GET /documents/{did}/versions/{r}/pdf`（FileResponse，inline download 文件名 `<原产物名去 .pdf>.r<r>.pdf`）。写白名单不加（全是 GET）。
3. W09 集成：`settle_compile` 成功分支调 `archive`；失败/取消不归档。重启不丢（manifest 在盘上）。
4. `docs/frontend/api.md`：§3.x 新小节（versions 端点 + manifest 形状 + 50 上限 + trigger/quality 语义）。
5. 前端归档视图（`#/d/:did/archive`，W04 占位路由）：
   - 版本列表（时间倒序）：r 徽标 + 时间 + trigger 徽标（自动/手动）+ 质量徽标（复用 W10 组件/判定）+ 大小 + 下载按钮（`versions/{r}/pdf`）
   - 当前版本高亮 + stale 提示（「草稿有未编译修改」条）
   - 空态：从未编译过的文档显示引导（指向翻译视图开始编辑）
   - InspectorPanel 归档 tab 换真（简单摘要 + 「查看全部」链接到归档视图）
6. 测试：后端（归档成功追加/50 上限淘汰/失败不归档/404/白名单外路径 404/重启仍在/quality 快照记录/trigger 值）；前端（列表渲染/当前高亮/stale 条/空态/下载 href/质量徽标复用）。
7. e2e：可加一条轻量用例（真 serve + 已有版本 fixture 或复用 retranslate spec 的副本 → 列表渲染 + 下载 href 断言；**不**跑真编译）。若 e2e 成本高，用 API 层 pytest 覆盖 + 手工冒烟截图替代，报告里说明选择。
8. README（serve 段）+ web/README.md 补版本归档。

## Constraints
- 后端只动 `serve/` + api.md + 测试；前端只动 `web/`。
- **不改变 W09 发布语义**：output/ 最新 PDF 仍是下载键主路径（W10 DownloadButton 不改主逻辑，可加「历史版本」链接跳归档视图）；versions 只增不覆盖（同 revision 重发布 = 先删旧 r 文件再写？——**不**：revision 单调，同 r 不会重发布；防御性处理：manifest 已有该 r 时覆盖文件并更新行，写注释）。
- 版本文件不在 W03 白名单 → `GET artifacts` 清单**不**出现 versions（防目录混入）。
- 50 上限淘汰时若被删版本恰是当前 compile.revision 指向的——不可能（当前 revision 是最大值，只删最旧）。
- 验证：pytest serve 全套 + ruff + 前端五项 + 手工冒烟（真 serve：两次编译产生 r1/r2 → 列表 2 条 → 下载 r1 字节 = 当时的 sha；可用 stub 编译或真编译一份小文档）。真 build ~180s，冒烟允许用 `tests` 的 stub 编译路径构造版本。
- macOS 无 GNU timeout；bash 工具 timeout ≤240s 分段；不委派、不 commit/push。

## Validation
```
PATH="$PWD/.venv/bin:$PATH" PYTHONPATH="$PWD" .venv/bin/python -m pytest tests/test_serve_versions.py tests/test_serve_compile.py tests/test_serve_draft.py tests/test_serve_jobs.py tests/test_serve_candidates.py tests/test_serve_app.py tests/test_serve_store.py tests/test_serve_documents.py tests/test_serve_events.py tests/test_serve_artifacts.py tests/test_serve_uploads.py tests/test_serve_profiles.py tests/test_single_entry.py -q -p no:cacheprovider --basetemp="tmp/pytest-W12-$(date +%Y%m%d-%H%M%S)"
.venv/bin/ruff check babeldoc_tools/serve tests/
cd web && pnpm typecheck && pnpm lint && pnpm test && pnpm build && pnpm e2e
git diff --check
```

## Report back
改动清单、归档写入语义（copy/link 选型 + 原子性说明）、两次编译两版本的冒烟证据（sha 对应）、上限淘汰证据、e2e/pytest 选择与结果、偏差清单、遗留风险。绑定 output 回传。最多两轮修复。
