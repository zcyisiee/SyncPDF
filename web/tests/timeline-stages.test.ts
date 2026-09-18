/**
 * `lib/timeline.ts`：stage-state 基线 + 事件流 live 段 + job 驱动 live（W14）的合并、
 * 条宽加权、视图映射、总用时。
 * 基线优先是核心不变式：`ok`/`failed` 的基线永远赢（事件归档被截断、或新 run 还没落盘时
 * 也不能把已完成的阶段显示成进行中）。
 */
import { describe, expect, it } from 'vitest';

import type { StageStateItem } from '../src/api/types';
import type { RunEvent } from '../src/lib/events';
import {
  barWidthPercent,
  elapsedSeconds,
  hasLiveSegment,
  jobLiveStage,
  maxSegmentDuration,
  openStageAt,
  timelineSegments,
  timelineStatus,
  totalDurationS,
  type TimelineSegment,
} from '../src/lib/timeline';

const NOW = Date.parse('2026-09-16T13:30:00.000Z');

function stage(
  name: string,
  status: string,
  duration: number | null,
  startedAt: string | null = '2026-09-16T13:00:00.000Z',
): StageStateItem {
  return {
    stage: name,
    status,
    ok: status === 'ok',
    started_at: startedAt,
    finished_at: status === 'ok' ? '2026-09-16T13:10:00.000Z' : null,
    duration_s: duration,
    timing_source: 'manifest',
  };
}

function event(seq: number, kind: string, stage: string, at: string): RunEvent {
  return { seq, at, stage, kind, data: {} };
}

/** 真 fixture 的形状：7 段全 ok。 */
const COMPLETE: StageStateItem[] = [
  stage('parse', 'ok', 15.83),
  stage('translate', 'ok', 248.93),
  stage('apply', 'ok', 4.07),
  stage('build', 'ok', 23.85),
  stage('check', 'ok', 25.23),
  stage('review', 'ok', 0.32),
  stage('report', 'ok', 0),
];

describe('timelineSegments', () => {
  it('全 ok：7 段都是 ok + 实测耗时；基线优先时事件流的误报 live 不算数', () => {
    // 归档被截断的事件流（末条是 check 的 stage_finished → isRunLive 会返回 true）
    const events = [
      event(1, 'stage_started', 'parse', '2026-09-16T13:28:29.373Z'),
      event(2, 'stage_finished', 'check', '2026-09-16T13:28:29.470Z'),
    ];
    const segments = timelineSegments(COMPLETE, events, NOW);
    expect(segments.map((segment) => segment.state)).toEqual(Array(7).fill('ok'));
    expect(segments[1].durationS).toBeCloseTo(248.93);
    expect(hasLiveSegment(segments)).toBe(false);
    expect(timelineStatus(segments).label).toBe('已完成');
  });

  it('failed：状态是 err（红），耗时仍取基线', () => {
    const stages = [...COMPLETE.slice(0, 4), stage('check', 'failed', 9.5)];
    const segments = timelineSegments(stages, [], NOW);
    expect(segments.find((segment) => segment.stage === 'check')?.state).toBe('err');
    expect(segments.find((segment) => segment.stage === 'check')?.durationS).toBe(9.5);
    expect(timelineStatus(segments).label).toBe('失败');
  });

  it('not_run：基线没跑 + 事件里没有未配对的 stage_started → 未运行', () => {
    const segments = timelineSegments(
      [stage('parse', 'ok', 12), stage('translate', 'not_run', null, null)],
      [],
      NOW,
    );
    const translate = segments.find((segment) => segment.stage === 'translate');
    expect(translate?.state).toBe('not_run');
    expect(translate?.durationS).toBeNull();
    expect(timelineStatus(segments).label).toBe('未完成');
  });

  it('live：基线没定论 + 事件有未配对的 stage_started → live + 本地已进行秒数', () => {
    const startedAt = '2026-09-16T13:28:20.000Z'; // NOW - 100s
    const segments = timelineSegments(
      [stage('parse', 'ok', 12), stage('translate', 'not_run', null, null)],
      [event(1, 'stage_started', 'parse', '2026-09-16T13:00:00.000Z'), event(2, 'stage_started', 'translate', startedAt)],
      NOW,
    );
    const translate = segments.find((segment) => segment.stage === 'translate');
    expect(translate?.state).toBe('live');
    expect(translate?.elapsedS).toBe(100);
    expect(hasLiveSegment(segments)).toBe(true);
    expect(timelineStatus(segments).label).toBe('翻译中');
    expect(timelineStatus(segments).tone).toBe('run');
    expect(timelineStatus(segments).running).toBe(true);
  });

  it('stage-state 全空（还没拉到）→ 7 段未运行，不是错误状态', () => {
    const segments = timelineSegments(undefined, [], NOW);
    expect(segments).toHaveLength(7);
    expect(segments.every((segment) => segment.state === 'not_run')).toBe(true);
    expect(totalDurationS(segments)).toBeNull();
  });

  it('耗时缺失（duration_s 为 null）时用 started/finished 反推；两头都缺 → null', () => {
    const segments = timelineSegments(
      [
        { ...stage('parse', 'ok', null), started_at: '2026-09-16T13:00:00.000Z', finished_at: '2026-09-16T13:00:30.000Z' },
        { ...stage('translate', 'ok', null), started_at: null, finished_at: null },
      ],
      [],
      NOW,
    );
    expect(segments[0].durationS).toBe(30);
    expect(segments[1].durationS).toBeNull();
  });

  it('事件里 stage_started 已配对 stage_finished → 不算 live（即使整体 isRunLive 为 true）', () => {
    const segments = timelineSegments(
      [stage('translate', 'not_run', null, null)],
      [
        event(1, 'stage_started', 'translate', '2026-09-16T13:00:00.000Z'),
        event(2, 'stage_finished', 'translate', '2026-09-16T13:05:00.000Z'),
      ],
      NOW,
    );
    expect(segments.find((segment) => segment.stage === 'translate')?.state).toBe('not_run');
  });
});

