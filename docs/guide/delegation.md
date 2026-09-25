# 子代理委派规范

本文保存原 `AGENTS.md` 的完整委派约束。主控在委派前阅读；叶子执行者在开始任务前阅读。适合独立封装的实现任务，使用一次性的 coding harness worker 执行；用户要求主控直接完成时遵从用户要求。

## 分工与边界

1. **任务小而清晰**：把目标拆成可独立验证的最小执行单元，避免把宏大目标直接交给一个子代理。
2. **约束必须具体**：brief 写明可修改范围、禁止修改的文件、禁止尝试的方向、尝试次数上限和退出/回退条件，防止过度探索或无限循环。
3. **主控亲自验收**：worker 的报告或成功退出码都不是正确性证明。主控必须亲自审阅 diff，并运行相关测试验证，不能跳过。

### 何时委派与 brief 取舍

- **小修主控直接做**：几行到几十行、根因已清楚的改动（删冗余分支、补一个测试、改文案或文档），由主控直接改、测、提交。只有需要定位根因、跨多层或多个文件、或验证链较长的任务，才派子代理。
- **验收标准必须能被正确实现满足**：写之前先确认"正确实现"能过。涉及译文语义的验收（链接、原子、术语定位）用真实译文或缓存重放，不用 `fake:cjk`：它的随机汉字不含类别词等语义，会逼子代理加宽松规则迁就假数据。
- **必读材料要精简**：brief 只指向 task state 中与本任务相关的小节和必要的报告章节，不要求通读全部历史。
- **单个任务要窄**：一个 brief 只包含一个修复及其测试，外加一篇最能证明问题的样本实跑。多篇回归、全文重跑、跨文档统计由主控合入后统一执行。
- **避免冷编译**：新 worktree 开工前，先用 APFS 写时复制克隆主树编译目录作为私有 target（`cp -cR <主树 target> <worktree>/engine/target-wt`），复用第三方依赖的编译产物，只重编本仓库的 crate。

只有顶层主控分派任务。被委派者是叶子，直接执行 brief；未经明确要求不得继续创建或委派子代理。

用户指定 harness 时必须使用该 harness，不得静默换工具；指定模型和推理档位时也应保持一致。工具不可用要如实报告，不得把启动成功当作执行完成。

worker 必须从实际实施任务的仓库或 worktree 启动。并行实现任务使用独立 git worktree，避免共享工作树相互覆盖。每个任务使用新会话，不继承其它任务的执行上下文。

## Task state 是必读的只读输入

