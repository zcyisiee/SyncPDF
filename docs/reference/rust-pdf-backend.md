# Rust PDF 后端当前边界

`engine/` 是正在开发的 Rust 后端；`syncpdf-cli` 用作内部 sidecar 与开发验收。产品入口约束仍是 `bdt`。这里描述代码现有行为，不表示已完成全篇翻译质量验收；专项进度见 [Rust 后端 task state](../reports/2026-09-22-rust-electron-rewrite/task-state.md)。

## 解析与绑定

PDFium 提供对象/字符几何，lopdf 提供源操作与字节。调用方必须保证两者读取同一不可变输入版本；先全页绑定，再修改 lopdf 文档。`BoundPage::check_replacement` 在 source 返回翻译输入前和公共删除 API 中检查已知异常、重复源操作、页身份与完整源跨度。

真实源 code 与提取字符不一一对应：generated 空格不具有可删除字节；连字可包含多个 Unicode 字符；折叠空格保留源 code；明确空映射无法得到可信几何时拒绝替换。CMap 支持 bfchar/bfrange 与 UTF-16 代理对。通过门禁不代表未知编码、所有 fallback 或全部 PDF 已获认证。

PDFium 去重后缺少字符的对象，仅在严格匹配的单 code `Tj`、自身有效 rotated bounds/matrix、有明确 ToUnicode/Encoding 证据时恢复。对象几何与字符几何分开记证据和统计；Type0 恢复仅支持完整 Identity-H 双字节 code。多 code、TJ、Identity-V、未知/冲突映射继续拒绝。

## 删除与写回

`PatchSet::delete_glyphs(&BoundPage, ids)` 先检查整批请求再入队。pipeline 将同页已准备的段落汇成一批，使用 1 基页号。apply 在私有文档候选上克隆目标页 Contents 和资源容器，唯一顶层 Form 克隆并按精确 Do 操作改名；全部成功后才替换原文档。共享流的其他页面不应随目标页删除而改变。

成功 apply 后旧绑定失效；若要再次编辑同页，需保存并由新版本重新绑定。重复 Form 实例在 source 拒绝；嵌套/重复目标 Form 不支持写回。绑定时私有保存页 Contents 有序引用和遍历到的源流解码字节；同页不同快照不能混排。apply 在候选修改前整批比对，拒绝同 ID 流的原地修改以及 Contents 的添加、移除和重排。这项门禁不认证字体/资源对象的语义变更，也不替代两套解析器使用同一原件的调用约束。

## 译文字形与字体嵌入

布局后处理仅凭可核实的物理行或源框线修正分区：同栏正文末行的漏出续文归入独立 Text 行；算法标题、三条源横线及贯通栏缝共同支持时，拆开 Code 与右侧说明。证据不足时保持原分区并报告 coverage gap，不以扩大不可译区域消除统计缺口。可译段与 Formula、Code、Table 等保留区域共享字形时，暂时保留整个段并发 `protected_source_overlap` 提示；这是源绘制保护，完整行内原子排版仍未实现。

Writer 为每个字体按原 GID 首次出现顺序分配 CID，内容流使用 CID。finalize 在同一次子集化后把 CID→原 GID 转成显式 CID→子集 GID；Encoding、宽度表与 ToUnicode 均以内容 CID 索引。字号只写入 `Tf`，`Tm` 保留无量纲横向缩放，避免字号平方。字体 Type0 字典搬入预留资源槽后，删除其临时尾对象时同步回收该最高对象号，保证表式 xref 的 Size 一致。

ActualText/ToUnicode 的文本检查不能证明实际字形正确；内置 Noto CJK CFF 与 PT Sans TTF 已用原字体独立渲染对照。此项不认证任意字体或完整排版质量。

字体包按资源名排序分配字体编号；段落基线与容纳计算使用段内实际字体的度量，不读取无关的固定编号字体。默认排版保持请求字号与行距，容纳失败返回 `Overflow`，不自动缩小字号。显式配置旧阶梯的库调用仍保留兼容性；生产 pipeline 使用固定字号默认值。溢出段不进入删除/译文写入集合，保留原文并发带页号和段落ID的 `typeset_overflow` 提示。源StyleRun保留精确字号和颜色，排版/Writer逐run传递；源行距以pt保存，在typeset适配处转为字号倍数。源serif/mono角色参与目标字体选择，编辑覆盖仍需后续接线。真实fallback身份、cluster安全和实际墨迹容纳仍待接线验收。

## 源文本与 Markdown 传输

`Paragraph.text_spans` 将阅读序文本映射到真实源字形范围；由几何证据生成的词间/行间空格使用零长度范围，不产生可删除的假字形。段落文本、原子识别与翻译单元使用同一映射；保留显式空格、连字和行末连字符。缺少几何分隔证据时不能保证所有词边界都能恢复。旋转侧注和共享同一源字形的多个可译段暂时保留源绘制并提示。`FootNote` 与代码、reference 等类别不参与翻译。首页有可靠标题/摘要边界时，结合邮箱、机构和相邻姓名/地址布局保护作者信息；不支持的布局不泛化猜测。

模型输入输出使用 `syncpdf-markdown-v1`：独占行 `<!-- syncpdf:block P01-001 -->` / `<!-- syncpdf:end P01-001 -->`，样式 `[文字]{style=1}`、原子 `{{KEEP_1}}` 和反斜杠行末硬换行。完整闭合块立即校验并交付排版，不等模型 EOF；语法损坏、半块或通道尾部失败明确上抛，此前已交付块保留。HTML 仅保留为内部单元/校验/事件兼容格式。

默认一个主请求，不按页自动切片；显式 `PromptSpec.max_chars` 限制 system+text 字符数，超限不发该请求。主请求和有界补救请求分别计数，通过 `translation_requests` 事件报告。缓存 hash 包含传输版本，命中仍需结构校验。未知/重复输出块不进入排版并阻止完整成功状态。真实模型的容量、输出协议遵守和全篇质量仍需实际验收。

## 尚未闭合的后端能力

pipeline 的页提交先克隆候选主文档，删除该页成功段落，并仅重放已提交页与当前页的译文；原子保存成功后才替换主文档、更新 revision 和发 PageReady。重复落定块幂等；未知块、缺绑定、删除/保存错误传播并停止后续处理。输出自检只对实际写入 CJK 的页要求 CJK；自检/链接对照错误阻止发布完成事件，最终发布沿用已验证的最后快照。

部分段落回退、源区域归属冲突或 coverage gap 时，保存可读的部分结果并发 `translation_incomplete`，RunFinished/RunSummary 的 ok=false，CLI 非零退出。reference/脚注等按策略保留的内容不算失败。含尚未可靠放置源绘制的行内 atom 时，暂时整段保留并提示 `atom_source_unplaced`；该保护不能代替完整公式排版。

段落空格/阅读序、逐 run 样式、公式可见性和链接重建仍需各自验收。生产 Rust 排版不调用 LaTeX；TeX/旧 bdt 可用于质量对照。

验证命令与证据保存在专项交接和 task state。所有测试缓存、截图、日志保留在仓库 `tmp/`；不使用用户文档库 `~/.sp` 作测试库。
