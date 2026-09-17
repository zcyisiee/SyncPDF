/**
 * 时间线纯逻辑（无 React）：`GET /stage-state` 是**基线**（有精确实测 `duration_s`），
 * 事件流只用来给「基线还没有定论」的阶段算本地已进行秒数（§4.7 / §8.3）。
 *
 * 为什么基线优先：事件的末条判据（`isRunLive`）在归档被截断时会误报 live —— 例如
 * `tmp/ccs3764-dyn` 的 `events.jsonl` 是 legacy replay，只覆盖到 `check`，而 `run_state`
 * 里 `review`/`report` 都有真实耗时。所以 `ok`/`failed` 的基线永远赢；事件只能把
 * 「基线说 not_run（或 running/waiting）」的阶段标成 live。
 */
import type { StageStateItem } from '../api/types';
import { isRunLive, type RunEvent } from './events';
import { STAGE_NAMES, stageTone, type StageName, type StatusTone } from './humanize';
import type { WorkbenchView } from './routing';

export type SegmentState = 'ok' | 'err' | 'live' | 'not_run';

/**
 * 阶段 → 点击跳转的视图（映射表写死；没有专属视图的阶段回落到能看清它的视图）：
 * - `parse` → 识别（识别视图就是解析/栏位结果）
 * - `translate` / `apply` → 翻译（套版结果在翻译视图的排版预览里看）
 * - `build` → 进度（编译没有独立视图，编译状态与产物在进度/归档里）
 * - `check` → 检查；`review` → 检查（复核结论属于检查视图）
 * - `report` → 进度（报告产物在进度/归档里看）
 */
export const STAGE_VIEWS: Record<StageName, WorkbenchView> = {
  parse: 'layout',
  translate: 'translate',
  apply: 'translate',
  build: 'progress',
  check: 'check',
  review: 'check',
  report: 'progress',
};

/** 条最小可见宽（%）：`live` 段刚起步的秒数是 0，实测 0.0s 的阶段（report）也是 0。 */
export const MIN_BAR_PERCENT = 2;

export interface TimelineSegment {
  stage: StageName;
  state: SegmentState;
  /** stage-state 报的原始状态串（title / 无障碍文案用，不翻译）。 */
  status: string;
  /** 有实测耗时的阶段（ok/err）的真实秒数；live/not_run 为 null（不编造）。 */
  durationS: number | null;
  /** live 段：从该段 `stage_started` 到现在的本地秒数（1s ticker 重算）。 */
  elapsedS: number | null;
  /** live 段的起算时刻（`stage_started.at`，缺则 stage-state 的 `started_at`）。 */
  at: string | null;
  /** 点击该段跳转的视图（`STAGE_VIEWS`）。 */
  view: WorkbenchView;
}

function toSeconds(value: number | null | undefined): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/** 起止时间（UTC ISO）→ 秒数；缺一头 / 解析不了 → null（不拿 0 冒充耗时）。 */
function durationBetween(startedAt: string | null | undefined, finishedAt: string | null | undefined): number | null {
  if (typeof startedAt !== 'string' || startedAt === '') return null;
  if (typeof finishedAt !== 'string' || finishedAt === '') return null;
  const started = Date.parse(startedAt);
  const finished = Date.parse(finishedAt);
  if (Number.isNaN(started) || Number.isNaN(finished)) return null;
  return Math.max(0, (finished - started) / 1000);
}

/**
 * 事件流口径的「该阶段已开始但没结束」的起点时刻：
 * 最后一次 `stage_started` 的 `seq` 晚于最后一次 `stage_finished`/`stage_error` → 返回它的 `at`。
 * 输入升序事件。配对不成立（没有 started / 已结束）→ null。
 */
export function openStageAt(events: readonly RunEvent[], stage: string): string | null {
  let openSeq = -1;
  let openAt: string | null = null;
  let closedSeq = -1;
  for (const event of events) {
    if (event.stage !== stage) continue;
    if (event.kind === 'stage_started') {
      if (event.seq > openSeq) {
        openSeq = event.seq;
        openAt = event.at === '' ? null : event.at;
      }
      continue;
    }
    if (event.kind === 'stage_finished' || event.kind === 'stage_error') {
      closedSeq = Math.max(closedSeq, event.seq);
    }
  }
  return openSeq > closedSeq ? openAt : null;
}

/** 起点时刻 → 本地已进行秒数（时钟差；时刻解析不了 → null）。 */
export function elapsedSeconds(at: string | null, nowMs: number): number | null {
  if (at === null || at === '') return null;
  const parsed = Date.parse(at);
  if (Number.isNaN(parsed)) return null;
  return Math.max(0, Math.floor((nowMs - parsed) / 1000));
}

