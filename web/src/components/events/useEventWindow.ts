/**
 * 事件面板的数据层：首拉尾部窗口（`useEventsTail`）+ SSE 增量（`useEventStream`）+
 * 「载入更早」分页（`useEvents`）。
 *
 * 窗口语义：
 * - 默认保留**最近 200 条**（§4.6 的性能红线：3758 条全渲染 DOM 会卡）；
 * - SSE 增量按 `seq` 去重后并入（断线重连会重发 `after_seq` 之后的事件）；
 * - 「载入更早」把更早的一页（500 条）并入并**放宽**上限（默认 200 + 已载入条数），
 *   已载入的历史不再被尾部裁剪吃掉；
 * - 没有 run 归档（404 `events_unavailable`）→ `hasArchive=false`，不建 EventSource；
 *   有活动 job 时首拉按 5s 重拉（新 run 归档只能这样被发现，见 `activeJob` 选项）。
 *
 * 结构上「首拉窗口」留在 query 里，本地 state 只放**增量**（SSE + 更早页），并按 `runKey`
 * 归属：换 run 时旧增量自动失效，不需要 effect 清状态。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';

import { ApiError } from '../../lib/api';
import {
  EVENTS_EARLIER_PAGE,
  EVENTS_PAGE_LIMIT,
  EVENTS_WINDOW_SIZE,
  mergeEarlierWindow,
  mergeTailWindow,
  runEventsFromPage,
  type JobUpdate,
  type RunEvent,
} from '../../lib/events';
import { EVENT_KINDS } from '../../lib/humanize';
import { EVENTS_TAIL_DISCOVERY_REFETCH_MS } from '../../lib/jobs';
import { useEvents, useEventsTail } from '../../lib/queries';
import { useEventStream, type EventSourceFactory, type SseStatus } from './useEventStream';

interface ExtraEvents {
  /** 增量归属的 run（`runKey`）；不匹配就丢弃（换了 run / 首拉还没到）。 */
  key: string;
  /** SSE 增量 + 已载入的更早页，升序。 */
  events: RunEvent[];
  /** 尾部窗口上限：200 + 已载入的更早条数（载入更早会放宽，不被 200 上限吃掉）。 */
  cap: number;
}

export interface EventFeed {
  /** 窗口内事件，升序（最旧 → 最新）；面板显示时反转成「最新在上」。 */
  events: RunEvent[];
  /** 本页事件来源的 run（SSE 固定订阅它）。 */
  runId: string | null;
  /** 首拉还在路上。 */
  isPending: boolean;
  /** 该文档有 run 归档（首拉成功）。false 时面板显示空态、不建 SSE。 */
  hasArchive: boolean;
  /** 真错误（`events_unavailable` 之外）；「没有 run 归档」不算错误。 */
  error: unknown;
  /** SSE 连接状态（`open` 才显示「实时」脉冲点）。 */
  connection: SseStatus;
  /** 窗口最旧的一条之前还有事件。 */
  hasEarlier: boolean;
  isLoadingEarlier: boolean;
  loadEarlier: () => void;
  retry: () => void;
}

