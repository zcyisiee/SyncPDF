# Rust PDF 后端修复 · 唯一 Task state

> 更新：2026-09-23，主控维护；只允许用户和当前主 Agent 修改，叶子只读，不另建副本。
> **交接就绪：用户要求由下一位 Agent 继续，本会话停在文档交付处。后端整体未完成。** R1/R2/R3工程与V3 GPU已验收，真实MVP可查看但明确为部分译文。当前入口：[最新交接](handoff-next-agent.md)。

## 1. 用户目标与不可改变的偏好

- 优先后端，不做界面、不改Electron；快速迭代真实可看的MVP，让用户依据PDF指导后续。最新反馈是“为什么回退原文、能否修复”，下一轮首要减少整段不翻译。
- 完整目标：译文样式对应原文；每页A3横向dual，左原文右译文；中文目录/书签；超链接正确保留；作者、机构/地址、脚注、图片、reference等按策略保护。
- Markdown one-shot；首个闭合、通过校验的译块立即编译；主请求/补救分别计数，不能拿fake冒充真实模型。
- Knuth–Plass正文两端对齐，字体/位置稳定。**保持原文或用户指定字号，禁止自动缩字号；容纳失败明确提示。**当前也不自动缩行距；二次编辑适用同样规则。
- 后端最终须支持按layout块修改字体、字号、可选译文并重新编译/导出，revision与失败原子性可靠。目前编辑接口未完成。
- 必须PP-DocLayout-V3与Apple GPU。本机已通过CoreML CPUAndGPU实现，非PyTorch MPS。生产不调用LaTeX；旧bdt/TeX仅作质量对照。
- 对外唯一入口bdt；`syncpdf-cli`是内部sidecar，不增加第二工具包。现有`bdt rust-translate`是获准子命令；旧Python路径保持原行为。
- 保留全部dirty/untracked，特别是既有`cache/`；不reset/clean/stash/覆盖他人改动。不重启已结束worker，**不重复p19绑定调查**。
- 若委派，遵循用户指定的 **gpt-6-sol:high、fresh context、Orca独立worktree**；主控分工、亲审完整diff/复验/集成。叶子不委派、不写task-state、不push/merge；一树一writer，默认2轮/30分钟、提前5分钟checkpoint。先读`docs/guide/delegation.md`。

## 2. 当前主树、活动状态与恢复入口

主树：`/Users/zhengcaiyi/orca/workspaces/ieeTranslater/桌面端`，分支`feat/desktop-develop`。

- 交接文档前HEAD `e0abcea8`，最新实现`9939ed9b`；本次交接文档提交在其后，以git log/status为准，不能reset到快照。
- 本次交接开工时只有`?? cache/`，无未提交源码；本次只修改交接/状态文档，独立提交。
- `../repair-r1-bind` HEAD `b47c7283`、干净；候选已完成累计review/保全/集成（`3f70cc2c`），旧交接第5节已过时。
- 无活跃子代理、syncpdf-cli、cargo/rustc任务；所有R1/R2/R3/GPU/MVP叶子已结束，不用等待或恢复。`codex-mvp-provider-check`和`codex-mvp-bdt-bridge`已由主控验收结束。
- `../repair-r1-layout/tmp/backend-repair/ortlib`仍是本机ORT库只读位置，不能删该树。各树target/runtime独立，不跨树并发共享写入。
- 唯一状态绝对路径即本文件；brief必须指定此路径，子树不得另建可写状态。

## 3. 已验证的实际能力

- PDFium/lopdf同一不可变来源的绑定、CMap/code边界、共享流隔离、stale门禁、页候选原子发布；继承Resources及q/Q字体/字距/缩放/leading恢复，含跨Contents流。
- 精确TJ名义推进（原数值、Tc/Tw、引号）、字体CID/GID映射和xref Size修复；未知简单字体宽度不猜1000/墨迹宽，保留拒绝边界。
- 源阅读序空格与真实glyph身份分开；作者/机构/脚注等保护；Markdown闭合块流式交付、错误传播；快照不重放未就绪页，失败不修改已有revision/文档。
- 精确逐run字号/颜色/源字体角色、源首行基线、实际font ink、cluster/fallback、Knuth–Plass与安全frame/栏归属；固定字号不可容纳则保留原文。
- V3锁定bbox模型+CoreML已通过真实Apple M5 Pro计算计划/ORT profile举证。Rust实际ORT1.23.2；全篇推理中位约99ms/页，初始化仍明显，不优先再做性能专项。
- `bdt rust-translate`通过本机pi真实翻译；`--cached-from`只读复制旧译文缓存、重新校验、跳过全部模型请求，缺失块回退并完成所有页保存/自检/发布。成功块统计需PageReady，typeset准备好不等于写入。
- 普通Number原子仅在源范围/单一样式明确、段落无注释相交条件下按原文回填。公式、引用、URL等尚未完整放置，不能把Number支持说成完整公式支持。

当前行为与边界详见[参考](../../reference/rust-pdf-backend.md)；实施计划不等于现有功能，见[接口契约](07-后续接口契约.md)。

## 4. 当前交付与回退基线