describe('jobLiveStage（W14：job 驱动的 live 段）', () => {
  const job = (overrides: Record<string, unknown>) => ({
    action: 'run',
    status: 'running',
    from_stage: 'translate',
    started_at: '2026-09-16T13:28:20.000Z',
    ...overrides,
  });

  it('只有 running 的 run/check 才驱动时间线（起点 = from_stage，缺省 parse）', () => {
    expect(jobLiveStage(job({}))).toEqual({
      stage: 'translate',
      at: '2026-09-16T13:28:20.000Z',
    });
    expect(jobLiveStage(job({ from_stage: null }))?.stage).toBe('parse');
    expect(jobLiveStage(job({ action: 'check', from_stage: 'check' }))?.stage).toBe('check');
  });

  it('queued/终态不驱动（还没跑 / 已经跑完）；compile/retranslate 永远不驱动（隔离副本）', () => {
    expect(jobLiveStage(job({ status: 'queued' }))).toBeNull();
    expect(jobLiveStage(job({ status: 'succeeded' }))).toBeNull();
    expect(jobLiveStage(job({ status: 'failed' }))).toBeNull();
    for (const action of ['compile', 'retranslate']) {
      expect(jobLiveStage(job({ action, from_stage: 'apply' }))).toBeNull();
    }
    expect(jobLiveStage(null)).toBeNull();
    expect(jobLiveStage(undefined)).toBeNull();
    // 未知 action / 非法阶段名不硬转成 live
    expect(jobLiveStage(job({ action: 'future', from_stage: 'translate' }))).toBeNull();
    expect(jobLiveStage(job({ from_stage: 'nope' }))).toBeNull();
  });

  it('基线优先：job 驱动只在基线没定论时生效，ok/err 阶段不被改写', () => {
    const stages = [stage('parse', 'ok', 15.83), stage('translate', 'failed', 9.5)];
    const segments = timelineSegments(stages, [], NOW, { stage: 'translate', at: null });
    expect(segments.find((item) => item.stage === 'parse')?.state).toBe('ok');
    expect(segments.find((item) => item.stage === 'translate')?.state).toBe('err');
  });

  it('job 驱动的那一段：live + 秒表从 job 的 started_at 算（拿不到时刻就不编造秒数）', () => {
    // 新 run 的 stage-state 还没落盘：全部 not_run，但 job 说 translate 在跑
    const segments = timelineSegments(
      undefined,
      [],
      NOW,
      { stage: 'translate', at: '2026-09-16T13:28:20.000Z' },
    );
    const translate = segments.find((item) => item.stage === 'translate');
    expect(translate?.state).toBe('live');
    expect(translate?.status).toBe('running');
    expect(translate?.elapsedS).toBe(100);
    expect(hasLiveSegment(segments)).toBe(true);
    expect(timelineStatus(segments).label).toBe('翻译中');
    // 其它阶段仍然 not_run（不假装整条时间线都在跑）
    expect(segments.filter((item) => item.state === 'live')).toHaveLength(1);

    const noClock = timelineSegments(undefined, [], NOW, { stage: 'translate', at: null });
    expect(noClock.find((item) => item.stage === 'translate')?.elapsedS).toBeNull();
  });

  it('没给 job（或 job 不驱动）时行为与 W13 一致', () => {
    const baseline = timelineSegments(undefined, [], NOW);
    const withNull = timelineSegments(undefined, [], NOW, null);
    expect(withNull).toEqual(baseline);
  });
});

