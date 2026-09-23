# Rust PDF 后端修复 · Task state

> 唯一维护者：用户与主 Agent；subagent 只读，不得另建副本。更新：2026-09-23；Codex 主控维护。
> 状态：**完整后端目标持续active。上一轮已完成绑定集成及stale/CID/Size修复（有效进展）；R1基础工程验收通过；R2源文本、事务状态与Markdown工程验收通过；下一批R3排版。整体未完成。**

## 1. 用户意图与任务偏好

- **当前优先打通输入 PDF → 输出译文 PDF 的后端链路**，不转去做界面。继承 hjfy 内容流级翻译路线，持续推进到真实全篇质量验收，不把局部测试通过当作完成。
- **用户最新完整目标**：译文样式对应原文；dual每页A3横向、左原文右译文；目录中文；超链接正确保留；作者、地址、脚注、图片、reference等无关内容不翻译；**Markdown one-shot**翻译，首个闭合译文块即开始编译；Knuth–Plass两端对齐；字体/字号/定位稳定；后端提供按layout块修改字体、字号并重新编译/导出的能力。目标不可缩成局部测试通过，逐项真实验证后才能完成。
- **用户已明确确认字号策略**：保持原文或用户指定字号，容纳失败时明确提示，禁止自动缩字号；适用于首次翻译及二次编辑。默认fit已固定字号/行距（`d23dac3a`）；`1adeea67`溢出块保留原文并定位提示，`c3850be6`真实夹具验证。逐run字号保真和编辑覆盖仍待接线。
- **生产不调用 LaTeX**；断行、美观程度接近 LaTeX，保留原文对应关系、公式、样式、图形与超链接，最终质量不逊于旧 bdt。TeX/旧 bdt 只作质量 oracle。
- 先后端，不改 Electron、旧 bdt 行为；不增加新的产品入口。质量与安全先于速度，不以跳过样本、放宽断言或压低 warning 伪装修复。
- **用户在 Codex 接手时更新：后续 subagent 固定 `gpt-6-sol:high`，fresh context，使用 orca-cli 准备独立 worktree。**主控拆小任务、冻结共享接口、亲自审 diff/复跑/合并；叶子不委派、不 push/merge。
- 每批整体验收通过才启动下一批；同一 worktree 一个 writer。恢复dirty前确认旧任务停止并保存证据，不从头覆盖已有工作；绑定恢复候选已由主控保全并提交，后续独立任务常规单独提交。
- 当前修复小任务默认最多两轮实质尝试、30 分钟、提前 5 分钟 checkpoint；只读调查可更短。遇到边界/未知先问主控；异步完成靠原生通知，不轮询等待。
- **Codex 接手已获用户授权**：按交接第 5 节继续；不重启已结束子任务、不重复 p19 调查、不做界面。原交接是历史快照；最新模型/工具偏好以本文件和用户本轮指令为准。

## 2. 专项知识与优化路线

- 参考：主树 `hjfy-architecture/ARCHITECTURE.md`、`hjfy-architecture/docs/reference/pdf-pipeline.md` 与 [本项目深挖](research/02-hjfy-engine-deep-dive.md)。这些是参考证据，**不等于当前 Rust 实现已具备全部能力**。
- 主链：PDFium 几何 + lopdf 源操作 → 布局/段落 → 带样式、原子与链接身份的翻译单元 → 流式结构校验 → 排版 → 精确回写/发布。公式图形优先保留源绘制，不重绘整页替代内容流编辑。
- code、Unicode、字符、glyph/cluster 并非一一对应；TJ 生成空格、连字、空映射、UTF-16、Form 调用实例必须区分。`matched` 数量不证明源操作、字节与几何同源。
- PDFium 与 lopdf 必须来自同一不可变输入版本；先绑定再修改。共享 Contents/Resources/Form 需隔离；不能借邻居几何补洞。已知不可信输入先拒绝，不能删除失败后仍叠加译文并报成功。
- 流式交付在块闭合且校验通过时发生，不等 EOF；页快照、保存事务、幂等与错误传播已实现，二次编辑revision事务仍待实现。
- 优化次序：可靠绑定/坐标/字号/流式 → 段落与事务状态 → Knuth–Plass 类 box/glue/penalty、cluster 安全、中文禁则与字体策略 → 公式/链接接线、容纳与视觉验收 → 性能。链接要随译文重建点击框，不只是保留 `/Annots`。

## 3. 已验收进度与未完成范围

| 项目 | 当前结论 |
|---|---|
| R1-writer 字号 | 主控验收并合入 `915120b1`；修正 Tf/Tm 重复缩放 |
| R1-layout 布局 | 主控验收并合入 `6c3d22d4`；模型输入/坐标修正，未放宽 coverage 门限 |
| R1-stream 真流式 | 主控验收并合入 `93e40ba5`；闭合块即时交付、尾部错误仍传播 |
| R1-bind 绑定/安全写回 | 累积review与同ID流stale窄修已完成；`b47c7283`经`3f70cc2c`合入，主树原件/共享页/失败不变性复验通过 |
| 本轮字体/序列化返工 | CID/GID `72d47b62`、Size `b48001c1`合入`d2a4d4ad`；原字体像素oracle、真实选页/全篇qpdf复验通过；全篇布局/事务质量仍失败 |

