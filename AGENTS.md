## Delegating implementation tasks

Use a delegated coding harness as a one-shot implementation worker for tasks that can be isolated behind a clear execution brief.

### Task Delegation Principles
为了充分发挥 Subagent 的能力并防止执行失控，任务分配必须严格遵循以下核心原则：
1. **任务极简拆解 (Small & Clear Tasks)**：避免分配宏大目标，务必将任务拆解为小而清晰、可独立验证的最小执行单元。
2. **设定严格边界 (Strict Boundaries to Prevent Infinite Loops)**：Subagent 工程化能力极强，但也极易陷入过度探索或死循环（Rabbit holes）。必须在 Brief 中明确其工作边界、修改范围、尝试次数上限和明确的退出/回退条件。
3. **主控严格验收 (Strict Orchestrator Acceptance)**：Orchestrator（你）必须对 Subagent 的交付结果进行代码级审查和测试验证，绝对不要盲信其“任务已完成”的口头报告。

**Only the top-level orchestrator delegates.** Delegated workers are leaf workers: execute the assigned brief directly and do not spawn additional agents unless explicitly requested.

When the user specifies a subagent harness, use that harness. Do not silently substitute another harness.

Run workers from the repository or worktree where the task should be implemented. For parallel tasks, use separate git worktrees.

### Supported harnesses

#### Antigravity CLI

```bash id="3oz8yr"
agy -p "$(cat <task-id>.md)

Execute this brief directly as a leaf implementation worker. Do not delegate. Follow AGENTS.md. Validate your changes and report the result." \
  --dangerously-skip-permissions \
  --output-format json \
  --print-timeout 30m
```

Antigravity headless runs start with fresh conversation context by default. `--dangerously-skip-permissions` gives the worker permission to execute tools without interactive approval.

#### Pi

```bash id="xjrz91"
pi -p --no-session \
  @<task-id>.md \
  "Execute the attached brief directly as a leaf implementation worker. Do not delegate. Follow AGENTS.md. Validate your changes and report the result."
```

Use `--no-session` so each delegated task starts with fresh conversation context.

#### Cursor CLI

```bash
agent -p --force \
  --output-format json \
  "Read <task-id>.md in full, then execute it directly as a leaf implementation worker. Do not delegate. Follow AGENTS.md. Validate your changes and report the result."
```

Run from the repository or worktree where the task should be implemented. Do not use `--resume` or `--continue` for delegated tasks; each worker should start with fresh conversation context.

`--force` enables unattended implementation in print mode. The task brief is passed by path and should be read in full before implementation.


### Execution briefs

A brief should normally contain:

* `# Task` — concise title
* `## Objective` — desired end state
* `## Context` — relevant repository facts
* `## Deliverables` — concrete required changes
* `## Constraints` — scope and compatibility boundaries (必须明确指出：不要修改哪些文件、不要尝试哪些方向，防止过度发散)
* `## Validation` — commands or checks that must pass
* `## Report back` — required completion summary

Prefer observable outcomes and acceptance criteria over prescribing implementation details unless the design is intentional.

Repository reality takes precedence over stale assumptions in a brief. Adapt minimally when necessary, preserve the intended outcome, do not broaden scope, and report material deviations.

### Acceptance

A worker report or successful process exit is not proof of correctness. The orchestrator must inspect the resulting diff and relevant validation before accepting the task. **永远不要省略主控亲自查阅 Diff 和验证测试（如跑通 pytest）的环节。**

## 本项目的要求
运行翻译测试的结果需要保留在当前路径的tmp/文件夹下。tmp 文件夹应该加入.gitignore

对外接口只有 `bdt`：新增能力必须作为 `bdt` 的子命令或其参数暴露，不得新建并行入口
（不再有 `babeldoc.tools.agent` 内部 CLI、`experiments/*_translate.py` 脚本入口或第二个
`babeldoc_tools` 包）。守卫测试 `tests/test_single_entry.py` 会拦截这类回归。

## 本地验证

- 使用仓库 `.venv/bin/python -m pytest` 运行测试，完整回归需把 `.venv/bin` 加入 `PATH`，供测试调用唯一入口 `bdt`。
- 测试使用 `--basetemp="tmp/pytest-$(date +%Y%m%d-%H%M%S)"` 将产物留在本仓库；每次采用新目录，避免 pytest 清空上一轮证据。
- 对改动文件运行 `.venv/bin/ruff check <files>`，提交前运行 `git diff --check`。
