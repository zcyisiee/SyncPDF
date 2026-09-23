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

Writer 为每个字体按原 GID 首次出现顺序分配 CID，内容流使用 CID。finalize 在同一次子集化后把 CID→原 GID 转成显式 CID→子集 GID；Encoding、宽度表与 ToUnicode 均以内容 CID 索引。字号只写入 `Tf`，`Tm` 保留无量纲横向缩放，避免字号平方。字体 Type0 字典搬入预留资源槽后，删除其临时尾对象时同步回收该最高对象号，保证表式 xref 的 Size 一致。

ActualText/ToUnicode 的文本检查不能证明实际字形正确；内置 Noto CJK CFF 与 PT Sans TTF 已用原字体独立渲染对照。此项不认证任意字体或完整排版质量。

## 尚未闭合的后端能力

页内 `PatchSet` 的失败原子性不等于整条 pipeline 的发布事务已完成。段落空格/阅读序、准备失败后的删除一致性、未就绪页快照、错误传播/退出码，以及排版、公式可见性和链接重建仍需各自验收。生产 Rust 排版不调用 LaTeX；TeX/旧 bdt 可用于质量对照。

验证命令与证据保存在专项交接和 task state。所有测试缓存、截图、日志保留在仓库 `tmp/`；不使用用户文档库 `~/.sp` 作测试库。
