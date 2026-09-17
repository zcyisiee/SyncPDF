/**
 * job 的纯函数口径（W08）：活动判据、轮询间隔、`from` 自动判断、页码形状。
 *
 * 服务端契约在 `docs/frontend/api.md` §3.4：`action=run` 的 `from` 取值是 7 个阶段，
 * `pages` 形如 `"1-3,5"`；客户端**永远**不传命令/密钥（profile 只传 id）。
 */
import type { DocumentDetail, JobRecord } from '../api/types';
import { STAGE_NAMES, type StageName } from './humanize';

/** 有 `queued`/`running` job 时的轮询间隔（run 归档还没出现的过渡信号）。 */
export const JOBS_ACTIVE_REFETCH_MS = 2_000;
/** 没有活动 job 时的轮询间隔：慢速兜底（别让页面永远不更新）。 */
export const JOBS_IDLE_REFETCH_MS = 30_000;

/** `pages` 形状（与 `serve/schemas.py::JOB_PAGES_PATTERN` 一致，服务端还会再校验）。 */
export const JOB_PAGES_PATTERN = /^[0-9]+(-[0-9]+)?(,[0-9]+(-[0-9]+)?)*$/;

/** `from` 可选项 = 7 个阶段（顺序与时间线一致）。 */
export const RUN_STAGES: readonly StageName[] = STAGE_NAMES;

const ACTIVE_JOB_STATUSES = new Set(['queued', 'running']);

/** 活动 job（`queued`/`running`）；同一文档最多一个。 */
export function activeJob(jobs: readonly JobRecord[] | undefined): JobRecord | null {
  if (!jobs) return null;
  return jobs.find((job) => ACTIVE_JOB_STATUSES.has(job.status)) ?? null;
}

/** 是否该按 2s 轮询（有活动 job 才快轮询，否则 30s 兜底）。 */
export function jobsRefetchInterval(jobs: readonly JobRecord[] | undefined): number {
  return activeJob(jobs) ? JOBS_ACTIVE_REFETCH_MS : JOBS_IDLE_REFETCH_MS;
}

/**
 * `from` 自动判断：有 parse 产物 → `translate`（parse 的结果就在 workdir 里，没必要重跑
 * 一次 MinerU 网络调用）；否则 → `parse`（新上传的文档只有 source.pdf）。
 *
 * 判据：`stage_summary.parse === 'ok'`（run_state 记的），或 W02 的 available 里
 * `anchors`/`parse_snapshot` 至少一个可用（旧 workdir 可能没有 run_state）。
 */
export function defaultFromStage(doc: DocumentDetail | undefined): StageName {
  if (!doc) return 'parse';
  if (doc.stage_summary.parse === 'ok') return 'translate';
  if (doc.available.anchors || doc.available.parse_snapshot) return 'translate';
  return 'parse';
}

/** 页码输入是否合法（空串 = 全部页，合法）。 */
export function isValidPagesSpec(value: string): boolean {
  const trimmed = value.trim();
  return trimmed === '' || JOB_PAGES_PATTERN.test(trimmed);
}

/** 页码输入 → 请求体里的 `pages`（空 → 不带该字段）。 */
export function normalizePagesSpec(value: string): string | null {
  const trimmed = value.trim();
  return trimmed === '' ? null : trimmed;
}

/** 终态 job 的失败/取消文案（成功 → `null`）。 */
export function jobOutcomeMessage(job: JobRecord): string | null {
  if (job.status === 'failed') {
    return [job.error_code, job.error_message].filter(Boolean).join('：') || '任务失败';
  }
  if (job.status === 'canceled') return '已取消（不再自动重跑）';
  if (job.status === 'interrupted') {
    return job.interrupted_reason === 'server_restart'
      ? '服务曾重启，这个任务没有继续跑，请重试'
      : (job.error_message ?? '任务被中断，请重试');
  }
  return null;
}

/** 工作台该显示哪张卡：活动 job → `active`；最近一次失败/取消/中断 → `failed`；否则 `start`。 */
export function jobCardMode(jobs: readonly JobRecord[] | undefined): 'active' | 'failed' | 'start' {
  const latest = jobs?.[0];
  if (!latest) return 'start';
  if (ACTIVE_JOB_STATUSES.has(latest.status)) return 'active';
  if (latest.status === 'succeeded') return 'start';
  return 'failed';
}
