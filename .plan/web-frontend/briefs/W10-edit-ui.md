# Task
W10：编辑 UI——点段改译文、bbox 拖拽/数值编辑、自动/手动编译、版本状态提示

## Objective
在 feat/web-frontend（W09 已合入：draft CRUD + compile job + 防抖 + stale 语义）实现用户核心编辑闭环的前端：翻译视图点段 → 右侧面板改译文 / 调排版（bbox 拖拽 + 数值）→ 保存草稿（自动防抖编译）→ 预览与下载更新为新修订版。完成后满足用户验收标准的"手改译文、调整 bbox、重新编译"全链路浏览器可操作。

## Context
必读：
1. W09 契约与实现：`docs/frontend/api.md` §3.2（compile 字段/stale、artifact.name 是裸文件名、下载键 = `output/<name>` 拼接）、§3.3（draft：target + layout{scale_cap,font_scale,line_skip,box_scale,box}，box 是 **PDF y 向上** `[x,y,x2,y2]`，target 存 canonical 形式、前端零转换）；PATCH 语义（base_revision 乐观并发、**任何活动 job 期间 409 document_busy**）；`serve/draft.py`/`compile.py` 的接口。
2. W05 代码：`BboxLayer`/`pdfToScreen`（坐标换算——**反向换算 screen→PDF 也要写**：拖拽后的屏幕矩形 → pdf_native box，走 viewport 逆变换 `convertToPdfRectangle`）；`stores/ui.ts` 的 `selectedParagraphId`。
3. W02/W06：`useDocument`（compile 字段已接真）、`useGeometry`、段落数据 `GET /documents/{did}/paragraphs`（source/target/layout_label/geometry）。
4. W09 的 `GET /documents/{did}/draft`（草稿覆盖，含 revision）——前端要合并显示：段落面板显示 target 时草稿覆盖优先于 translated.jsonl 基线。
5. DESIGN.md §4.4 段落面板、§4.5 编辑态（如设计稿有差异以设计稿为准；不确定处报告）。

## Deliverables
1. `lib/queries.ts`：`useDraft(did)`（staleTime 5s + PATCH 后 invalidate）、`usePatchDraftMutation(did)`（带 base_revision，409 revision_conflict → 提示刷新重试；409 document_busy → 提示"编译中，稍后再试"）。
2. `components/edit/ParagraphEditor.tsx`（右侧面板"段落"tab，W06 留了 tab 位；进度视图仍是事件流 tab）：
   - 选中段落：id/label、源文（只读，等宽小字）、**译文 textarea**（草稿值 ?? 基线值；显示"草稿已修改"标记 + 恢复按钮）、排版参数区（scale_cap/font_scale/line_skip/box_scale 数值输入 + 范围校验提示，box 四值只读显示 + "在预览中拖拽调整"提示）   - 保存：失焦或 Cmd+S 手动（自动保存每键 1.5s 防抖——**前端本地防抖**与服务端防抖叠加没问题，服务端才是真源）；PATCH 成功后 revision 更新
   - 无选中：空态提示"在预览中点击段落框"
3. `components/edit/BboxEditor.tsx`（翻译视图 + layout bbox 模式下叠加在 BboxLayer 之上）：
   - 选中段的 rect 变为可拖拽（8 个手柄：四角 + 四边中点；Pointer Events，复用 Gutter 的拖拽模式）
   - 拖拽实时更新屏幕矩形（预览反馈），松手时 screen→PDF 逆变换 → PATCH draft 的 box
   - 拖拽期间禁用点击穿透（避免误选其他段）；ESC 取消拖拽
   - **换算必须单测**：`screenToPdfBox(rect, viewport)` 纯函数（convertToPdfRectangle + y 向上 box 排序），与 W05 的 `pdfToScreen` 互逆（roundtrip 测试）