export function timelineSegments(
  stages: readonly StageStateItem[] | undefined,
  events: readonly RunEvent[],
  nowMs: number,
): TimelineSegment[] {
  const byStage = new Map((stages ?? []).map((item) => [item.stage, item]));
  // 事件流层面的活跃信号（brief 冻结规则）只是"可能还在跑"，最终仍由基线裁决。
  const runLive = isRunLive(events);
  return STAGE_NAMES.map((stage) => {
    const item = byStage.get(stage);
    const status = item?.status ?? 'not_run';
    const tone = stageTone(status);
    const view = STAGE_VIEWS[stage];
    const duration = toSeconds(item?.duration_s) ?? durationBetween(item?.started_at, item?.finished_at);
    if (tone === 'pass') {
      return { stage, state: 'ok', status, durationS: duration, elapsedS: null, at: item?.started_at ?? null, view };
    }
    if (tone === 'err') {
      return { stage, state: 'err', status, durationS: duration, elapsedS: null, at: item?.started_at ?? null, view };
    }
    const openAt = runLive ? openStageAt(events, stage) : null;
    if (tone === 'run' || openAt !== null) {
      const at = openAt ?? item?.started_at ?? null;
      return {
        stage,
        state: 'live',
        status,
        durationS: null,
        elapsedS: elapsedSeconds(at, nowMs),
        at,
        view,
      };
    }
    return { stage, state: 'not_run', status, durationS: null, elapsedS: null, at: item?.started_at ?? null, view };
  });
}

/** 比例尺分母：最长的**已完成**阶段（live 段的本地秒数不参与分母，否则整条时间线随秒表抖动）。 */
export function maxSegmentDuration(segments: readonly TimelineSegment[]): number {
  return segments.reduce((max, segment) => {
    if (segment.state !== 'ok' && segment.state !== 'err') return max;
    return Math.max(max, segment.durationS ?? 0);
  }, 0);
}

/**
 * 条宽（%，§8.3：宽度 = 该段耗时 / 最长段，同一线性标尺）：`not_run` → null
 * （未开始的段固定 64px 虚线，不参与比例）；0s 与刚起步的 live 段给 2% 最小可见宽。
 */
export function barWidthPercent(segment: TimelineSegment, maxDurationS: number): number | null {
  if (segment.state === 'not_run') return null;
  const seconds = segment.state === 'live' ? segment.elapsedS ?? 0 : segment.durationS ?? 0;
  if (maxDurationS <= 0) return MIN_BAR_PERCENT;
  return Math.min(100, Math.max(MIN_BAR_PERCENT, (seconds / maxDurationS) * 100));
}

/** 总用时 = 已完成阶段实测耗时之和 + live 段已进行秒数；一个都没得测 → null。 */
export function totalDurationS(segments: readonly TimelineSegment[]): number | null {
  let total = 0;
  let measured = false;
  for (const segment of segments) {
    if (segment.state === 'ok' || segment.state === 'err') {
      if (segment.durationS === null) continue;
      total += segment.durationS;
      measured = true;
      continue;
    }
    if (segment.state === 'live' && segment.elapsedS !== null) {
      total += segment.elapsedS;
      measured = true;
    }
  }
  return measured ? total : null;
}

/** 有 live 段 ⇔ 文档正在跑（顶栏/视图栏「翻译中」徽标与自动刷新的唯一判据）。 */
export function hasLiveSegment(segments: readonly TimelineSegment[]): boolean {
  return segments.some((segment) => segment.state === 'live');
}

/** 整条时间线的状态徽标（§8.3 元信息行；口径与 `documentStatus` 一致，但判据是段状态）。 */
export function timelineStatus(segments: readonly TimelineSegment[]): {
  label: string;
  tone: StatusTone;
  running: boolean;
} {
  if (segments.some((segment) => segment.state === 'live')) {
    return { label: '翻译中', tone: 'run', running: true };
  }
  if (segments.some((segment) => segment.state === 'err')) {
    return { label: '失败', tone: 'err', running: false };
  }
  if (segments.every((segment) => segment.state === 'ok')) {
    return { label: '已完成', tone: 'pass', running: false };
  }
  if (segments.every((segment) => segment.state === 'not_run')) {
    return { label: '未运行', tone: 'idle', running: false };
  }
  return { label: '未完成', tone: 'idle', running: false };
}
