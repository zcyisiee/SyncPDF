# Agent 入口

非小改动先读 [当前架构](ARCHITECTURE.md)，再按任务查下表。代码是行为证据；发现文档与代码不符时核实并修正文档。未来方案不得写成已实现功能。

## 文档导航

| 文件 | 回答的问题 |
|---|---|
| [docs/index.md](docs/index.md) | 从哪里读、如何维护文档 |
| [docs/guide/cli.md](docs/guide/cli.md) | 怎样运行、验证与排查 |
| [docs/guide/delegation.md](docs/guide/delegation.md) | 子代理约束、harness 命令、brief 与主控验收 |
| [docs/reference/pipeline.md](docs/reference/pipeline.md) | 阶段产物、协议、续跑与存储归谁负责 |
| [docs/reference/http-api.md](docs/reference/http-api.md) | 上传、进度、编辑、编译的接口约定 |
| [docs/design/online-translation.md](docs/design/online-translation.md) | 在线部署目标、存储约束与待决策项 |

## 代码地图

- `babeldoc_tools/__main__.py`、`run.py`：`bdt` 参数与阶段编排。
- `babeldoc/tools/agent/`：内部 Markdown/IR 协议、重建与质量检查，无独立 CLI。
- `babeldoc/format/pdf/`、`babeldoc/docvision/`：PDF 引擎与布局后端。
- `babeldoc_tools/serve/`：HTTP、任务、草稿、SQLite 元数据与文件资产。
- `web/src/`：工作台；`skills/document-translate/agents/`：模型提示词；`tests/`：行为守卫。

## 必守边界与验收

- 对外入口只有 `bdt`；新增能力只能是其子命令或参数，不增加并行入口或第二个工具包。见 `tests/test_single_entry.py`。
- 翻译测试、截图、日志和验证产物保留在本仓库 `tmp/`（已忽略），每次 pytest 使用新的 `--basetemp`。
- 改架构同步更新 `ARCHITECTURE.md`；改行为同步更新对应参考文档与必要的行为测试。
- 委派前必须阅读 [子代理委派规范](docs/guide/delegation.md)：仅主控分工，叶子直接执行；主控亲自审 diff 和验证。
- 完成前按 [验证说明](docs/guide/cli.md) 运行适用检查；不能把已有失败当作通过，也不要顺手扩大修复范围。

```bash
export PATH="$PWD/.venv/bin:$PATH"
.venv/bin/python -m pytest --basetemp="tmp/pytest-$(date +%Y%m%d-%H%M%S)"
# 仅对改动的 Python 文件运行：
.venv/bin/ruff check <files>
git diff --check
```