4. `WorkbenchScreen.tsx` 翻译视图接线：右侧面板双 tab（段落 | 事件流——拖到翻译视图时事件流退为次要 tab）；预览区 bbox 模式强制 layout（编辑 box 语义）+ 选中段可拖拽
5. 编译状态 UI（预览工具条或面板顶部条）：
   - `compile.status=running` → "编译中…"（spin）+ **编辑禁用**（textarea readonly + 提示，契约 409 也会挡）
   - `compile.stale=true` → 醒目提示"草稿比当前 PDF 新（PDF 修订 r{compile.revision}，草稿 r{draft.revision}）" + "手动编译"按钮（POST job action=compile）
   - `compile.status=failed` → 错误条 + error_code + 重试按钮
   - 成功 → toast/条内提示"已更新到 r{revision}"
6. 下载按钮（工具条右侧）：**只在有可下载 PDF 时启用**；显示 `r{compile.revision}` 与质量徽标（quality.check.verdict：needs_fix 显式黄标"检查未通过"——不许冒充通过；pipeline_ok 才绿）；点击下载 `GET artifacts/{mono.pdf 名}`（用 compile.artifact.revision 标注文件名后缀如 `paper.mono.r7.pdf`——前端改名，服务端不动）
7. 测试（Vitest）：`screenToPdfBox` 纯函数 + roundtrip；ParagraphEditor（草稿优先显示/防抖保存/409 处理两分支/编译中禁用）；BboxEditor（手柄渲染/拖拽回调数学——纯函数 `resizeBox`）；下载按钮状态矩阵（none/ok/failed/stale × quality）。
8. Playwright `e2e/edit.spec.ts`（真 serve，fixture：`tmp/e2e-2602-02908v2-20260917` 只读基线 + **上传/复制出的副本 workdir** 由测试内创建（serve root 指向 e2e 专用 tmp root，测试 setup 里 cp -R 一份可写副本））：点段 → 改译文 → 保存 → 等 compile ok（真 build，stub 不行——e2e-2602 的 build 链路完整）→ 详情 revision/stale 断言 → 拖拽 bbox 手柄 → 松手 → draft 更新断言（GET draft box 值变化）。**真 build 每次 ~180s**：e2e 全链只做一次真编译断言（改译文→保存→等 ok→revision 断言），其余环节（stale/手动编译按钮/失败态）用 stub build 服务或编译前状态断言，避免 e2e 跑 10 分钟；e2e 单用例 timeout 放宽到 300s。
9. `web/README.md` 更新编辑工作流。

## Constraints
- 只改 `web/`。契约不改（发现问题停下报告）。
- 前端换算：**屏幕坐标 → PDF box 必须经 viewport 逆变换**（`convertToPdfRectangle`），禁止假设 scale=1 或直接减 cropbox；roundtrip 单测是验收红线。
- 编辑期间 SSE/事件流不受影响（W06 的面板 tab 化不破坏现有用例——改 W06 测试属预期内，语义不变）。
- 上传下载按钮的 disabled 逻辑不许出现"下载失败 PDF 冒充成功"。
- `pnpm typecheck && pnpm lint && pnpm test && pnpm build && pnpm e2e` 全绿（e2e 含 W05/W06 既有 5 用例 + 新增）。
- macOS 无 GNU timeout；bash 工具 timeout 参数 ≤240s 分段。
- 不委派、不 commit/push；证据存 web/tmp-smoke/。

## Validation
```
cd web && pnpm typecheck && pnpm lint && pnpm test && pnpm build && pnpm e2e
```
手工冒烟（写进报告，截图 4–5 张存 web/tmp-smoke/）：真 serve + 已有 workdir 副本 → 翻译视图点段改译文 → 保存 → 预览工具条编译状态流转（running→ok）→ 拖 bbox → stale 提示 → 手动编译 → 下载按钮显示新修订号。

## Report back
改动清单、换算 roundtrip 测试结果、e2e 时序（改→编译→stale→编译）、截图路径、五项验证结果、偏差清单、遗留风险。绑定 output 回传。最多两轮修复。
