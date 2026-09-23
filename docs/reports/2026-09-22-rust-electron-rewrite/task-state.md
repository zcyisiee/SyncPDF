# Rust PDF 后端修复 · 唯一 Task state

> 更新：2026-09-23；仅用户和主 Agent 可修改。主树 `feat/desktop-develop`。本轮“应译正文漏译与公式保护”修复及CCS样本验收已完成；完整Rust产品目标仍有后续工作。
> **最终：21页、193块全部写入，0回退、0送译前冲突、0覆盖缺口。** [验收报告](12-MVP真实翻译验收.md#inline-formula-coverage) · [经验总结](../../lessons/pdf-binding-and-render-evidence.md#inline-formula-ownership) · [缺陷及历史误报](../../issues/rust-inline-formula-coverage.md)。

## 用户意图与边界

- 修复Rust核心翻译后端：所有应译正文实际送译并正确编译，作者/机构/脚注/图片文字/reference等保持保护。用户已开放全部权限继续完成；本轮主 Agent 单独执行，不启动旧worker。
- YAGNI、先实测后优化；用户指定样本为主树 `ccs2026b-paper3764.pdf`。不做UI/Electron，不重复p19绑定调查，不用额外重构延迟实测。
- 源字号×0.9、普通基线间距=目标主字号×1.3，保持样式层级；公式保持原尺寸，仅在发生碰撞处增加所需行距。不通过缩字号/压行距/扩大碰撞容差求通过。
- Markdown one-shot，首个闭合有效块立即编译；主请求/补救分别计数。真实缓存复排不能冒充新的模型测试，不伪造译文缓存。
- R6用户决定继续有效：允许模型换序/拆合/复用/省略已知样式，删除目标语言字符比例门槛；保留块/KEEP/数字身份及PDF写回安全。
- PP-DocLayout-V3、CoreML CPUAndGPU；生产不调用LaTeX。唯一对外入口 `bdt`，Rust binary仅内部sidecar。
- 按逻辑分批提交，使用fix/feat/docs分类和简洁中文说明；不改写历史、不push。既有 `cache/` 保留且不入库；不reset/clean/stash。运行产物仅在仓库tmp，不改共享conda，不用 `~/.sp` 作测试库。
- 若以后委派，先读委派规范；原偏好为gpt-6-sol:high、fresh context、Orca独立worktree，一树一writer，叶子不委派/不写本文件/不push。用户未要求本轮新增委派。

## 正文覆盖修复验收（最新断行样张见下节）

**样张：`tmp/backend-repair/inline-full-v5/translated.pdf`。** 同目录保存事件、result、audit、qpdf日志、重点页PNG及cache来源；详细过程/失败尝试见验收报告。

- 193应译块全部写入，21页保存；576正常保护实体；run_finished=true、exit0。原第2页四块正文实际送译，第20页漏检及狭窄子标题全部写入。
- 最终1真实主请求/0补救/183真实缓存命中，补译10块，65.989秒。前序page2和全文运行也实际调用模型。最终cache由v3/v4原hash原译文合并，未手填内容。
- 94行内公式原字体/路径核验，53,384参考墨迹像素缺失0；成功段源框外36,327字符位置/字号/颜色变化0，第3页图片区域像素相同。
- 319链接目标保留，314点击框移动、全部点击标签对应；28书签/180命名目标保留。qpdf通过，无KEEP残留。
- 已目视p1/2/3/4/5/9/20；仍有源段落锚定留下的空白，不把覆盖通过扩大为任意PDF或模型措辞质量认证。此样本匿名，作者保护依靠已有前置信息行为守卫及本轮未改变的保护策略。
- 相关五crate528通过/0失败/8ignored；随后协议补译更改重跑translate115通过/0失败/2ignored。CLI/单入口36pytest；严格Clippy、fmt、Ruff、release、diff-check通过。未另跑全workspace/前端。
- strict docs仍有既存HTTP参考中文锚点警告，未当通过；本轮移除状态页过时链接并修正所编辑Rust参考的CLI链接，没有扩修其它文档。

## 本轮实现入口

- `paragraph/inline_formula.rs`：公式完整源归属与独立clip证据，上下标按正文基线分行；正文重叠按较小区域唯一拥有字形。数字单位正则补词界，避免`2 shows`的s误作单位。
- `syncpdf-core`的 `Atom.source` / `LineBox.placed_atoms`，`syncpdf-typeset`的 `Inline::SourceAtom`：KEEP与原尺寸几何贯通，局部必要行距、源/目标障碍同时核对。
- `syncpdf-pdf/src/source_atom.rs`：不可变源绘制隔离为Form，保留原字体/路径；仅私有副本剥离无关和嵌套图片文字，成功页裁去原公式路径后重放。仍在页级候选事务内。
- `layout.rs` / `frame.rs`：有标签、单行、紧邻且居中的图表外子标题恢复；按面板宽度居中排版，保留其它内容障碍。
- `link_text.rs`：公式内引用按实际原子位移更新注释，保留Dest/A。
- `markdown.rs` / `translator.rs`：只有正确闭合且ID可识别的坏正文进入既有最多3轮补译，不交付/缓存坏块，后续有效块照常流式交付；半块/边界损坏/通道失败仍报错。
- `rust_backend.py`：结果增加 `blocked_before_translation` / `coverage_gap_pages`，源冲突计入unsuccessful，覆盖缺口不能完整成功。

## 表18/19断行复核（2026-09-23，已完成）

用户指出表18/19断行不自然。已证实译文无换行/br；根因是无glue片段的短行代价过低及左对齐仍按justify选择断点，末行不计余量又留下孤字短尾。已按实际对齐评分、限制短行兜底、均衡ragged末行并避免自动单字末行；保留显式硬换行。新断行暴露的0.031pt右侧墨迹越界，仅用另一侧空隙平移纠正，没有缩字或放宽容差。

**最新样张：`tmp/backend-repair/caption-break-v3/translated.pdf`。** 21页193/0，0源冲突/覆盖缺口；193缓存命中/0模型请求，28.095秒。表18从9行→8行、最短中间行40.9%→78.0%；表19从7行→6行、14.3%→97.8%。193译文HTML和保存PDF非空白字符总量/身份不变。94公式、保护字符、图片、链接和qpdf全部通过；已目视第20页说明段。

234相关Rust单元/集成通过/5ignored；末行评分最后一处更改后typeset重跑65通过；Clippy/fmt/release/diff-check通过。文档仍有既存HTTP锚点警告。v1的192/1及v2收尾仍短的中间证据保留，不冒充最终结果。详细[验收](12-MVP真实翻译验收.md#caption-line-breaks)和[经验](../../lessons/pdf-binding-and-render-evidence.md#mixed-script-break-cost)已同步。修复代码提交 `6245d0f5`，文档另批提交，未push。恢复复排优先使用最新样张目录；inline-full-v5缓存文本未改变，仍可复用。

## 状态与下一步

本次漏译修复及后续表18/19断行修复、完整编译和保护内容验收已完成。已将经验提炼到上述lessons链接，架构/参考/CLI/issue/验收同步。代码按逻辑提交：`67958e9c`（源公式/正文/子标题）、`898ca829`（闭合坏块有界补译）、`d306583e`（完整覆盖统计）；文档另批提交。运行缓存与产物不提交，未push。

后续产品范围仍待用户安排：A3横向dual、中文目录/书签、逐块字体/字号/译文编辑与原子revision重编译。任意公式布局、任意语言/注释格式、RTL ActualText仍未全面认证。CoreML原生stdout偶发污染风险未在本轮处理。旧23页论文R6后重译未完成，本次CCS结论不能套用旧样本。

## 环境与恢复

主树：`/Users/zhengcaiyi/orca/workspaces/ieeTranslater/桌面端`；唯一状态即本文件。无活跃子代理/翻译任务，不恢复旧worker。

```bash
source tmp/backend-repair/codex-r1-integration/env.sh
cargo build --manifest-path engine/Cargo.toml --release -p syncpdf-cli
~/miniconda3/envs/bdt/bin/python -m babeldoc_tools rust-translate \
  ccs2026b-paper3764.pdf --workdir tmp/backend-repair/<新目录> \
  --cached-from tmp/backend-repair/inline-full-v5 \
  --layout-device coreml --font-scale 0.9 --line-height 1.3
```

上面仅缓存复排；新真实翻译去掉`--cached-from`。ORT只读库仍在 `../repair-r1-layout/tmp/backend-repair/ortlib`，不能删该树。开发日志 `tmp/backend-repair/inline-{tests,protocol-tests,pytest,clippy,build5,docs}.log`；审计脚本 `inline-audit.py`。

历史：R4–R7从6e61bb19后按逻辑提交。旧 `ccs3764-final` 为154写入/3回退，另40送译前冲突和1覆盖缺口；此前报告误导已纠正。历史样张/失败保留，详见累计验收与issue。不能把旧“3回退”当成整篇仅3段未译。
