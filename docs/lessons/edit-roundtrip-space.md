# 可编辑数据必须与校验处于同一表示空间

**结论**：引擎发给编辑器、又会被用户改后送回校验的数据，必须用校验所在的表示空间发出。展示用的"解析后"形态只能另开字段，不能顶替。

**根因与证据**：`paragraph.translated_html` 曾是 `resolve_text(...).to_html()`（原子展开成原文文本），而手改覆盖按模型空间（`{{KEEP_n}}`）校验。结果是编辑器永远见不到胶囊，含原子的段一经手改就因 `placeholder_count` 回退；不含原子的段恰好正常，所以长期没被发现。修复见 f9b8f362：`Target.html` 改为 `parsed.to_html()`。

**以后如何做**：
- 新增"读出 → 编辑 → 写回"链路时，先写往返测试：取事件里的值、做最小修改、送回，断言不回退。
- 测试样本必须覆盖含特殊成分（原子、链接、样式段）的正例，普通纯文本不算。

回归：`run::incremental::tests::edit_recompiles_only_its_page_and_matches_a_full_run`（断言所改段含 `{{KEEP_`）。
