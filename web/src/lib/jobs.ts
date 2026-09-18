/**
 * job 的纯函数口径（W08）：活动判据、轮询间隔、`from` 自动判断、页码形状。
 *
 * 服务端契约在 `docs/reference/http-api.md`：`action=run` 的 `from` 取值是 7 个阶段，
 * `pages` 形如 `"1-3,5"`；客户端**永远**不传命令/密钥（profile 只传 id）。
 */
import type { DocumentDetail, JobRecord } from '../api/types';
import { STAGE_NAMES, type StageName } from './humanize';

/**
 * 有 `queued`/`running` job 时的兜底轮询间隔：W14 从 2s 放宽到 5s。
 *
 * 快路径是 SSE 的 `job_update`（api.md §1.4.1）：收到就立刻 invalidate（= 立刻一次
 * refetch，并把 TanStack 的间隔计时重置），所以“推送之后的 5s 内不会再轮询”；
 * 这个 5s 只用于两种地方：job 还没建出 run 归档（那时 SSE 是 404，没有快路径）
 * 与 `job_update` 丢帧/断线。
 */
export const JOBS_ACTIVE_REFETCH_MS = 5_000;
/** 没有活动 job 时的轮询间隔：慢速兜底（别让页面永远不更新）。 */
export const JOBS_IDLE_REFETCH_MS = 30_000;
/**
 * 收到 `job_update` 之后的静默窗口：`true` = 这段时间内跳过兜底轮询。
 *
 * 与 `JOBS_ACTIVE_REFETCH_MS` 同值不是巧合：推送走 `invalidateQueries` → 立刻 refetch，
 * 静默窗口跳过的是“推送后紧接着那一次冗余轮询”；窗口一过就按 `jobsRefetchInterval` 继续
 * 兜底。注意：这个窗口**只**由 push 开路，而且到点由 `useJobUpdates` 的定时器清掉状态
 * （靠重渲染让 TanStack 重启计时器）—— 没有那个定时器，间隔会停在 `false` 再不恢复。
 */
export const JOBS_UPDATE_QUIET_MS = 5_000;

/** 活动 job 尚未产出 run 归档时，事件尾部窗口的重试间隔（拿到 run 就转 SSE，不再轮询）。 */
export const EVENTS_TAIL_DISCOVERY_REFETCH_MS = 5_000;

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

/** 是否该按兜底间隔轮询（有活动 job 5s，否则 30s）。 */
export function jobsRefetchInterval(jobs: readonly JobRecord[] | undefined): number {
  return activeJob(jobs) ? JOBS_ACTIVE_REFETCH_MS : JOBS_IDLE_REFETCH_MS;
}

/**
 * 静默窗口判据（纯函数）：`pushedAtMs` 之后的 `JOBS_UPDATE_QUIET_MS` 内为真。
 * 没收到过推送（`null`）→ 假（没有快路径可言，靠兜底轮询）。
 */
export function withinJobUpdateQuietWindow(
  pushedAtMs: number | null,
  nowMs: number = Date.now(),
): boolean {
  return pushedAtMs !== null && nowMs - pushedAtMs < JOBS_UPDATE_QUIET_MS;
}

/** `job_update` 的 `action` 与 JobRecord 的同一枚举（SSE 里是字符串）。 */
export type JobUpdateAction = JobRecord['action'];

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
  // A successful job can remain at index 0 briefly while the newly accepted job
  // is appended by the server. The active job is the current source of truth.
  if (activeJob(jobs) !== null) return 'active';
  const latest = jobs?.[0];
  if (!latest) return 'start';
  if (latest.status === 'succeeded') return 'start';
  return 'failed';
}

/** 终态集合（收到这些 `status` 的 `job_update` 就是“这次任务结束了”）。 */
const TERMINAL_JOB_STATUSES = new Set(['succeeded', 'failed', 'canceled', 'interrupted']);

/**
 * `job_update` → 该失效的查询 key 前缀（纯函数，api.md §1.4.1 的前端分流）。
 *
 * 分四档（brief 冻结）：
 *
 * - 总是：`jobs`（job 列表就是这次通知的主题）；
 * - `action=run`：文档/阶段/段落/产物/事件窗口/文档列表（翻译会换掉段落与 PDF，
 *   新 run 归档要靠事件窗口首拉才能被发现）；
 * - `action=compile`：详情 + 版本归档（编译只动发布与 `compile.*`）；
 * - `action=retranslate`：候选（生成只动候选行；`['documents',did,'paragraphs']` 前缀
 *   同时盖住段落列表与所有候选列表）；
 * - **终态再补一刀**：`artifacts` + `document`（+ compile 的 versions）—— 预览、产物清单、
 *   段落面板都在这一刀里换成新产物，不需要用户手动刷新。
 *
 * 返回的是**已限定到 did 的 key 前缀**（调用方直接交给 `invalidateQueries`）。
 */
export function jobUpdateInvalidations(
  did: string,
  update: { action: string; status: string },
): readonly (readonly unknown[])[] {
  const keys: unknown[][] = [['documents', did, 'jobs']];
  switch (update.action) {
    case 'compile':
      keys.push(['documents', did], ['documents', did, 'versions']);
      break;
    case 'retranslate':
      keys.push(['documents', did, 'paragraphs']);
      break;
    case 'run':
    default:
      keys.push(
        ['documents', did],
        ['documents', did, 'stage-state'],
        ['documents', did, 'paragraphs'],
        // 事件窗口（尾部首拉）也要重拉：新 run 归档只能靠它被发现（SSE 订阅的是首拉
        // 拿到的 run_id），否则“翻译中”的 run 事件永远进不了面板。
        ['documents', did, 'events'],
        ['documents'],
      );
  }
  if (TERMINAL_JOB_STATUSES.has(update.status)) {
    keys.push(['documents', did, 'artifacts']);
    if (update.action === 'compile') keys.push(['documents', did, 'versions']);
  }
  return keys;
}
