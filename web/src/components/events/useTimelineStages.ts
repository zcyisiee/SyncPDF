/**
 * 时间线数据：`stage-state`（基线）+ 事件流（live 段的本地已进行秒数）+ 活动 job（W14：
 * 新 run 的 stage-state 还没落盘时，job 自己是“这个阶段在跑”的真信号）。
 * `live` 时开 1s ticker 只重算 live 段的秒表（**不做**任何假百分比/ETA）。
 *
 * “job 驱动”那一段的判定（哪个 action、哪个阶段、基线要不要让路）全在
 * `lib/timeline.ts::jobLiveStage`，这里只把该文档的最新一条 job 透传过去。
 */
import { useEffect, useMemo, useState } from 'react';

import type { JobRecord } from '../../api/types';
import { useStageState } from '../../lib/queries';
import type { RunEvent } from '../../lib/events';
import {
  hasLiveSegment,
  jobLiveStage,
  timelineSegments,
  type TimelineSegment,
} from '../../lib/timeline';

/** live 段的秒表刷新间隔。 */
export const TIMELINE_TICK_MS = 1_000;

export interface TimelineData {
  segments: TimelineSegment[];
  /** 有 live 段 ⇔ 文档正在跑（顶栏/视图栏徽标 + 详情自动刷新都用它）。 */
  live: boolean;
  isPending: boolean;
  isError: boolean;
  error: unknown;
}

export function useTimelineStages(
  did: string | null,
  events: readonly RunEvent[],
  options: {
    refetchMs?: number;
    /**
     * 活动 job 记录（`useJobs` 的第一条）：`running` 且 `action=run|check` 时，把基线还没
     * 定论的那个阶段标成 live（W14：新 run 的 stage-state 还没落盘）。
     */
    job?: JobRecord | null;
  } = {},
): TimelineData {
  const stageState = useStageState(did, options.refetchMs ?? 0);
  const [nowMs, setNowMs] = useState(() => Date.now());

  const driven = jobLiveStage(options.job);
  const segments = useMemo(
    () => timelineSegments(stageState.data?.stages, events, nowMs, driven),
    [stageState.data, events, nowMs, driven],
  );
  const live = hasLiveSegment(segments);

  useEffect(() => {
    if (!live) return;
    // 秒表只在 live 时跑：对齐 live 的那一帧用上一次的 `nowMs`（最多差 1s，下一个 tick 就纠正），
    // 不在 effect 里同步 setState（react-hooks/set-state-in-effect）。
    const timer = setInterval(() => setNowMs(Date.now()), TIMELINE_TICK_MS);
    return () => clearInterval(timer);
  }, [live]);

  return {
    segments,
    live,
    isPending: stageState.isPending,
    isError: stageState.isError,
    error: stageState.error,
  };
}
