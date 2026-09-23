# Rust PDF 后端修复 · 唯一 Task state

> 更新：2026-09-23；仅用户和主Agent可修改，子代理只读。主树 `feat/desktop-develop`，起始HEAD `6e61bb19`；R4/R5/R6/R7已按逻辑拆分提交，既有`cache/`保留且不入库。
> **本轮R7完成：按用户要求先修链接/数学字符，再补有限动态bbox。CCS由88写入/69回退改善到154写入/3回退，原成功段无退化，原22点击框错位消除。仍是部分结果，后端整体未完成。** [验收报告](12-MVP真实翻译验收.md#ccs3764-r7) · [可复用经验](../../lessons/pdf-binding-and-render-evidence.md#先分清非空间失败再回收译后净空)。

## 用户意图与不可改变的边界

- 用户要求按逻辑分批提交，使用`fix`、`feat`、`docs`等分类和简洁清晰的中文说明；不提交运行缓存或验证产物。本批R4–R7已按上述要求整理提交，不改写已有提交历史。
- **YAGNI、先实测后优化、快速完成。** 当前样本为主树`ccs2026b-paper3764.pdf`；不做界面/Electron，不用通用重构拖延样张，不重复p19绑定调查，不恢复已结束worker。
- 先链接回退和数学字符，再动态bbox；首个闭合且校验通过的译块立即编译，Markdown one-shot，主请求/补救分别计数。缓存复排不能冒充新的真实模型测试。
- 源字号×0.9、行距=目标主字号×1.3，保留标题/正文/小字层级；不以失败后无限缩字、压行距或扩大碰撞容差求通过。源作者/机构/脚注/图片/reference等保护不动。
- R6用户决定：信任模型按目标语序调整已知样式，允许拆合/复用/省略和文本原子跨相邻样式；彻底删除目标语言字符比例门槛。保留块/KEEP/数字身份、已知样式和PDF安全。
- PP-DocLayout-V3，Apple GPU通过CoreML CPUAndGPU，非PyTorch MPS；生产不调用LaTeX。对外入口只有`bdt`，`syncpdf-cli`仅内部sidecar。
- 完整产品仍需A3横向dual、中文目录/书签、按块字体/字号/译文编辑与原子revision重编译；这些不能当已实现。
- 保留全部dirty/untracked，尤其既有`cache/`；禁止reset/clean/stash或覆盖他人工作。不改共享conda，不以`~/.sp`作测试库，测试产物只放本仓库`tmp/`。
- 若以后委派：先读`docs/guide/delegation.md`；用户指定gpt-6-sol:high、fresh context、Orca独立worktree，一树一writer；主控分工和验收，叶子不委派/不写本文件/不push。brief指定本文件唯一绝对路径，子树不另建状态。

## 当前交付与验收

**最终样张：`tmp/backend-repair/ccs3764-final/translated.pdf`。** 同目录有事件、result、`audit.json`、`math-and-figure-audit.json`及截图；脚本/构建测试日志在`tmp/backend-repair/ccs3764-fix/`。

- 21页全保存，**154实际写入/3回退**，另625策略/源保护不替换；新增66成功、原88成功无退化。
- 复用`ccs3764-real-v2`的157段真实DeepSeek缓存；0主请求/0补救，19.671秒。RunFinished=false、exit1、`engine_incomplete`，没有伪装完整成功。
- 319链接（317内部、2URI）、180命名目标、28书签保留；233点击框移动，全部保存后点击标签对应检查通过，原22错位消除。模板DOI仅保留，不认证网络可访问。
- 57,225个保留源字符位置/字号/颜色变化0，无KEEP泄漏，qpdf通过。30个数学字形与完整原字体独立渲染对照，3,272参考墨迹像素缺失0；第3页保留图区域像素完全相同。
- 同栏动态bbox第1轮救回P20-014/P20-015，基线分别上移约3.674/7.358pt。只是垂直净空重算，不是译后模型重检测，不跨栏/扩单元格/改字号。
- 最后3段为狭窄表格子标题：P05-007 `（a）MergeGuard`；P20-005 `（b）6 个中有 3 个后门。`；P20-021 `（b）8 个中有 4 个后门。`。两行译文会撞保留文字/框线，纵向空间不足，继续保留原文。
- pipeline库158 passed/0 failed/3 ignored；字体、typeset专项、两个PDF裁剪回归、35pytest通过；E2E的历史fallback断言改为最终状态后单独复跑通过。相关四crate严格Clippy、fmt、release、diff-check通过。**没有完成本轮全workspace验收**，不用R5全绿数字替代。
- `ccs3764-optimized/`中途曾有CoreML原生诊断污染stdout，正确返回`engine_events_invalid`；失败日志保留，后续独立复跑和最终目录事件流正常。原生stdout隔离风险尚未修复。

## 提交整理（完成）

- 从`6e61bb19`起按依赖顺序拆为5个`fix`（校验、墨迹、字体、PDF裁剪、文本原子与链接）、2个`feat`（字号/行距、动态bbox）及1个`docs`，标题均用简洁中文；共享文件分块暂存，没有整包混提。
- 代码和字体二进制散列与提交前一致；仅清理许可文本行尾空格，并修正CLI指南中“尚未动态回收空白”的过时说明。`cache/`、`tmp/`未提交，未push。
- 本次重新通过35项CLI/单入口pytest、Rust fmt及整批diff-check；未重新翻译或重跑完整Rust workspace，先前样张和验收范围不变。

## 本轮实现与代码入口

- `syncpdf-font/{assets,src/loader.rs,src/profile.rs}`：内嵌STIX Two Math 2.12 b168/OFL，追加fallback链，精确Unicode，不依赖系统字体或TeX。
- `syncpdf-typeset/src/layout.rs`：负侧承起笔纠正；已有逐glyph行间碰撞复核保留，容差不变。
- `syncpdf-pipeline/src/stages/text_atoms.rs`：数字/严格数字引用和精确HTTP(S) URL恢复；公式/不支持Other仍拒绝。
- `stages/link_text.rs`：有atom/无atom统一处理；KEEP精确锚点，旧缓存普通引用只接受唯一或上下文可消歧匹配，拒绝模糊/不支持注释。内部临时样式传递到实际目标字形，按行生成Rect/QuadPoints，Dest/A不动。`run.rs`在页候选内更新，保存失败不提交源文档/链接/revision。
- `syncpdf-pdf/src/bind.rs`：`n`清空未绘路径；Form入口CTM下BBox限制子绘图，修正第3页巨型假障碍。Rect会归一化端点，空交在构造前判断。
- `stages/refine.rs`、`run/refinement.rs`：源障碍减去真正被替换字形、加已接受译文墨迹；页保存前最多3轮，没改善即停。只重试合法但未排入的目标，不重绑修改后PDF。
- **事件消费变化**：PageReady前初始fallback可被精修typeset取代，按段ID取最新状态；保存后不再重排。重复模型块幂等与页原子发布边界保持。
- R6放宽校验仍在`syncpdf-translate/{validate,prompt,unit}.rs`；R5显式Typography仍只改变目标规格，不改翻译单元/缓存身份。

## 下一步与已知限制

本轮用户指定优化已完成并交付部分样张；若继续优化剩余3段，先针对子标题验证**真实单元格/同栏横向净空**或更简洁译文，不能任意越框或直接改缓存内容。可见留白、中英混排和语义翻译质量未全面验收。

- 625不替换包含正常策略以及38 `protected_source_overlap`、2 `translatable_region_overlap`等；这些在157翻译块/3回退之外。不能以少回退宣称整篇已译完。
- 通用公式源绘制、任意语言/注释格式链接、RTL ActualText等仍未认证。`links_check()`本身仍只检查数量和目标，必须加保存后点击几何验证。
- 数字单位正则会把`2 shows`/`250 samples`词首s识别为单位；本轮未改。修复会改变单元/缓存hash，须明确失效或重译受影响块，不能伪造缓存命中。
- 旧23页论文R5为147/15；R6后的旧论文重编译与4段缺缓存补译未做，不能把CCS改善套用旧样本。失败历史回包没有保存，错误码不能重建原译文。
- 现存strict docs构建有4处旧锚点警告，未当通过，不顺手扩修。源阶段旧诊断缓存可含修正前绘图障碍；最终生产运行重新绑定源页，验收以最终PDF/事件为准。

## 环境、恢复与历史证据

主树：`/Users/zhengcaiyi/orca/workspaces/ieeTranslater/桌面端`；唯一状态即本文件。当前没有活跃子代理、翻译或cargo任务；不恢复旧worker。R4–R7代码与文档已分批提交；既有`cache/`仍保留在工作区、未入库。

```bash
source tmp/backend-repair/codex-r1-integration/env.sh
cargo build --manifest-path engine/Cargo.toml --release -p syncpdf-cli
~/miniconda3/envs/bdt/bin/python -m babeldoc_tools rust-translate \
  "$PWD/ccs2026b-paper3764.pdf" --workdir tmp/backend-repair/<新目录> \
  --cached-from tmp/backend-repair/ccs3764-real-v2 \
  --layout-device coreml --font-scale 0.9 --line-height 1.3
```

ORT只读库仍在`../repair-r1-layout/tmp/backend-repair/ortlib`，不能删该树；各树target/runtime独立。macOS nohup可能清除DYLD变量，真实测试记录器曾在Python子进程环境恢复路径。

历史：原CCS `ccs3764-real-v2/`为88/69、22错位；1主请求/0补救，157缓存，首块149.009秒、首页153.060秒、翻译200.659秒结束，已证明真实流式。`ccs3764-test/`保留源清单、流式快照、链接错位反例，`ccs3764-diagnosis/`保留69回退分类。R7先141/16（链接/数学）、再153/4（初次净空/裁剪）、最终154/3；中间失败和样张全部保留。

旧样本`/Users/zhengcaiyi/Downloads/2106.04690v2.pdf`：`mvp-20260923`107/55，R4 `mvp-r4-20260923`144/18，R5 `mvp-r5-typography`147/15。R4/R5完整workspace数字仅属于历史；细节与命令见[累计验收](12-MVP真实翻译验收.md)、[参考](../../reference/rust-pdf-backend.md)、[经验总结](../../lessons/pdf-binding-and-render-evidence.md)。A3 dual、中文目录、编辑及整体质量仍待完成。
