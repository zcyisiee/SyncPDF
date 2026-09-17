/**
 * 时间线数据：`stage-state`（基线）+ 事件流（live 段的本地已进行秒数）。
 * `live` 时开 1s ticker 只重算 live 段的秒表（**不做**任何假百分比/ETA）。
 */
import { useEffect, useMemo, useState } from 'react';

import { useStageState } from '../../lib/queries';
import type { RunEvent } from '../../lib/events';
import { hasLiveSegment, timelineSegments, type TimelineSegment } from '../../lib/timeline';

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
  options: { refetchMs?: number } = {},
): TimelineData {
  const stageState = useStageState(did, options.refetchMs ?? 0);
  const [nowMs, setNowMs] = useState(() => Date.now());

  const segments = useMemo(
    () => timelineSegments(stageState.data?.stages, events, nowMs),
    [stageState.data, events, nowMs],
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
