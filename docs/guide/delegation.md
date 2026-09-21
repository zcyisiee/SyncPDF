# 子代理委派规范

本文保存原 `AGENTS.md` 的完整委派约束。主控在委派前阅读；叶子执行者在开始任务前阅读。适合独立封装的实现任务，使用一次性的 coding harness worker 执行；用户要求主控直接完成时遵从用户要求。

## 分工与边界

1. **任务小而清晰**：把目标拆成可独立验证的最小执行单元，避免把宏大目标直接交给一个子代理。
2. **约束必须具体**：brief 写明可修改范围、禁止修改的文件、禁止尝试的方向、尝试次数上限和退出/回退条件，防止过度探索或无限循环。
3. **主控亲自验收**：worker 的报告或成功退出码都不是正确性证明。主控必须亲自审阅 diff，并运行相关测试验证，不能跳过。

只有顶层主控分派任务。被委派者是叶子，直接执行 brief；未经明确要求不得继续创建或委派子代理。

用户指定 harness 时必须使用该 harness，不得静默换工具；指定模型和推理档位时也应保持一致。工具不可用要如实报告，不得把启动成功当作执行完成。

worker 必须从实际实施任务的仓库或 worktree 启动。并行实现任务使用独立 git worktree，避免共享工作树相互覆盖。每个任务使用新会话，不继承其它任务的执行上下文。

## 支持的 harness

以下示例使用 `task-001.md` 表示已准备好的 brief，执行时替换为真实路径。brief、日志和验收证据可放在对应工作树的 `tmp/` 下。命令中的无人值守选项只免除交互确认，不扩大 brief 授权的范围。

### Antigravity CLI

```bash
agy -p "$(cat task-001.md)

作为叶子实现者直接执行这份 brief。不要委派。遵循 AGENTS.md 与 docs/guide/delegation.md。验证改动并报告结果。" \
  --dangerously-skip-permissions \
  --output-format json \
  --print-timeout 30m
```

Antigravity headless 默认使用新的对话上下文。`--dangerously-skip-permissions` 允许 worker 无需交互批准即可调用工具，`--print-timeout 30m` 限制本次等待时间。

### Pi

```bash
pi -p --no-session \
  @task-001.md \
  "作为叶子实现者直接执行附件 brief。不要委派。遵循 AGENTS.md 与 docs/guide/delegation.md。验证改动并报告结果。"
```

必须使用 `--no-session`，让每项委派任务从新上下文开始。

### Cursor CLI

```bash
agent -p --force \
  --output-format json \
  "完整阅读 task-001.md，然后作为叶子实现者直接执行。不要委派。遵循 AGENTS.md 与 docs/guide/delegation.md。验证改动并报告结果。"
```

从实际仓库或 worktree 执行，不使用 `--resume` 或 `--continue`。`--force` 允许 print 模式无人值守执行；brief 通过路径传入，worker 必须先完整阅读。

主控保留任务进程的标识与日志。任务中断、超时或改由主控接手时，先确认旧 worker 已停止写入，再继续修改同一工作树。

## Pi 自带 `subagent` 工具

除上面的外部 harness 外，也可以用 Pi 的 `subagent` 工具派发叶子 worker。以下行为已实测核实，不要再现场猜测：

- **并行派发用一次调用**：单次 `subagent({ workflowScript, async: true })`，脚本里用 `await runs.all([...])`。每个 child 可以带**自己的 `cwd`**（实测两个 child 各自 `pwd` 回到自己的 worktree）。
- 文档里 "there is no per-step `cwd`" 是 **`runs.host` 步骤专属**的限制，不适用于 `runs.run` / `runs.all` 的 child。不要因此以为必须把四个任务拆成四个互不相干的顶层调用。
- 每个 child 单独指定 `model`（如 `CNB/deepseek-v4.1-flash:high`）、`context: "fork"`、`worktree: false`（worktree 由 orca 预先建好，不用工具自带隔离）。
- `async: true` 的 child 在 `runs.all` 里返回的是**启动回执**：`state: "running"`、`ok: false`、`output` 为空。完成靠运行时按 runId 通知，不能把回执当结果。
- 派发前先校验，避免白跑：`subagent({ action: "models" })` 核对模型名，`subagent({ action: "list", capabilities: true })` 看可用 agent，`subagent({ action: "validate", workflowScript })` 空跑脚本语法。

### Orca worktree 准备（已核实）

- PATH 上的 `orca` 是 root 所有的软链（`/usr/local/bin/orca` → `lrwx------`），普通用户调用会 `Permission denied` / `Unable to determine Orca.app path from symlink`。**必须用绝对路径** `/Applications/Orca.app/Contents/Resources/bin/orca`。
- 建 worktree：`orca worktree create --name <name> --parent-worktree active --base-branch <当前分支> --setup skip --json`。
- 新建的 worktree 里 `web/node_modules` 不存在，不要重装；从主工作树软链后自检：

```bash
ln -s <主工作树>/web/node_modules <新 worktree>/web/node_modules
(cd <新 worktree>/web && npx tsc --noEmit -p tsconfig.json)
```

## 执行 brief

通常使用以下结构；标题与正文用中文，括号中的键对应原规范的概念：

```markdown
# 任务（Task）
简洁标题。

## 目标（Objective）
期望达到的可观察状态。

## 背景（Context）
仓库事实、实现位置、必要的前置条件。

## 交付物（Deliverables）
具体变更与验收标准。

## 约束（Constraints）
允许修改什么；不要修改哪些文件、不要尝试哪些方向。
兼容性边界、尝试次数上限、退出及回退条件。

## 验证（Validation）
必须执行的命令、成功判据与证据保存位置。

## 回报（Report back）
变更摘要、实际 diff 范围、验证结果、未解决问题与重要偏差。
```

优先写可观察的结果与验收标准；只有设计已有明确意图时才规定实现细节。仓库现实优先于 brief 中过时的假设：只作维持目标所必需的最小调整，不扩大范围，并报告重要偏差。达到尝试上限或退出条件时，报告阻碍与已有证据，不自行改成另一个任务。

## 验收与本项目约束

- 主控亲自检查新增、修改、删除文件及完整 diff，确认符合任务范围；再运行相关测试，核实 worker 的报告。不能只引用 worker 的“已完成”。
- 对外产品入口只有 `bdt`。新增能力必须作为其子命令或参数，不恢复 `babeldoc.tools.agent` 内部 CLI、`experiments/*_translate.py` 翻译脚本入口或第二个 `babeldoc_tools` 包；守卫在 `tests/test_single_entry.py`。
- 翻译测试结果保存在当前仓库的 `tmp/`，该目录必须被 `.gitignore` 忽略。使用仓库 `.venv/bin/python -m pytest`；完整回归把 `.venv/bin` 加入 `PATH`，供测试调用 `bdt`。
- 每次 pytest 使用新目录 `--basetemp="tmp/pytest-$(date +%Y%m%d-%H%M%S)"`，避免清空上一轮证据。改动的 Python 文件运行 `.venv/bin/ruff check <files>`；提交前运行 `git diff --check`。

完整验证命令与交付口径见 [运行与验证](cli.md)。上述 harness 命令属于开发委派工具，不构成项目新的产品入口。
