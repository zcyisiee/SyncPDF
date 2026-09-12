// LaTeX bbox 两端对齐升级：单阶段执行 workflow（P0–P5 各启动一次，由父代理串联）
// 依据 .plan/latex-justify/PLAN.md（subagent-bubbly-scroll）
// 阶段内：实现 → fresh 审查 →（BLOCK 时）修复 → 复审，最多 2 个修复轮；仍 BLOCK 带证据终止。
// 阶段间：父代理主验收（diff + Validation 命令 + 目视）→ 中文 commit → 用同一 missionId 启动下一阶段。
// 子代理统一模型：deepseek/deepseek-flash。
//
// 用法：启动前把 PHASE 改成本阶段条目，父代理调用
//   subagent({ workflowScriptPath: <本文件>, async: true, missionId: <mission> })

const PHASE = { id: "P0-diagnostics", label: "P0 诊断与验收工具" };
// 依次可用：P0-diagnostics / P1-gating / P2-fusion / P3-typography / P4-batching / P5-regression

const REPO = "/Users/zhengcaiyi/Desktop/博0/杂项/Github小玩意/BabelDOC/ieeTranslater";
const BRIEFS = REPO + "/.plan/latex-justify/briefs";
const MODEL = "deepseek/deepseek-flash";

function briefPath(id) {
  return BRIEFS + "/" + id + ".md";
}

// worker 任务：执行 brief，不委派，不 commit
function workerTask(briefId) {
  return [
    "先完整通读并执行 brief：" + briefPath(briefId) + "（该文件是唯一权威任务说明，逐条 Deliverables 落实）。",
    "",
    "Execute the attached brief directly as a leaf implementation worker. Do not delegate. Follow AGENTS.md. Validate your changes and report the result.",
    "",
    "硬约束：",
    "- 仓库：" + REPO + "（分支 feature/latex-bbox-layout），只在此仓库内改动；",
    "- 不要 git commit / push（主代理验收后提交）；不要启动新的子代理；",
    "- 单测基线：pytest 既有失败数不得增加（当前 7 个 fixture 环境失败）；",
    "- 仓库现实优先于 brief 里的 stale 假设，最小适配并报告偏差；",
    "- 报告按 brief 的 Report back 结构输出。"
  ].join("\n");
}

// 审查任务：对照 brief 静态验收（reviewer 无 bash，验证命令由父代理主验收执行）
function reviewTask(briefId, implReport) {
  return [
    "你是验收审查员（fresh context）。审查 " + REPO + "（分支 feature/latex-bbox-layout）当前未提交的工作区改动（git diff 即本阶段全部改动，前序阶段已由主代理提交），对照 brief：" + briefPath(briefId) + "。",
    "",
    "实现者的完成报告（供核对，不可尽信）：",
    "---",
    String(implReport).slice(0, 10000),
    "---",
    "",
    "步骤：",
    "1. 通读 brief 全文（Objective/Deliverables/Constraints/Validation）；",
    "2. 读 git diff 与新文件，逐条核对 Deliverables 是否落实、Constraints 是否守住（尤其：默认关闭路径零行为变化、不碰既有 7 个失败测试、不引入新依赖、不碰 /tmp 既有 workdir）；",
    "3. 对照实现者报告抽查关键实现正确性（逻辑与边界，不是风格）；",
    "4. 对报告里声称的验证结果，检查可静态复核的证据（测试代码是否真的断言了声称的行为、命令是否与 brief 一致）。",
    "",
    "不要修改任何文件。只输出报告：",
    "- 结论：PASS / FAIL / PASS-with-notes；",
    "- Deliverables 对照表（逐条 落实/部分/缺失 + 证据文件:行号）；",
    "- 问题按 P0/P1/P2 分级，附证据（P0/P1 会导致 BLOCK）；",
    "- 对父代理主验收的提示（建议父代理重点运行/目视的项）。",
    "",
    "结尾必须给出 Merge verdict: OK / OK with notes / BLOCK。"
  ].join("\n");
}

// 修复轮任务：只修指定审查发现
function fixTask(briefId, round, reviewOutput) {
  return [
    "修复轮 " + round + "。仓库：" + REPO + "（分支 feature/latex-bbox-layout，未提交工作区，只含本阶段改动）。",
    "brief：" + briefPath(briefId) + "。",
    "",
    "上一轮审查发现的问题（只修这些问题，不要扩大范围）：",
    "---",
    String(reviewOutput).slice(0, 12000),
    "---",
    "",
    "要求：逐条修复 P0/P1（P2 酌情）；修完重跑受影响的验证命令；不 commit、不委派；",
    "报告：每个问题的修复方式 + 验证输出摘要。"
  ].join("\n");
}

// ---- 单阶段执行 ----
const board = [];

const impl = await runs.run("latex-" + PHASE.id + "-impl", {
  agent: "worker",
  label: "实现 " + PHASE.label,
  model: MODEL,
  task: workerTask(PHASE.id),
  control: { needsAttentionAfterMs: 1500000 }
});
board.push({ key: "impl", phase: PHASE.id, ok: impl.ok, runId: impl.runId });

let outcome = "impl-failed";
let lastReview = null;

if (impl.ok) {
  outcome = "unreviewed";
  for (let round = 1; round <= 3; round++) {
    const review = await runs.run("latex-" + PHASE.id + "-review-r" + round, {
      agent: "reviewer",
      label: "审查 " + PHASE.label + "（第" + round + "轮）",
      model: MODEL,
      task: reviewTask(PHASE.id, impl.output)
    });
    lastReview = review;
    if (!review.ok) {
      outcome = "review-error";
      break;
    }
    const verdict = String(review.output || "").includes("Merge verdict: BLOCK") ? "BLOCK" : "OK";
    if (verdict === "OK") {
      outcome = "reviewed-ok";
      break;
    }
    if (round === 3) {
      outcome = "review-blocked-3rounds";
      break;
    }
    const fix = await runs.run("latex-" + PHASE.id + "-fix-r" + round, {
      agent: "worker",
      label: "修复 " + PHASE.label + "（第" + round + "轮）",
      model: MODEL,
      task: fixTask(PHASE.id, round, review.output)
    });
    board.push({ key: "fix-r" + round, phase: PHASE.id, ok: fix.ok, runId: fix.runId });
    if (!fix.ok) {
      outcome = "fix-failed";
      break;
    }
  }
}

return {
  phase: PHASE.id,
  outcome,
  board,
  implReport: impl.ok ? String(impl.output || "").slice(0, 6000) : null,
  finalReview: lastReview && lastReview.ok ? String(lastReview.output || "").slice(0, 6000) : null,
  note: "outcome=reviewed-ok 时，父代理执行主验收（Validation 命令 + gates + 目视）并按计划中文 commit，然后把 PHASE 改为下一阶段、以同一 missionId 重新启动本 workflow。"
};