describe('openStageAt / elapsedSeconds', () => {
  it('未配对的 stage_started → 取它的 at；已配对 → null', () => {
    const events = [
      event(1, 'stage_started', 'translate', '2026-09-16T13:00:00.000Z'),
      event(2, 'stage_finished', 'translate', '2026-09-16T13:01:00.000Z'),
      event(3, 'stage_started', 'translate', '2026-09-16T13:02:00.000Z'),
    ];
    expect(openStageAt(events, 'translate')).toBe('2026-09-16T13:02:00.000Z');
    expect(openStageAt(events, 'parse')).toBeNull();
  });

  it('elapsedSeconds：整除取秒；≤0 归零；坏时刻 null', () => {
    expect(elapsedSeconds('2026-09-16T13:29:00.000Z', NOW)).toBe(60);
    expect(elapsedSeconds('2026-09-16T13:31:00.000Z', NOW)).toBe(0);
    expect(elapsedSeconds('', NOW)).toBeNull();
    expect(elapsedSeconds('nope', NOW)).toBeNull();
  });
});

describe('条宽加权（§8.3：该段耗时 / 最长段，共享线性标尺）', () => {
  it('最长段 = 100%，其余按比例；未开始段不参与（返回 null → 固定 64px 虚线）', () => {
    const segments = timelineSegments(COMPLETE, [], NOW);
    const max = maxSegmentDuration(segments);
    expect(max).toBeCloseTo(248.93);
    const translate = segments[1];
    const parse = segments[0];
    expect(barWidthPercent(translate, max)).toBeCloseTo(100);
    expect(barWidthPercent(parse, max)).toBeCloseTo((15.83 / 248.93) * 100);
    expect(barWidthPercent(segments[6], max)).toBe(2); // report 0s → 最小可见宽
    const notRun = timelineSegments([], [], NOW)[0];
    expect(barWidthPercent(notRun, max)).toBeNull();
  });

  it('live 段用本地秒数算宽，最长段为 0 时给最小宽', () => {
    const segments = timelineSegments(
      [stage('parse', 'ok', 0)],
      [
        event(1, 'stage_started', 'translate', '2026-09-16T13:29:00.000Z'),
        event(2, 'call_started', 'translate', '2026-09-16T13:29:01.000Z'),
      ],
      NOW,
    );
    const live = segments.find((segment) => segment.stage === 'translate') as TimelineSegment;
    expect(live.state).toBe('live');
    expect(live.elapsedS).toBe(60);
    // 分母是「最长的已完成段」= report 之外没有已完成的段 → 0 → 给最小可见宽
    expect(maxSegmentDuration(segments)).toBe(0);
    expect(barWidthPercent(live, maxSegmentDuration(segments))).toBe(2);
  });
});

describe('总用时与视图映射', () => {
  it('总用时 = 已完成阶段实测耗时之和（不编造 ETA）', () => {
    const segments = timelineSegments(COMPLETE, [], NOW);
    expect(totalDurationS(segments)).toBeCloseTo(318.23);
  });

  it('live 段的已进行秒数计入总用时', () => {
    const segments = timelineSegments(
      [stage('parse', 'ok', 12)],
      [event(1, 'stage_started', 'translate', '2026-09-16T13:29:00.000Z')],
      NOW,
    );
    expect(totalDurationS(segments)).toBe(72);
  });

  it('段不可点击（视图合并后没有跳转目标）：segment 不再带 view 字段', () => {
    const segments = timelineSegments(COMPLETE, [], NOW);
    for (const segment of segments) {
      expect('view' in segment).toBe(false);
    }
  });
});