- 最终主树代码`d2a4d4ad`主控复验：workspace **553项通过、0失败、3 ignored**（manual原件probe在绑定合并后另显式通过，另2项doctest）；fmt/strict clippy/release通过。旧无文本夹具早退仍单列。证据在`tmp/backend-repair/codex-r1-integration/`，完整结论见[集成验收](06-R1绑定集成验收.md)。
- 真实 `up-vns` 12 页删除/非目标页保护保持通过；主控独立 probe 核对指定论文 **23 页、78508 字形，全部绑定门禁通过**。仅 p19 新增 1 个对象几何来源；52 个既有 Unicode fallback 仍单列。p19 实际删除与独立定点删除在三档 dpi 逐像素一致，其余22页不变；这不是完整译文质量或 R1 整批验收。
- 重复/嵌套目标 Form、旧绑定重复 apply 等仍有不支持边界；R2及后续排版/公式/链接/全篇视觉未完成。文档strict构建仍因既有HTTP→pipeline锚点告警失败（当前与干净HEAD同样失败），不计通过，详情见交接。

## 4. 当前问题与唯一下一步

- **R2工程验收通过**：[09验收](09-R2源文本与Markdown验收.md)。共享core `3e6838db`、事务/状态 `ce4b6e2b`、Markdown模块 `63e6e741`、源文本映射 `6209a3ee`、作者机构保护 `77e5e70d`已提交；主控Markdown接线 `0301c237`。主树workspace **591通过、0失败、5 ignored**；随后 metadata 5项和真实首页probe分别通过，strict Clippy/fmt/release通过。已有无文本夹具早退不算真实文本验收。
- **真实23页 release** `r2-markdown-safe-all`：32.47秒；主请求1/补救0/缓存0；99 overflow、63 atom_source_unplaced、40 protected_source_overlap、1 rotated_source_text；仍无成功译文。23页源文本及144dpi像素完全相同，qpdf exit0，自检无误报，RunFinished ok:false / CLI exit1，正确标明部分结果。fake只能证明工程行为，完整质量仍失败。
- **新增行为已独立举证**：闭合Markdown块在模型返回前交付；坏尾部/半块上抛且先前有效块可缓存；保存失败不改文档/revision/已有文件；未ready页不重放待排译文；未知/重复身份不计完整成功。行内atom尚未真放置，整段回退只是保护。
- **源文本**：几何生成空格与真实GlyphID分开；ligature/空映射/显式空格测试通过；最终真实首页“Training neural networks is costly”正确恢复。首页4段作者/机构/邮箱保留，标题/摘要/正文可译。几何启发式仍有边界，源p1控制字符等未声称修复。
- **当前R3分工**：新Orca树 `codex-r3-knuth` 实现纯box/glue/penalty最优断行模块；`codex-r3-shaping` 修字体grapheme/fallback/cluster范围；`codex-r3-frame-probe` 只读量测首页排版框/基线。三份brief已冻结独立API；均gpt-6-sol:high新叶子，前两项30分钟/2轮、probe20分钟。主控负责共享IR、逐run字号/颜色及layout接线和验收。固定原/用户字号，不能自动缩小；容纳失败继续明确提示。随后逐项接原子绘制、链接、A3 dual、中文目录、编辑重编译，再做真实LLM全文与视觉验收。
- **R3首个窄修已验证（本批未完）**：源line_height为绝对pt，旧typeset适配误作字号倍数再乘size；另源样式字号曾按0.5pt取整、整段字号取run数中位而非源字形加权。主控已接精确run字号/颜色、字体角色与pt转换，127项pipeline unit、33项typeset、真实混合字号/颜色PDFium专项及strict Clippy通过。up-vns第2页已出现可容纳译文，旧全页必须回退断言被更强的逐回退框源字形/坐标保护取代并通过。宽高/实际ink与Knuth–Plass未接，不算R3通过。
- 已结束的 `r2_source` / `r2_markdown` / `r2_metadata` 和所有R1 worker不重启；Orca独立树保留且clean。新分工严格gpt-6-sol:high/fresh/独立Orca worktree，task-state仅主控写。
- **R1基础验收**见[08](08-R1布局与固定字号验收.md)：567 passed /0 failed/4 ignored；strict Clippy/fmt/release及23页coverage probe通过。p7/p15可见字形缺口0，其余21页分区不变；错误扩大Caption/Code候选被拒绝。字体原始像素跨两次选页/全篇稳定，qpdf全过。R1旧全回退仍ok:true及错误自检已由R2修正。
- **禁止重复调查p19**：绑定集成与目标自身单code对象证据窄修已完成，详见06/历史handoff。共享/嵌套Form、未知编码等不支持边界仍保留；不以放宽门禁取得通过。
- docs strict构建既有HTTP→pipeline锚点告警仍失败，不计通过，不扩大修复范围。

## 5. 恢复入口与证据（详情不在本文件复制）

- 主树：`/Users/zhengcaiyi/orca/workspaces/ieeTranslater/桌面端`，本批提交见 git log 与08验收；绑定继承dirty全部保全并只提交10文件为`b47c7283`，文档独立提交`4363a5f3`，无关cache仍保留。brief必须给出本文件绝对路径，叶子跨树只读。
- [本轮集成验收](06-R1绑定集成验收.md)；[Codex历史交接](handoff-codex.md)；[执行计划/阶段历史](05-后端修复执行计划.md)；[失败基线](04-合并后引擎快速验收.md)。最新主控验收/独立probe/保全：主树 `tmp/backend-repair/codex-r1-integration/`；之前验收在 `tmp/backend-repair/parent-review/{object,cmap/final,identity,shared}/`，原始调查在绑定树 `tmp/backend-repair/p19-evidence/`。
- 运行产物只放各树 `tmp/backend-repair/`；target/runtime 不跨树共享可写；vendor/fixtures 只读，不碰 `~/.sp` 或既有未跟踪 `cache/`。环境命令见执行计划及各 brief。
- 已提炼本轮已验证的[PDF绑定与渲染经验](../../lessons/pdf-binding-and-render-evidence.md)；整体后端任务未完成，不将阶段成功写成产品通过。
