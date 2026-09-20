/**
 * 右侧面板的「进度」tab（W06）：**双时间线**。
 *
 * 翻译与编译/成页是并行的两条流水（流式预览），一条串行事件流只能看到翻译、
 * 看不到页面在什么时候出来：
 * - 左列「翻译」：run 归档窗口（`useEventWindow`，kind 分组过滤 + 载入更早 +
 *   新事件浮标），最新在上；
 * - 右列「编译」：持久事件流（`usePersistentEvents` 返回的窗口：
 *   translation_block_completed / preview_ready / preview_failed / compile_float），
 *   只显示（用户可读的）编译侧事件，同样最新在上。
 *
 * 性能红线：两列各自只渲染窗口里的行（翻译 200 + 编译 200），3758 条 run 不做
 * 虚拟化也够（§4.6 的精神：最多保留 N 条 + 溢出滚动）。
 * 换 run（重新开始翻译）：左列滚动/浮标/展开行按 run 重置；右列游标是文档级
 * 持久游标，跨 run 连续累计，不重置。
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';

import { describeApiError } from '../../lib/api';
import { EVENTS_WINDOW_SIZE, KIND_GROUPS, kindGroup, type KindGroup, type RunEvent } from '../../lib/events';
import type { PersistentEvent } from '../../lib/usePersistentEvents';
import { Button } from '../ui/Button';
import { EventRow } from './EventRow';
import type { EventFeed } from './useEventWindow';
import type { SseStatus } from './useEventStream';

/** SSE 状态行文案（§4.6：头部标注「实时」脉冲点或错误态）。 */
const CONNECTION_TEXT: Record<SseStatus, string> = {
  idle: '未连接',
  connecting: '连接中…',
  open: '实时',
  reconnecting: '连接断开，重试中',
  closed: '连接已关闭',
  unsupported: '此环境不支持实时推送',
};

function ConnectionLine({ status }: { status: SseStatus }) {
  const live = status === 'open';
  const broken = status === 'reconnecting' || status === 'closed';
  return (
    <span
      data-od-id="event-stream-status"
      data-status={status}
      className={`inline-flex h-[19px] flex-none items-center gap-[5px] rounded-[3px] px-[6px] font-mono text-micro leading-none ${
        broken ? 'bg-err-soft text-err-ink' : live ? 'bg-run-soft text-run-ink' : 'bg-sand text-ink-4'
      }`}
    >
      <span
        aria-hidden="true"
        className={`h-[5px] w-[5px] flex-none rounded-full bg-current ${live ? 'pulse-dot' : ''}`}
      />
      {CONNECTION_TEXT[status]}
    </span>
  );
}

/** 编译时间线一行的可读摘要（右列）：把持久事件压成一行人话 + 语气色。 */
function compileLine(event: PersistentEvent): { text: string; tone: 'ok' | 'err' | 'info' } {
  const data = event.data ?? {};
  const block = event.blockId ?? (typeof data.paragraph_id === 'string' ? data.paragraph_id : null);
  const page = typeof data.page === 'number' ? data.page : event.page;
  switch (event.type) {
    case 'translation_block_completed':
      return { text: `${block ?? '?'} 译文提交`, tone: 'info' };
    case 'preview_ready':
      return {
        text: `第 ${page ?? '?'} 页${data.complete === false ? '（部分）' : ''}已更新`,
        tone: 'ok',
      };
    case 'preview_failed':
      return { text: `${block ?? '?'} 预览失败`, tone: 'err' };
    case 'compile_float':
      return { text: `${block ?? '?'} 贴片浮动 → 第 ${page ?? '?'} 页`, tone: 'info' };
    default:
      return { text: event.type, tone: 'info' };
  }
}