**用户已收到** `tmp/backend-repair/mvp-20260923/`：`translated.pdf`（23页）、`preview-3-pages.pdf`、`README.md`未成功清单、events/result/review-summary/source-retention JSON和PNG。

- **107块实际写入、55块回退**：40原子未放置、12固定字号排版失败、3缺有效译文。
- 另40源区域重叠、1旋转侧注提前保留，不算上述55；作者/脚注/reference等策略保护不是失败。
- 158段真实缓存命中，0主请求/0补救；18.993秒，23个PageReady，0 error事件，完成最终发布。
- 62,943个保留字符位置/字号/颜色变化0，qpdf通过；已目视重点页，但仍有中英混排与留白。**未通过完整论文质量验收**。
- RunFinished false / bdt exit1正确表示部分结果，不能为了“通过”改状态或压低warning。

真实历史：

| 目录（`tmp/backend-repair/`下） | 事实 |
|---|---|
| `mvp-real-p1-3` | pi63.10秒，16成功/14回退，1主/0补救 |
| `mvp-real-full-v1` | pi352.39秒，90成功/72回退，1主/10补救；**当前复排用此真实缓存** |
| `mvp-real-full-v2` | 数字修复后107块准备好，但补救返回unknown escape，4块未落定；不是最终发布 |
| `mvp-20260923` | 缓存重编译最终交付，107写入/55回退 |

主请求中的首个typeset133.50秒、首页落盘136.10秒，翻译阶段351.94秒才结束，已验证边译边编译。4个缓存未命中中的1个先记原子回退，所以translate_missing只有3，勿误报数字。

## 5. 唯一下一步与验收方式

下一位主控从**减少整段回退**开始，优先常见引用及其链接几何，再扩展公式源绘制；随后处理区域归属和排版误判，尽快提供改进后的PDF。此次仅交接文档，尚未开始新R4实现。

- 40原子回退需按kind/链接关联细分，不能全部当数学公式。一个引用导致整个正文段不译是当前最大缺口。不得直接删除保护分支。
- 40源区域重叠需可靠glyph归属/行内分割，不放宽coverage或扩大不译区域消统计。
- 12 overflow还未逐块归因：可能为frame、actual ink、碰撞、shaping等；先修误判，真空间不足保持字号并提示。
- 缺有效译文涉及模型输出协议/字面量/语言校验；不要盲目重试或放宽校验。修排版先用真实cache-only，不重复花模型时间。
- 原子几何仍有估算：`typeset.rs::para_glyph_bbox`返回None，LineBox.kept_atoms只存ID；缺真实源绘制/目标位置接口。`links.rs`尚无译文Rect/QuadPoints重建。具体陷阱与入口见[交接第5节](handoff-next-agent.md#5-下一个具体任务减少整段回退)。
- A3 dual、中文目录、链接重排、字体/字号编辑及revision重编译、打包分发仍未完成；RTL ActualText等通用PDF边界也不能泛化宣称支持。
- 每批主控审diff、必要行为测试、真实PDF内容/渲染/链接验收；不要只看测试数或文件存在。保留旧失败及MVP，用新tmp目录做对比。

## 6. 验证与证据索引

| 阶段 | 有效结果/文档 |
|---|---|
| 绑定集成 | [06](06-R1绑定集成验收.md)，原dirty已保全；p19已修，无需重做 |
| R1基础 | [08](08-R1布局与固定字号验收.md)，567/0/4 ignored |
| R2源文/Markdown/事务 | [09](09-R2源文本与Markdown验收.md)，591/0/5 ignored及真实probe |
| R3固定字号/排版 | [10](10-R3固定字号与排版验收.md)，641/0/6 ignored，随后宽度/qQ专项 |
| V3 GPU | [11](11-V3-GPU验收.md)，接线后workspace648/0/7 ignored（MVP前快照） |
| 最新MVP | [12](12-MVP真实翻译验收.md)，相关Rust269/0/5 ignored，strict Clippy/fmt/release；bdt26pytest/Ruff；真实PDF保护通过 |

最新日志在`tmp/backend-repair/mvp-runner/`；GPU/R3在`tmp/backend-repair/layout-final-review/`、`tmp/backend-repair/codex-r1-integration/`。最新269不是全workspace数字；已有无文本夹具早退/ignored不当有效真实覆盖。文档strict构建旧HTTP→pipeline中文锚点告警仍失败，不能计通过，不扩大修复范围。

本机输入：`/Users/zhengcaiyi/Downloads/2106.04690v2.pdf`。从主树先`source tmp/backend-repair/codex-r1-integration/env.sh`；Python用`~/miniconda3/envs/bdt/bin/python -m babeldoc_tools`。完整可复跑命令、缺动态库/Node处理见[最新交接](handoff-next-agent.md#6-本机环境与最快复现)。不改共享conda、不碰`~/.sp`或既有cache。

本任务可复用经验已经进入[PDF绑定与渲染经验](../../lessons/pdf-binding-and-render-evidence.md)。任务尚未完成，不因交接而标完成；用户自行启动下一位Agent后，由新主控继续维护本文件。
