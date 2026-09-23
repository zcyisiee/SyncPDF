# Agent 入口

非小改动先读 [当前架构](ARCHITECTURE.md)，再按任务查下表。代码是行为证据；发现文档与代码不符时核实并修正文档。未来方案不得写成已实现功能。

## 任务状态（开始与恢复时必读）

- `AGENTS.md`、`ARCHITECTURE.md`、`docs/` 是长期 **project state**；每个具体任务另维护唯一的 **task state**：默认 `.plan/<任务>/task-state.md`。已有计划目录可原地维护，不重复建副本。
- `task-state.md` 只允许**用户和主 Agent**修改；subagent 只读。记录用户意图/偏好、专项知识、进度与验收、当前问题/下一步，保持简短，细节链接到计划和证据。
- 主控在任务开始、compact/接管恢复、委派前先读 task state；偏好、决策、验收或阻断变化时及时更新。brief 必须指定其唯一文件的绝对路径，子 worktree 不另建可写副本。
- 完成后由主控把可复用经验提炼到 [`docs/lessons/`](docs/lessons/index.md)，再将 task state 标记完成并链接总结；不得把未实现方案升格成事实。完整生命周期见 [文档维护](docs/index.md#任务状态生命周期)。

当前 Rust 后端修复的唯一状态：[task-state.md](docs/reports/2026-09-22-rust-electron-rewrite/task-state.md)。

## 文档导航

| 文件 | 回答的问题 |
|---|---|
| [docs/index.md](docs/index.md) | 从哪里读、如何维护文档 |
| [docs/guide/cli.md](docs/guide/cli.md) | 怎样运行、验证与排查 |
| [docs/guide/delegation.md](docs/guide/delegation.md) | 子代理约束、harness 命令、brief 与主控验收 |
| [docs/reference/pipeline.md](docs/reference/pipeline.md) | 阶段产物、协议、续跑与存储归谁负责 |
| [docs/reference/http-api.md](docs/reference/http-api.md) | 上传、进度、编辑、编译的接口约定 |
| [docs/design/online-translation.md](docs/design/online-translation.md) | 在线部署目标、存储约束与待决策项 |
| [docs/issues/](docs/issues/index.md) | 已核实的重大缺陷：现象、证据、根因与候选方案 |

## 代码地图

- `babeldoc_tools/__main__.py`、`run.py`：`bdt` 参数与阶段编排。
- `babeldoc/tools/agent/`：内部 Markdown/IR 协议、重建与质量检查，无独立 CLI。
- `babeldoc/format/pdf/`、`babeldoc/docvision/`：PDF 引擎与布局后端。
- `babeldoc_tools/serve/`：HTTP、任务、草稿、SQLite 元数据与文件资产。
- `web/src/`：工作台（React）；`skills/document-translate/agents/`：模型提示词；`tests/`：行为守卫。

## 必守边界与验收

- 对外入口只有 `bdt`；新增能力只能是其子命令或参数，不增加并行入口或第二个工具包。见 `tests/test_single_entry.py`。
- 翻译测试、截图、日志和验证产物保留在本仓库 `tmp/`（已忽略），每次 pytest 使用新的 `--basetemp`。
- 改架构同步更新 `ARCHITECTURE.md`；改行为同步更新对应参考文档与必要的行为测试。
- 界面改动直接改 `web/src`（三栏工作台：左栏论文导航 / 中栏预览 / 右栏检查器三 tab + 底部操作区），行为同步改对应行为测试；本轮界面定稿用的静态设计稿 `web/design/` 已按此边界删除，不再维护第二份界面原型。
- 委派前必须阅读 [子代理委派规范](docs/guide/delegation.md)：仅主控分工，叶子直接执行；主控亲自审 diff 和验证。
- 完成前按 [验证说明](docs/guide/cli.md) 运行适用检查；不能把已有失败当作通过，也不要顺手扩大修复范围。

## 环境与共享存储

- 全局环境是 conda env `bdt`（`~/miniconda3/envs/bdt`，editable 安装自某个 worktree）：所有 worktree 共用，不再各自建 `.venv`。在 worktree 根目录用 `python -m babeldoc_tools ...` 运行本地代码（cwd 优先于 editable 目标）；从任意目录可用 `bdt`（走 editable 指向的那份代码）。切换默认代码树：在目标 worktree 里 `~/miniconda3/envs/bdt/bin/pip install -e '.[web]' paddlex==3.7.2`。
- 文档库统一在 `~/.sp`（`app.db`、`assets/`、各文档 workdir）：`bdt serve` 不带 `--root`/`--workdir` 时就是它。不要在 worktree 的 `tmp/` 下再建 library。

```bash
PY=~/miniconda3/envs/bdt/bin/python
$PY -m pytest --basetemp="tmp/pytest-$(date +%Y%m%d-%H%M%S)"
# 仅对改动的 Python 文件运行：
~/miniconda3/envs/bdt/bin/ruff check <files>
git diff --check
```