function CompileTimeline({ events }: { events: PersistentEvent[] }) {
  const rows = useMemo(() => [...events].reverse(), [events]);
  const blocksDone = useMemo(
    () => events.filter((event) => event.type === 'translation_block_completed').length,
    [events],
  );
  const pagesReady = useMemo(
    () => events.filter((event) => event.type === 'preview_ready').length,
    [events],
  );
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const atTopRef = useRef(true);
  useLayoutEffect(() => {
    const element = scrollRef.current;
    if (element !== null && atTopRef.current) element.scrollTop = 0;
  }, [rows]);
  const onScroll = useCallback(() => {
    const element = scrollRef.current;
    if (element === null) return;
    atTopRef.current = element.scrollTop <= 4;
  }, []);
  return (
    <div className="flex min-h-0 min-w-0 flex-1 flex-col border-l border-hair" data-od-id="compile-timeline">
      <div className="flex flex-none items-center gap-s2 border-b border-hair px-s2 py-[6px]">
        <span className="font-serif text-sm font-medium leading-[1.35] text-ink-2">编译</span>
        <span className="font-mono text-micro text-ink-4 [font-variant-numeric:tabular-nums]">
          块 {blocksDone} · 页 {pagesReady}
        </span>
      </div>
      <div
        ref={scrollRef}
        onScroll={onScroll}
        data-od-id="compile-timeline-scroll"
        className="min-h-0 flex-1 overflow-auto"
      >
        {rows.length === 0 ? (
          <p className="px-s2 py-s2 text-micro text-ink-4" data-od-id="compile-timeline-empty">
            还没有编译/成页事件（翻译提交后这里逐块出现）
          </p>
        ) : (
          <ul className="flex flex-col">
            {rows.map((event) => {
              const line = compileLine(event);
              return (
                <li
                  key={event.seq}
                  data-od-id="compile-timeline-row"
                  data-type={event.type}
                  title={event.at ?? undefined}
                  className={`border-b border-hair px-s2 py-[3px] font-mono text-micro leading-[1.5] ${
                    line.tone === 'ok' ? 'text-run-ink' : line.tone === 'err' ? 'text-err-ink' : 'text-ink-3'
                  }`}
                >
                  {line.text}
                </li>
              );
            })}
          </ul>
        )}
      </div>
    </div>
  );
}