export function useEventWindow(
  did: string | null,
  options: {
    createEventSource?: EventSourceFactory;
    /** `job_update` 回调（W14）：虚拟 kind，不进事件窗口，只用于缓存失效。 */
    onJobUpdate?: (update: JobUpdate) => void;
    /**
     * 有活动 job（`queued`/`running`）：首拉窗口按 `EVENTS_TAIL_DISCOVERY_REFETCH_MS` 重拉，
     * 直到 job 结束。首个 job 之前文档根本没有 run 归档（首拉 404），而**新 run 只能靠
     * 首拉才能被 SSE 发现**（EventSource 订阅的是首拉拿到的那个 run_id）。
     */
    activeJob?: boolean;
  } = {},
): EventFeed {
  const tail = useEventsTail(did, {
    refetchMs:
      options.activeJob === true && did !== null ? EVENTS_TAIL_DISCOVERY_REFETCH_MS : 0,
  });
  const tailData = tail.data;
  const runId = tailData?.runId ?? null;
  const runKey = `${did ?? ''}\u0000${runId ?? ''}`;
  const baseEvents = useMemo(() => tailData?.events ?? [], [tailData]);

  const [extra, setExtra] = useState<ExtraEvents>({ key: '', events: [], cap: EVENTS_WINDOW_SIZE });
  // 「载入更早」的游标也按 run 归属：换 run 后旧的游标自然失效（不需要清状态的 effect）。
  const [cursor, setCursor] = useState<{ key: string; afterSeq: number | null }>({
    key: '',
    afterSeq: null,
  });
  const earlierAfterSeq = cursor.key === runKey ? cursor.afterSeq : null;

  // SSE 回调是外部系统驱动的异步回调，读 runKey 要用 ref（ref 只在 effect 里写）。
  const runKeyRef = useRef(runKey);
  useEffect(() => {
    runKeyRef.current = runKey;
  }, [runKey]);

  const appendEvents = useCallback((incoming: RunEvent[]) => {
    setExtra((prev) => {
      const key = runKeyRef.current;
      const current = prev.key === key ? prev : { key, events: [], cap: EVENTS_WINDOW_SIZE };
      return { key, events: mergeTailWindow(current.events, incoming, current.cap), cap: current.cap };
    });
  }, []);

  const { createEventSource, onJobUpdate } = options;
  const connection = useEventStream({
    did,
    runId,
    afterSeq: tailData?.nextAfterSeq ?? null,
    kinds: EVENT_KINDS,
    onEvents: appendEvents,
    onJobUpdate,
    createEventSource,
  });

  const events = useMemo(() => {
    if (extra.key !== runKey) return baseEvents;
    return mergeTailWindow(baseEvents, extra.events, extra.cap);
  }, [baseEvents, extra, runKey]);

  // 「载入更早」：`after_seq` 只能正向扫，所以从「最旧 seq - 1 - 一页」起扫，再只留
  // `seq < 最旧 seq` 的尾部 500 条（一次请求；窗口策略见报告）。
  const olderCursor = events[0]?.seq ?? null;
  const earlier = useEvents(did, {
    afterSeq: earlierAfterSeq ?? 0,
    limit: EVENTS_PAGE_LIMIT,
    enabled: earlierAfterSeq !== null,
  });
  const earlierData = earlier.data;

  useEffect(() => {
    if (earlierData === undefined) return;
    // 分页结果要并进本地窗口增量：这是「外部数据源 → 本地累积态」的同步（不是派生渲染状态）。
    setExtra((prev) => {
      const key = runKeyRef.current;
      const current = prev.key === key ? prev : { key, events: [], cap: EVENTS_WINDOW_SIZE };
      const oldest = current.events[0]?.seq ?? baseEvents[0]?.seq ?? 0;
      const older = runEventsFromPage(earlierData)
        .filter((event) => event.seq < oldest)
        .slice(-EVENTS_EARLIER_PAGE);
      if (older.length === 0) return prev;
      return {
        key,
        events: mergeEarlierWindow(current.events, older, current.cap),
        cap: current.cap + older.length,
      };
    });
  }, [earlierData, baseEvents]);

  const loadEarlier = useCallback(() => {
    if (olderCursor === null || olderCursor <= 1) return;
    setCursor({ key: runKey, afterSeq: Math.max(0, olderCursor - 1 - EVENTS_PAGE_LIMIT) });
  }, [olderCursor, runKey]);

  const notFound = tail.error instanceof ApiError && tail.error.status === 404;
  const hasArchive = tailData !== undefined;
  // 没有 run 归档（404 events_unavailable）不是错误，由面板显示空态；真错误才交给错误卡。
  const error = !hasArchive && !tail.isPending && !notFound ? tail.error : null;

  return {
    events,
    runId,
    isPending: tail.isPending,
    hasArchive,
    error,
    connection,
    hasEarlier: olderCursor !== null && olderCursor > 1,
    isLoadingEarlier: earlier.isFetching,
    loadEarlier,
    retry: () => void tail.refetch(),
  };
}
