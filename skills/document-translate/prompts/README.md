# legacy 提示词（保留兼容）

本目录是旧 `experiments/batch_translate.py`（sheet 分批 JSONL 协议）与
`experiments/markdown_translate.py`（整篇 Markdown）使用的提示词。

新版工具层（`babeldoc_tools/`，仓库根）从 `../agents/` 读取提示词：

| 旧文件 | 新位置 | 用途 |
|---|---|---|
| `markdown-translator.md` | `../agents/translator.md` | 整篇翻译（`translate_document`） |
| — | `../agents/translator-repair.md` | 按 id 重译（`retranslate_ids`） |
| `format-reviewer.md` | `../agents/reviewer-protocol.md` | 结构/协议审查清单 |
| — | `../agents/reviewer-fidelity.md` | 回译校验（`backtranslate_check`） |
| — | `../agents/reviewer-layout.md` | 版式视觉审查 |
| — | `../agents/layout-fixer.md` | findings → layout patch 决策 |
| `translator.md` | （保留） | 旧 sheet 分批协议的翻译提示词 |

两个目录的提示词都带 ```text 代码块，`load_prompt()` 取块内内容；
旧流程改动请只改本目录，新流程改动改 `../agents/`。
