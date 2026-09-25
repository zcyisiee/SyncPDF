# 桌面端前端重写 · Task state

> 仅用户和主 Agent 可改；subagent 只读。开始：2026-09-25。分支 `feat/desktop-develop`（主树）。

## 用户意图（已定决策，详见计划原文）

- VSCode 精简工作台：标题栏右上 ⌘B / ⌘J / ⌥⌘B / 齿轮；圆角面板+间隙+可拖动、布局持久化；仅浅色 Claude Cream；codicons。
- 左栏论文库（开始区+卡片，右键删除/Finder），编辑器：欢迎页 / PDF（原文·译文·双栏，同步滚动缩放）/ 设置页；bbox 按类型着色+图例+行内公式虚线。
- 右栏 4 tab：块详情 / 排版 / 术语 / 论文信息；底部 3 tab：事件流 / 问题 / 引擎日志。
- 数据存 `~/.sp`（library.db、blobs/<sha>.pdf、docs/<id>/），译文只保留最新；串行队列；safeStorage 存 key；agy/pi/OpenAI 兼容 HTTP；用户指令追加并计入缓存键；术语全局+单篇追加。
- 执行：主控亲做，杂活可派 sonnet（遵守 delegation.md）。

## 进度

- [x] M0：`~/.sp` → `~/.sp-legacy-20260925`（确认无 bdt serve/syncpdf 进程、无打开句柄），新建 `~/.sp/{blobs,docs}`。
- [x] M0：`app/src/shared/protocol.ts` 按 Rust 协议重写（815cb8ad）
- [x] M1 引擎：`layout`/`doc_meta` 事件、paragraph `kind`/`source_text`（815cb8ad）；行内公式 = 公式区域落在可译区域内（几何判定，同 inline_formula KEEP 标准）
- [x] M2 引擎（提前）：`syncpdf-cli run` 长驻会话，run 排队串行，cancel/EOF 由读线程即时取消（48a1fa05）
- [x] M1 前端：外壳+论文库+PDF/bbox（575f32e4）；真实验收修复 29164222 / 77e1794b / edeb4165：两篇 agy 真实翻译均完成（StoryScope 94.5s·12 段回退，SoL-Pi 15 页），截图 `tmp/desktop-frontend/real-*.png`
- [x] M2：事件流/问题/日志、块详情只读、串行队列（M1 已含）；阶段缓存按论文落 `~/.sp/docs/<id>/store.db`（215c2575，run 请求新增可选 `store`）
- [x] 用户反馈修复：bbox 按侧、按段（paragraph 事件新增 `source_bbox`；原文侧用源框，译文侧用译文行框并集，行内公式只画原文侧）；两端对齐判定容忍首行缩进、末行短行与字符突出（容差 max(1pt, 0.35em)），摘要等非 Text 段落恢复 Knuth 两端对齐。StoryScope 截图 `tmp/desktop-frontend/fix-2*.png`
- [x] 用户反馈 UI 打磨：Claude Cream token 逐项对齐；面板头改 VSCode 式图标视图排 + 标题行图标操作（无下划线、无文字按钮）；卡片常驻圆角边框；触控板双指缩放（ctrl+wheel，光标锚点）；app 图标 `app/build/icon.{svg,png}`。截图 `tmp/desktop-frontend/ui-*.png`、`pinch-in.png`
- [x] M3 编辑/重译/单块覆盖：引擎 c2e8d322（apply_edit/retranslate 写本篇 `block_edits`，随后缓存 run 应用；新事件 `block_edits`）；前端块编辑器（胶囊、恢复/重译/应用、单块字号/行距/字体/对齐）。StoryScope P05-004 实测手改+0.85 字号→缓存重跑 8.6s→恢复保留字号，截图 `tmp/m3-ui/`。取舍：去掉 base_revision（单窗口+串行队列无冲突）、allow_extend（refinement 已自动扩展）；版面模型懒加载推迟
- [x] 用户反馈（编辑后全篇重译 + 7 项 UI）：引擎 c3fb2367（共享剥文字 Form、每页压缩一次：全篇缓存重编 88s→17.6s）、f9b8f362（会话内单页重编 `page_reopened`；段落事件改模型空间 HTML，修含原子段手改必回退；回退/落定计数改派生）。前端：`page_reopened` 只清该页问题；按页修订号只重画回写页；样式段去底色；块头 撤回/重新翻译此块/单独编译此块；视图图标 原/译/对照；双栏去分隔线与左栏滚动条；排版溢出回退页左上角"待动态编译"。实测 Similarity P02-003：会话首次编辑全篇 19.3s（无保留状态），之后 1.1–2.1s 单页，第 1 页画布未重画。截图 `tmp/desktop-frontend/shot3.png`
- [ ] M4 设置页+HTTP translator+user_instructions
- [ ] M5 整篇排版+存储管理+文档

## 专项知识

- `node:sqlite` 在系统 Node 24 与 Electron 44（Node 24.21）均可用 → 论文库不引入原生模块。
- 引擎 release：`engine/target/release/syncpdf-cli`。
- 教训：别用 `CARGO_TARGET_DIR` 让临时 worktree 共用 `engine/target`——`fixtures::dir()` 等编译期路径会串到临时目录，测试静默 skip / 旧协议产物残留。
- 引擎 `run_finished.ok` = 零回退/零保护冲突/零覆盖缺口；已发布译文看 `document_finished`，前端以 `ok || 已发布` 判完成。
- CDP 验收脚本在 `tmp/desktop-frontend/`（cdp/wheel/key/console.mjs）；wheel 坐标用 CSS px，不是截图像素。
- 引擎会话长驻：重编 release 后必须重启 app，否则仍跑旧二进制。
- `syncpdf-cli inspect --input <pdf> [--page N]` 可直接看每段 kind/align/缩进，排查排版判定先用它。
- 旧引擎缓存的段落记录（缺新字段）在 `snapshot` 按协议守卫丢弃，需重新翻译补齐。
- 界面偏好：按钮一律 codicon 图标（VSCode action-label：16px 图标 / 22px 框 / 圆角悬停底色），选中态用圆角底色不用下划线；配色以 claude-cream 主题 JSON 为准。VSCode 参考 CSS 在 `tmp/vscode-ref/`。
- CDP 模拟 pinch：`tmp/desktop-frontend/pinch.mjs`（mouseWheel + modifiers=2）。
- 流水线大段同步计算不让出执行权：会话的取消只能由 stdin 读线程直接触发令牌。

## 当前问题 / 下一步

- 下一步 M4：设置页 + OpenAI 兼容 HTTP translator + `user_instructions`（计入缓存键）。
- 待用户知悉（不在本任务修）：StoryScope 曾因 agy `incomplete response (status=ERROR)` 460s 失败（重跑成功）；bind 诊断 "form depth limit" 单篇 12,944 条，属后端 bind-degraded 工作流，已汇总进问题、明细进引擎日志。
- 已知：会话内首次编辑仍走全篇（引擎刚启动无保留状态，缓存全命中 ~20s）；pdf.js 渐进渲染，CJK 字体加载时截图可能只见上半页。
- 已知：页面首批渲染要等字体加载数秒，之后跳页 ~100ms。