- 主控委派前先更新并读取该任务唯一的 `task-state.md`；具体维护规则见 [任务状态生命周期](../index.md#task-state-lifecycle)。
- 每份 brief 必须给出该文件的**绝对路径**，以及本轮允许范围、验收和停止条件。叶子开工先读状态，再读专项计划；旧 worktree 没有最新文件时，跨树只读主控指定的 canonical 文件，不自建副本。
- 只有用户和主 Agent 可修改 task state；writer 的源码修改权限不包含此文件。发现偏好冲突、新阻断或事实变化，通过 supervisor/交付报告提出，由主控核实更新。
- compact/接管后的主控先读状态并核对实际 run、dirty 和验收证据；不得把旧“正在运行”记录当作当前进程事实。任务收尾的经验升格仍由主控负责。

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

### Devin CLI（SWE-2 High）

本机命令为 `devin`，不是 `devin-cli`。先用 `devin --version`、`devin models list` 核实安装和模型；显式指定 `swe-2-high`，不要依赖默认模型。官方[命令参考](https://docs.devin.ai/cli/reference/commands)与本机帮助已核对。

```bash
devin --model swe-2-high \
  --prompt-file task-001.md -p \
  --permission-mode dangerous \
  --respect-workspace-trust false
```

- 从任务实际 worktree 启动，每个任务新建会话，**不传 `-c/--continue`、`-r/--resume`，不 fork 或附带主控对话历史**。SWE-2 上下文约256K；brief应精简自足，只给必要代码/证据路径、明确目标、可写范围、验证及停止条件，按需读取而非倾倒日志。
- `-p` 是一次性非交互模式；`--prompt-file`从文件读取任务。`dangerous`免除工具交互批准，`--respect-workspace-trust false`跳过非交互无法展示的工作树信任对话；二者仅用于已授权的隔离开发任务，不扩大brief权限，不允许额外写主树/全局配置或嵌套委派。
- 本项目可通过[外部runner配置](../../.pi/agents/devin-swe2.md)用Pi `subagent`管理Devin进程、超时、日志与完成通知。模型固定在runner argv，不传Pi原生的`model/context/toolBudget/acceptance`等不受支持选项。project agent按child实际cwd发现，故新worktree必须包含同一`.pi/agents/devin-swe2.md`（未提交的主树配置不会自动出现）。先做无工具启动探针；探针成功不是工程验收，仍需主控审diff并独立测试。
- 外部runner不支持Pi原生steer/resume。brief必须写明自行收束的时间和修正轮数，外层另设硬超时。中断后先确认进程已退出并保存差异，再以新会话作有边界的后续任务；不得静默换模型/harness。

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

### 外部 runner（`runner.type: external-cli`）注意项（已实测核实）

用 Pi 的 `subagent` 托管外部 harness（`devin-swe2`、`paddle-progress-agy`、内置 `codex-exec` 等）时，下列行为与原生 Pi child **不同**，不要按原生经验处理。证据：run `1eea810b`（真实只读任务）、`357ab403`（默认配置探针）、`7ed2f077`（同任务 + `control` 覆盖）；观察版本 `pi-subagents@0.71.0`。

- **只能异步启动**：外部 runner 传 `async: false` 会被直接拒绝。用默认 `async: true` 拿到 runId，完成靠运行时原生通知；`subagent({ action: "status", id })` 只作一次性查看，不要 sleep 轮询。
- **能力边界固定**，每个 run 的 `meta.json` 里 `runner.capabilities` 可直接查：`stop=true`，而 `steer` / `resume` / `structuredOutput` / `toolEvents` / `supervisor` / `forkContext` / `extensionBindings` 全为 `false`（`nonResumableReason`：one-shot stdin adapter 没有可持久的外部会话身份）。因此 attention 通知里给的 `steer` 与 routed `resume` nudge 对外部 runner **无效**，发了也不会送达——唯一可用的实时控制是 `interrupt`/`stop`。中断后先确认进程已退出并保存 diff，再以**新会话**做有边界的后续任务（不 resume）。
- **60 秒 `needs_attention` 是噪声，不是停工信号**：外部 runner 没有原生 tool/turn 事件，Pi 能观测到的唯一活动是子进程真正写出的 stdout/stderr 字节。devin 的 ACP 握手加首个模型响应天然静默超过 60 秒，所以**只要首次输出晚于 60 秒就必然在 60 秒处报一次** `needs attention (no observed activity for 60s)`。实测 run `357ab403`：第 60 秒时 `external-0.stdout.log` 为 **0 字节**、devin 进程存活、任务最终 `exitCode 0` 成功，全程仅此一条控制事件。成因是监控统计 `output-<n>.log` 的 mtime（`runs/background/subagent-runner.js` 的 `stepOutputActivityAt`），而外部 runner 的实时输出写在 `external-<n>.stdout.log`（`runs/shared/external-cli-runner.js`），前者要到进程退出后才生成。
  - 处理口径：先 `status`，再看 `external-<n>.stdout.log` 是否在增长、进程是否还在，确认没卡就不要打断。
  - 需要静默时，在**调用参数**里放宽窗口（已 A/B 实测：默认配置 1 条事件，覆盖后 0 条）：

    ```js
    subagent({ agent: "devin-swe2", task: "...", control: { needsAttentionAfterMs: 600000 } })
    ```

    `control` 只存在于工具调用参数：agent frontmatter 没有该字段，`subagents.agentOverrides.<name>` 也不接受它，所以无法做成 agent 级默认，只能逐次传参或改 pi-subagents 本体（改动会影响所有会话，且被包升级覆盖，须 owner 批准）。
- **`structuredOutput` 不可用**：adapter 把 stdout 当**不可信文本**，不产出 Pi 结构化结果，也不产生原生 tool 事件。外部 agent 把 JSON（例如自带的 acceptance-report 围栏）混在自然语言里时 Pi 不会替你解析；要结构化就在 workflow 里自行解析 `output`，或配 typed gate。子进程往 stdout 吐的原生日志同样会污染结果。
- **不要传 Pi 原生 child 选项**：`model` / `context` / `toolBudget` / `acceptance` / `fast` / `forkContext` / `skills` 对通用 external-cli 不生效。模型与档位必须钉在 runner argv 里（如 `.pi/agents/devin-swe2.md`），不要指望在调用处覆盖。
- **不能当失败 lane 的隐式回退**：原生 `subagent` 失败后改走外部/前台/CLI runner 需要 owner 明确批准，并先记录失败 run 与 worktree 状态、确认工作树干净或保存部分 diff；不得静默换 harness。
- **启动前提与探针的边界**：需要本地 CLI 已安装**且已认证**。`subagent({ action: "list", capabilities: true })` 里的 `runner.available` 只是被动 PATH 探测，不证明认证或启动兼容；先做无工具启动探针，但**探针成功不是工程验收**，仍须主控亲审 diff 并独立测试。
- **project agent 按 child 的实际 cwd 发现**，因此新 worktree 必须自带同一份 `.pi/agents/*.md`（未提交的主树配置不会自动出现）。细则见上文 Devin 节。

## 执行 brief

通常使用以下结构；标题与正文用中文，括号中的键对应原规范的概念：

```markdown
# 任务（Task）
简洁标题。

## 必读状态（Task state）
唯一 task-state.md 的绝对路径；叶子只读，不修改、不另建副本。

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
- 翻译测试结果保存在当前仓库的 `tmp/`，该目录必须被 `.gitignore` 忽略。所有 worktree 共用 conda env `bdt`，不再各建 `.venv`；在 worktree 根目录用 `~/miniconda3/envs/bdt/bin/python -m pytest`。测试若调用 `bdt`，需确认 editable 安装目标与本次验证代码树一致，不能把其它 worktree 的运行结果算成本树验证。
- 每次 pytest 使用新目录 `--basetemp="tmp/pytest-$(date +%Y%m%d-%H%M%S)"`，避免清空上一轮证据。改动的 Python 文件运行 `~/miniconda3/envs/bdt/bin/ruff check <files>`；提交前运行 `git diff --check`。

完整验证命令与交付口径见 [运行与验证](cli.md)。上述 harness 命令属于开发委派工具，不构成项目新的产品入口。
