# R2 源文本、Markdown 与发布一致性验收

2026-09-23，主控审阅独立任务完整diff、主树复跑并接线。**本批工程门禁通过，整体后端与真实译文质量未完成。**唯一状态见[task-state](task-state.md)。

## 已实现

- `SourceTextSpan`把逻辑文本和真实源字形分开，生成空格不伪造可删除字形；翻译单元消费同一映射。旋转侧注及重复字形归属保留并提示。脚注不译；首页标题至摘要间结合机构/邮箱/姓名布局保护作者信息。
- 模型输入输出改为版本化Markdown：显式块边界、样式身份、KEEP原子、硬换行；结束标记闭合立即校验交付。坏语法、半块、尾部通道失败上抛；此前有效块保留。默认整文一个主请求，显式容量不足报错；补救请求另计。缓存键含传输版本，命中仍校验。
- 页提交只重放已保存页与当前页；私有候选删除/渲染/原子保存成功后才更新主文档和revision。保存失败、缺绑定、未知ID传播；重复落定幂等。最终发布最后一个已验证快照。
- CJK自检来自实际替换内容；回退或源区域冲突时保存部分结果且ok:false/CLI非零。未放置atom的段暂时整段保留，不能冒充完整公式排版。

## 主控验证

证据目录：主树`tmp/backend-repair/layout-final-review/`。

| 验证 | 结果/证据 |
|---|---|
| workspace | 591通过、0失败、5 ignored；`r2-workspace.log`。ignored含两项真实手工probe及2doctest、1新增首页probe；绑定/coverage之前已显式验证，不重复p19 |
| metadata接入后的专项 | 5通过；`r2-metadata-parent.log` |
| 最终真实首页 | 手工probe显式通过，4段作者/机构/邮箱保留，正文词间空格恢复；`r2-source-p1-metadata.{log,json}` |
| strict Clippy / fmt / release | 通过；`r2-workspace-clippy-v3.log`、`r2-release.log`。前两次clippy的测试导入/闭包drop告警已修正 |
| 事务与提前交付 | 控制模型门闩证明交付早于响应完成；真实PDF测试证明待处理页保持原文、保存失败不改既有文件/状态；见workspace内`transaction_tests`和`repair_stream_incremental` |

## 指定论文23页 release

产物在`tmp/backend-repair/codex-r1-integration/r2-markdown-safe-all/`。32.47秒，Markdown主请求1次、补救0、缓存0。99段overflow、63段atom未放置、40段受保护区域重叠、1段旋转侧注。

**仍无成功译文。**23页提取文本与144dpi像素均与原件完全一致（`source-retention.json`），qpdf exit0（`layout-final-review/r2-qpdf.log`）。自检无错误，23个PageReady，RunFinished ok:false、CLI exit1，已正确表达不完整结果。全回退样本本身不能证明译文快照隔离；该行为由有实际CJK替换的受控真实PDF测试验证。

## 尚未验收

几何空格启发式不保证所有PDF的词边界，首页原有控制字符未修复；作者保护只覆盖有可靠首页标题/摘要边界与锚点的布局。逐run字号/颜色、真实fallback字体、cluster安全、Knuth–Plass、实际可用排版框、原子绘制和链接目标框仍待实施。A3双栏、中文目录和二次编辑后端仍待闭合。fake不代表真实LLM翻译，完整目标保持active。
