# Rust PDF 后端修复 · 唯一 Task state

> 更新：2026-09-23；仅用户和主 Agent 可修改。主树 `feat/desktop-develop`。本轮“应译正文漏译与公式保护”修复及CCS样本验收已完成；完整Rust产品目标仍有后续工作。
> **最新浮动排版验收：1.0字号/1.5行距，21页、190块全部写入，0回退、0送译前冲突、0覆盖缺口；最新产物typography-float-v3，见[浮动验收](12-MVP真实翻译验收.md#typography-local-float)。** [标题验收](12-MVP真实翻译验收.md#heading-adaptive-layout) · [验收报告](12-MVP真实翻译验收.md#inline-formula-coverage) · [经验总结](../../lessons/pdf-binding-and-render-evidence.md#inline-formula-ownership) · [缺陷及历史误报](../../issues/rust-inline-formula-coverage.md)。

## 用户意图与边界

- 修复Rust核心翻译后端：所有应译正文实际送译并正确编译，作者/机构/脚注/图片文字/reference等保持保护。用户已开放全部权限继续完成；本轮主 Agent 单独执行，不启动旧worker。
- YAGNI、先实测后优化；用户指定样本为主树 `ccs2026b-paper3764.pdf`。不做UI/Electron，不重复p19绑定调查，不用额外重构延迟实测。
- 用户最新反馈当前行距太紧，希望至少1.5倍行距，并寻找合适字号；原0.9字号/1.3行距仅为历史基线。已用浮动空间修复支持1.0字号/1.5行距，保持样式层级；公式保持原尺寸，仅在发生碰撞处增加所需行距。不通过缩字号/压行距/扩大碰撞容差求通过。
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

## 标题语义与自适应宽度（2026-09-23，已完成）

用户要求commit前轮更新并继续解决短标题被迫换行。前轮6245d0f5/8dbb6836已提交。首页两个标题块同属Title区域，居中短续行Merging因左缘缩进误切；附录A/A.2又因编号后的悬挂缩进误切。已按同区、相近字号/行距及中心轴或编号后正文对齐证据合段，整段真实翻译。目标Frame从下方正文确定页/栏宽度，同排邻块/图片及实际墨迹继续阻挡，缺证据沿用原框；不改源IR、不缩字。

**最新样张：`tmp/backend-repair/title-adapt-v2/translated.pdf`。** 首页完整标题与A.2各一行；附录A完整译文超过栏宽，正常两行。21页190/0、0源冲突/覆盖缺口、176缓存+14真实补译、1主请求/0补救、56.255秒。此前v1仅修首页为192/0、179缓存+13真实补译、60.823秒；不把v1当最终全部标题验收。缓存只读备份，变更ID未篡改命中。

66,916应译源字形身份/数量不变且唯一归属；第1/14/15页以外156译文HTML及保存文本不变。94公式/53,384参考墨迹像素缺失0、36,327保护字符变化0，第3页图片像素相同；319链接/28书签/180命名目标正确，qpdf通过。已目视首页和附录两标题；仍保留源段落锚定空白。

pipeline167通过/0失败/4ignored；最后加强新编号守卫后专项1通过；真实inventory1通过。Clippy/fmt/release/diff-check通过，strict docs仍有既存HTTP锚点警告，未跑未改动Python/UI。首次比较脚本错误要求附录A也一行，实测译文较长后纠正验收预期，没有改译文/缩字求通过。详细[验收](12-MVP真实翻译验收.md#heading-adaptive-layout)及[经验](../../lessons/pdf-binding-and-render-evidence.md#heading-semantic-container)已同步。证据和日志均在tmp/backend-repair/title-adapt-*，缓存复排请使用v2。代码提交98782120，文档另批提交；未push。

## A3双语导出（2026-09-23，已完成）

用户要求commit现有更新并新增可选dual PDF：每页A3横向，左原文、右译文。前轮标题代码98782120/文档d819cffa已提交。本轮`bdt rust-translate --dual`经Rust生成额外`dual.pdf`，保留`translated.pdf`；PDF内容流/Form矢量拼页，按CropBox/旋转等比适配半页并居中。链接坐标/目的地同步变换，右侧同名目标解析为右侧显式目标，保留一套原文目录；发布前自检及请求产物存在门禁生效。未做UI/新并行入口/编辑Export协议。

**最终样张：`tmp/backend-repair/dual-v2/dual.pdf`。** CCS21页，每页420×297mm；190真实缓存命中、0模型请求、31.510秒，190/0、0源冲突/覆盖缺口。左右分别101,080/61,110非空白字符逐页与两输入相同；638链接点击框/目标/坐标、28书签、180命名目标通过，qpdf/保存后自检通过。已目视首页、公式页、表18/19；v1与补充门禁后的v2双语文件SHA相同。

相关Rust281通过/0失败/4ignored，门禁专项1及流读取dual专项2通过；CLI/单入口39pytest通过；Clippy/fmt/Ruff/release/diff-check通过。strict docs仅既有HTTP锚点警告。导出失败不覆盖输入/伪报成功；任意交互批注/表单/标签阅读树未认证。详细[验收](12-MVP真实翻译验收.md#a3-dual-export)及[经验](../../lessons/pdf-binding-and-render-evidence.md#dual-page-geometry)已同步。产物和日志均在tmp/backend-repair/dual-*。功能代码提交2b3493a4，文档另批提交，未push；既有cache保持未跟踪。

## 1.0字号 / 1.5行距与浮动bbox（2026-09-23，已完成）

用户要求行距至少1.5，并追问既有浮动bbox能否支持原字号。三档初测：0.9/1.5为190/0，0.95/1.5为189/1（P09-017），1.0/1.5为188/2（另P14-114）；均190真实缓存命中/0模型请求。0.9版本完整保护审计通过，仅作为比较基线，不能以缩字号代替浮动空间修复。

旧Python已有同栏/跨栏和条件跨页；Rust之前只做最多3轮同栏上下回收，固定横向宽度且不能让相邻已译段让位。本次在原页文字右边界内尝试向右扩展，重新核对全高障碍；还支持与紧邻下方已译段保持顺序、原段间距联排，两段都通过才一起更新。

真实v2：1.0字号/1.5行距、21页190/0，0送译前冲突/覆盖缺口，190缓存/0模型，31.768秒。P09-017实际是图4说明（此前称正文不精确），加宽21.662pt后断行成功、首基线下移0.298pt；P14-114附录标题和P14-115下方正文各下移1.617/6.459pt，两行标题保持栏宽。v1也190/0，但标题侵入右页边距，目视拒绝；v2约束原页文字边界后修正。没有跨页，不改默认字号/行距。

v2保护字符36,327变化0、94公式参考墨迹53,384缺失0、图3像素相同；638双语链接及28书签/180命名目标通过。原矩形链接审计将首页跨两行DOI的联合Rect内邻文误报为错误标签，逐行QuadPoints精查仅含完整URL且目标正确，证据wrapped-link-audit.json；下一轮使用逐行区域审计，不能把旧误报忽略不报。最终v3：21页190/0、32.113秒、0模型请求，两份PDF与v2 SHA一致；逐行链接标签0错误，单语/dual完整审计及qpdf通过。pipeline单元172+集成7项，共179通过/0失败/5ignored，Clippy/fmt/release通过；strict docs仍仅既存HTTP锚点警告。产物tmp/backend-repair/typography-float-v3/{translated.pdf,dual.pdf}。详细[验收](12-MVP真实翻译验收.md#typography-local-float)和[经验](../../lessons/pdf-binding-and-render-evidence.md#local-float-and-leading)已同步。修复代码提交4567c253，文档另批提交，未push。对比图仍在typography-comparison，未更新的1.0标签指修复前结果。本轮检测到用户同步修改AGENTS.md，保持其改动且不纳入本次提交；cache仍未跟踪。

## 状态与下一步

本次漏译修复、表18/19断行、标题适配、A3双语导出及1.0字号/1.5行距浮动排版均完成并通过对应验收。已将经验提炼到上述lessons链接，架构/参考/CLI/issue/验收同步。代码按逻辑提交：`67958e9c`（源公式/正文/子标题）、`898ca829`（闭合坏块有界补译）、`d306583e`（完整覆盖统计）；文档另批提交。运行缓存与产物不提交，未push。

后续产品范围仍待用户安排：中文目录/书签、逐块字体/字号/译文编辑与原子revision重编译。任意公式布局、任意语言/注释格式、RTL ActualText仍未全面认证。CoreML原生stdout偶发污染风险未在本轮处理。旧23页论文R6后重译未完成，本次CCS结论不能套用旧样本。

## 环境与恢复

主树：`/Users/zhengcaiyi/orca/workspaces/ieeTranslater/桌面端`；唯一状态即本文件。无活跃子代理/翻译任务，不恢复旧worker。

```bash
source tmp/backend-repair/codex-r1-integration/env.sh
cargo build --manifest-path engine/Cargo.toml --release -p syncpdf-cli
~/miniconda3/envs/bdt/bin/python -m babeldoc_tools rust-translate \
  ccs2026b-paper3764.pdf --workdir tmp/backend-repair/<新目录> \
  --cached-from tmp/backend-repair/title-adapt-v2 \
  --layout-device coreml --font-scale 1.0 --line-height 1.5 --dual
```

上面仅缓存复排；新真实翻译去掉`--cached-from`。ORT只读库仍在 `../repair-r1-layout/tmp/backend-repair/ortlib`，不能删该树。开发日志 `tmp/backend-repair/inline-{tests,protocol-tests,pytest,clippy,build5,docs}.log`；审计脚本 `inline-audit.py`。

历史：R4–R7从6e61bb19后按逻辑提交。旧 `ccs3764-final` 为154写入/3回退，另40送译前冲突和1覆盖缺口；此前报告误导已纠正。历史样张/失败保留，详见累计验收与issue。不能把旧“3回退”当成整篇仅3段未译。
