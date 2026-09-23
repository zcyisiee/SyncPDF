# Rust PDF 后端修复 · Task state

> 唯一维护者：用户与主 Agent；subagent 只读，不得另建副本。更新：2026-09-22，Codex接管、累积review和主树集成验收已完成。
> 状态：**完整后端目标持续active。上一轮已完成绑定集成及stale/CID/Size修复（有效进展）；当前继续R1布局/字体稳定性，并审计完整目标所需接口。整体未完成。**

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
- 流式交付要在块闭合且校验通过时发生，不等 EOF；页快照与最终发布的事务/幂等、错误传播仍需后续完善。
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

- p19 历史阻断已解除：PDFium 对重叠重复字符去重；目标仍绘制，`stream264/op165/code7`→对象108/path[0]→真实 font271:0→Differences `/four`→`'4'`。独立删除仍可见11/27/68像素变化，不是跳过空白。
- **已实现并局部验收的窄契约**：仅严格匹配的单code `Tj` 缺字符时，取原对象 `GetRotatedBounds` 四角经祖先Form变换后的页AABB及自身matrix的origin；Unicode来自可验证ToUnicode/Encoding。独立只读证据/统计携带真实font_id及身份，门禁复核，不冒充loose-char bbox、不借邻居。
- 新支路简单字体1字节、Type0仅Identity-H 2字节；Identity-V、未知/变长CMap、多code、空/冲突映射、无效几何继续拒绝。普通字符解码的继承边界未因此获认证。本轮仅改pdfium/bind及专属测试。
- **当前分工**：主控已完成字体定位与默认固定字号，继续接容纳失败提示；`/root/r1_coverage`在Orca树`../codex-r1-coverage`处理p7/p15真实coverage gap（不放宽门限/不全页fallback）；`/root/goal_contract_audit`已结束，只读报告在`../codex-goal-audit/tmp/backend-repair/goal-audit.md`。R1 gate未过先冻结后续共享契约；不调查p19或重启旧任务。
- **字体稳定性已复验**：`20c1242b`稳定资源编号，段落基线/fit用实际字体度量。63项font/typeset测试通过，旧代码red证据保留。`d23dac3a`固定字号后139项pipeline/typeset测试、strict clippy、release通过；真实论文两次1/3页和全23页运行的p1/p3在144dpi原始像素完全相同（无坐标归一化），其余21页未动，3份PDF均qpdf exit0。证据`tmp/backend-repair/font-stability/real-cross-run.json`。全篇fake仍209次overflow、2coverage gap、3self_check，整体质量未通过。
- **固定字号失败保护已接线**：指定论文p1/p3的26个overflow块全部fallback、qpdf exit0；该fake选页没有成功译文，不能算翻译完成。up-vns前三页测试同时证明能放下的块有中文和整页回退时源字形/位置完全保持；旧“每页CJK”断言在新策略下失败，已改为真实结果对应的双向守卫，证据`font-stability/overflow-{pipeline,e2e}-green.log`（前者记录首次失败，后者修正后通过）。整条错误传播/发布仍待R2。后续共享合同已写[07接口契约](07-后续接口契约.md)，未实施项明确标为待实现。
- 字体窄修`72d47b62`已完整review并合入；主控重新编译专属回归、原字体独立渲染对照，Noto CJK CFF与PT Sans TTF共7字墨迹IoU均1.0000。`../codex-r1-fontmap` worker已结束，树clean；该树旧论文fixture缺失的早退不算通过，主树完整夹具复验为准。
- 主控另定位并修复字体对象搬移后的trailer Size不一致；`b48001c1`。qpdf回归扩到table+stream，旧table模式red、新两模式green；最终真实1/3页和全23页PDF均qpdf exit0、无警告。
- 最终fake全部重跑并逐页查看：选择1/3页时其余21页文字与像素不变；全23页仍有p7/p15 coverage gap（0.0134/0.0086）、159次overflow+min_scale、p1链接label fallback、未ready的p1提前写入19个快照、3个参考文献页无CJK的自检error仍报ok。R2/后续遗留单列，不能算全篇质量通过。跨选页同页尚有Tm纵向漂移；仅诊断副本对齐Tm后p1/p3像素完全一致，生产未归一化，根因未认证。
- 新 stale guard：绑定时私有记录源页Contents有序ID与遍历到的页/Form/Do父流字节；同页不同快照不可混排，apply前整批比对。主控复现旧ABC→XBC误删并确认修复后拒绝、XBC不变；4项专项复验通过。它不认证字体/资源语义变更，调用层仍须保证两套解析器读同一不可变原件。
- 旧Orca树`codex-r1-review`及`codex-r1-fontmap`所有worker已结束；保留证据，不重启。新树brief均在各树`tmp/backend-repair/brief.md`，target/runtime各自独立、vendor/fixtures只读。

## 5. 恢复入口与证据（详情不在本文件复制）

- 主树：`/Users/zhengcaiyi/orca/workspaces/ieeTranslater/桌面端`，当前HEAD`c3850be6`；绑定继承dirty全部保全并只提交10文件为`b47c7283`，文档独立提交`4363a5f3`，无关cache仍保留。brief必须给出本文件绝对路径，叶子跨树只读。
- [本轮集成验收](06-R1绑定集成验收.md)；[Codex历史交接](handoff-codex.md)；[执行计划/阶段历史](05-后端修复执行计划.md)；[失败基线](04-合并后引擎快速验收.md)。最新主控验收/独立probe/保全：主树 `tmp/backend-repair/codex-r1-integration/`；之前验收在 `tmp/backend-repair/parent-review/{object,cmap/final,identity,shared}/`，原始调查在绑定树 `tmp/backend-repair/p19-evidence/`。
- 运行产物只放各树 `tmp/backend-repair/`；target/runtime 不跨树共享可写；vendor/fixtures 只读，不碰 `~/.sp` 或既有未跟踪 `cache/`。环境命令见执行计划及各 brief。
- 已提炼本轮已验证的[PDF绑定与渲染经验](../../lessons/pdf-binding-and-render-evidence.md)；整体后端任务未完成，不将阶段成功写成产品通过。