export function EventStreamPanel({
  did,
  feed,
  compileEvents,
}: {
  did: string;
  feed: EventFeed;
  compileEvents: PersistentEvent[];
}) {
  const [group, setGroup] = useState<KindGroup | 'all'>('all');
  // 面板的交互态按 **run** 归属（与 `useEventWindow` 的窗口/游标同口径）：换 run
  // （重新开始翻译）后新的 seq 从 1 重新计数，旧的展开行、浮标、滚动位置都不再成立。
  // 状态带 key 而不是用 effect 清空：换 run 的那一次渲染就已经是干净值。
  const runKey = feed.runId ?? '';
  const [expanded, setExpanded] = useState<{ run: string; seq: number | null }>({ run: '', seq: null });
  const expandedSeq = expanded.run === runKey ? expanded.seq : null;
  const [pending, setPending] = useState<{ run: string; count: number }>({ run: '', count: 0 });
  const pendingCount = pending.run === runKey ? pending.count : 0;
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const atTopRef = useRef(true);
  // 已见最新 seq 也按 run 归属：新 run 的 seq 从 1 重新计数，拿旧 run 的最大 seq 做差
  // 会算出负数，浮标与跟随判断就全错了。
  const headRef = useRef<{ run: string; seq: number }>({ run: '', seq: 0 });

  // 显示顺序：窗口升序 → 反转成「最新在上」（§4.6）。
  const rows = useMemo(() => {
    const visible =
      group === 'all' ? feed.events : feed.events.filter((event) => kindGroup(event.kind) === group);
    return [...visible].reverse();
  }, [feed.events, group]);

  // 换 run：面板回到「跟随最新」——新 run 的事件从第 1 条开始显示，滚动贴住顶部。
  useLayoutEffect(() => {
    atTopRef.current = true;
    const element = scrollRef.current;
    if (element !== null) element.scrollTop = 0;
  }, [runKey]);

  // 新事件到达：停在顶部就继续跟随（贴住最新），否则只累计浮标数（不打断阅读）。
  useEffect(() => {
    const newest = feed.events[feed.events.length - 1]?.seq ?? 0;
    const seen = headRef.current.run === runKey ? headRef.current.seq : 0;
    const delta = newest - seen;
    headRef.current = { run: runKey, seq: newest };
    if (delta <= 0) return;
    if (atTopRef.current) {
      setPending({ run: runKey, count: 0 });
      return;
    }
    setPending((current) => ({
      run: runKey,
      count: (current.run === runKey ? current.count : 0) + delta,
    }));
  }, [feed.events, runKey]);

  // 滚动锚定由浏览器负责（不关 overflow-anchor）；只有「停在顶部」需要显式贴回 0。
  useLayoutEffect(() => {
    if (!atTopRef.current) return;
    const element = scrollRef.current;
    if (element !== null) element.scrollTop = 0;
  }, [rows]);

  const onScroll = useCallback(() => {
    const element = scrollRef.current;
    if (element === null) return;
    const atTop = element.scrollTop <= 4;
    atTopRef.current = atTop;
    if (atTop) setPending({ run: runKey, count: 0 });
  }, [runKey]);

  const jumpToNewest = useCallback(() => {
    const element = scrollRef.current;
    if (element !== null) element.scrollTop = 0;
    atTopRef.current = true;
    setPending({ run: runKey, count: 0 });
  }, [runKey]);

  const toggleRow = useCallback(
    (seq: number) => {
      setExpanded((current) => {
        const previous = current.run === runKey ? current.seq : null;
        return { run: runKey, seq: previous === seq ? null : seq };
      });
    },
    [runKey],
  );

  const body = () => {
    if (feed.isPending) {
      return <p className="px-s3 py-s2 text-tiny text-ink-4" data-od-id="event-stream-empty">正在加载事件…</p>;
    }
    if (!feed.hasArchive) {
      if (feed.error !== null && feed.error !== undefined) {
        const described = describeApiError(feed.error);
        return (
          <div className="px-s3 py-s2" data-od-id="event-stream-empty">
            <p className="text-tiny text-ink-2">{described.title}</p>
            <p className="mt-[2px] font-mono text-micro text-ink-4">{described.detail ?? described.message}</p>
            <Button size="sm" className="mt-s2" onClick={feed.retry}>
              重试
            </Button>
          </div>
        );
      }
      return (
        <p className="px-s3 py-s2 text-tiny text-ink-4" data-od-id="event-stream-empty">
          该文档没有 run 归档（debug/runs 为空），没有事件可看
        </p>
      );
    }
    if (feed.events.length === 0) {
      return (
        <p className="px-s3 py-s2 text-tiny text-ink-4" data-od-id="event-stream-empty">
          这个 run 还没有事件（刚启动时 events.jsonl 还没落盘）
        </p>
      );
    }
    if (rows.length === 0) {
      return (
        <p className="px-s3 py-s2 text-tiny text-ink-4" data-od-id="event-stream-empty">
          当前分组（{KIND_GROUPS.find((item) => item.id === group)?.label ?? group}）在窗口里没有事件
        </p>
      );
    }
    return (
      <ul className="flex flex-col">
        {rows.map((event: RunEvent) => (
          <EventRow
            key={event.seq}
            event={event}
            expanded={expandedSeq === event.seq}
            onToggle={toggleRow}
          />
        ))}
      </ul>
    );
  };

  return (
    <section
      aria-label="事件流"
      data-od-id="event-stream"
      className="flex min-h-0 flex-1 flex-col"
    >
      <div className="flex flex-none items-center gap-s2 border-b border-hair px-s3 py-[6px]">
        <span className="font-serif text-sm font-medium leading-[1.35] text-ink-2">事件流</span>
        <ConnectionLine status={feed.connection} />
        <div className="ml-auto flex flex-none items-center gap-s2">
          <label className="sr-only" htmlFor="event-kind-group">
            事件分组
          </label>
          <select
            id="event-kind-group"
            data-od-id="event-kind-filter"
            value={group}
            onChange={(event) => setGroup(event.target.value as KindGroup | 'all')}
            className="h-6 rounded border border-hair-2 bg-ivory px-[6px] font-mono text-micro text-ink-2"
          >
            {KIND_GROUPS.map((item) => (
              <option key={item.id} value={item.id}>
                {item.label}
              </option>
            ))}
          </select>
          <Button
            size="sm"
            data-od-id="event-load-earlier"
            disabled={!feed.hasEarlier || feed.isLoadingEarlier}
            onClick={feed.loadEarlier}
            title={feed.hasEarlier ? '向更早翻 500 条' : '窗口已经到事件流的开头'}
          >
            载入更早
          </Button>
        </div>
      </div>
      <div className="flex flex-none items-center gap-s2 border-b border-hair px-s3 py-[3px] font-mono text-micro text-ink-4">
        <span className="min-w-0 truncate" data-od-id="event-run-id" title={feed.runId ?? ''}>
          翻译 run {feed.runId ?? '—'}
        </span>
        <span className="ml-auto flex-none [font-variant-numeric:tabular-nums]">
          显示 {rows.length}/{feed.events.length} · 窗口 {EVENTS_WINDOW_SIZE} 条
        </span>
      </div>
      <div className="relative flex min-h-0 flex-1">
        <div className="relative min-h-0 min-w-0 flex-1">
          <div
            ref={scrollRef}
            onScroll={onScroll}
            data-od-id="event-stream-scroll"
            className="h-full overflow-auto"
          >
            {body()}
          </div>
          {pendingCount > 0 ? (
            <button
              type="button"
              data-od-id="event-new-events"
              onClick={jumpToNewest}
              className="absolute left-1/2 top-s2 h-6 -translate-x-1/2 rounded border border-hair-2 bg-ivory px-s3 font-mono text-micro text-ink-2 shadow-lift"
            >
              ↑ {pendingCount} 条新事件
            </button>
          ) : null}
        </div>
        <CompileTimeline events={compileEvents} />
      </div>
      <p className="flex-none border-t border-hair px-s3 py-[3px] font-mono text-micro text-ink-4">
        {did} · 左：翻译 run 归档 · 右：编译/成页（文档级游标）
      </p>
    </section>
  );
}
