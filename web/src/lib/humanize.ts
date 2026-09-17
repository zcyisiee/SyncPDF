/**
 * 文案口径（最小版）：7 个阶段名 + 状态徽标文案。
 * 事件 kind → 人话的映射表在 W06 建 `src/events/humanize.ts`，本模块不预置。
 */
export const STAGE_NAMES = [
  'parse',
  'translate',
  'apply',
  'build',
  'check',
  'review',
  'report',
] as const;

export type StageName = (typeof STAGE_NAMES)[number];

export const STAGE_LABELS: Record<StageName, string> = {
  parse: '解析',
  translate: '翻译',
  apply: '套版',
  build: '编译',
  check: '检查',
  review: '复核',
  report: '报告',
};

export function stageLabel(stage: string): string {
  return STAGE_LABELS[stage as StageName] ?? stage;
}

export type StatusTone = 'pass' | 'run' | 'err' | 'idle';

const RUNNING_STATUSES = new Set(['running', 'waiting', 'queued', 'in_progress']);
const FAILED_STATUSES = new Set(['error', 'failed', 'failure', 'needs_fix']);
const DONE_STATUSES = new Set(['ok', 'done', 'pass', 'passed']);

/** 唯一会触发 pulse 动效的状态（DESIGN.md §4.3：脉冲只出现在真正运行中的元素上）。 */
export function isRunningStatus(status: string): boolean {
  return status === 'running';
}

export function stageTone(status: string): StatusTone {
  if (RUNNING_STATUSES.has(status)) return 'run';
  if (FAILED_STATUSES.has(status)) return 'err';
  if (DONE_STATUSES.has(status)) return 'pass';
  return 'idle';
}

export function stageStatusLabel(status: string): string {
  if (status === 'not_run') return '未运行';
  switch (stageTone(status)) {
    case 'run':
      return '进行中';
    case 'err':
      return '失败';
    case 'pass':
      return '已完成';
    default:
      return status;
  }
}

export interface DocumentStatus {
  label: string;
  tone: StatusTone;
  /** true 只代表 run_state 里真有 `running` 阶段（waiting 不算运行中）。 */
  running: boolean;
}

/** 文档整体状态：取 7 个阶段里最"响"的一个（运行中 > 失败 > 未完成 > 已完成 > 未运行）。 */
export function documentStatus(stageSummary: Record<string, string> | undefined): DocumentStatus {
  const statuses = STAGE_NAMES.map((stage) => stageSummary?.[stage] ?? 'not_run');
  const running = statuses.some(isRunningStatus);
  if (statuses.some((status) => RUNNING_STATUSES.has(status))) {
    return { label: '进行中', tone: 'run', running };
  }
  if (statuses.some((status) => FAILED_STATUSES.has(status))) {
    return { label: '失败', tone: 'err', running: false };
  }
  if (statuses.every((status) => DONE_STATUSES.has(status))) {
    return { label: '已完成', tone: 'pass', running: false };
  }
  if (statuses.every((status) => status === 'not_run')) {
    return { label: '未运行', tone: 'idle', running: false };
  }
  return { label: '未完成', tone: 'idle', running: false };
}

/** `updated_at`（UTC ISO8601）→ 中文相对时间；拿不到时间戳显示 `—`。 */
export function humanizeUpdatedAt(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return '—';
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return '—';
  const minutes = Math.floor((now.getTime() - at.getTime()) / 60_000);
  if (minutes < 1) return '刚刚';
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days} 天前`;
  return at.toISOString().slice(0, 10);
}

/** 产物缺失时计数是 `null`（不是 0），显示 `—` 而不是编造 0（docs/frontend/api.md §3.1）。 */
export function countLabel(value: number | null | undefined, unit: string): string {
  return typeof value === 'number' ? `${value} ${unit}` : '—';
}

export function progressLabel(
  translated: number | null | undefined,
  total: number | null | undefined,
): string {
  return `${translated ?? '—'}/${total ?? '—'}`;
}
