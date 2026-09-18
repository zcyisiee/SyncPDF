/**
 * 右侧面板的「进度」tab（W06）：顶部状态行 + kind 分组过滤 + 事件窗口（尾部 200 条）+ 载入更早。
 *
 * 性能红线：只渲染窗口里的行（默认 200，展开后 200 + 已载入的历史），3758 条 run 不做虚拟化也够
 * （§4.6 的精神：最多保留 N 条 + 溢出滚动）。
 * 排序：**最新在上**（§4.6）；所以「自动跟随」= 停在列表顶部，浮标是「↑ N 条新事件」（点击回顶）。
 * 面板结构留了 tab 位（W10 会加段落 tab）：当前只有一个 tab 头，不假装有多个页签。
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';

import { describeApiError } from '../../lib/api';
import { EVENTS_WINDOW_SIZE, KIND_GROUPS, kindGroup, type KindGroup, type RunEvent } from '../../lib/events';
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

export function EventStreamPanel({ did, feed }: { did: string; feed: EventFeed }) {
  const [group, setGroup] = useState<KindGroup | 'all'>('all');
  const [expandedSeq, setExpandedSeq] = useState<number | null>(null);
  const [pending, setPending] = useState(0);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const atTopRef = useRef(true);
  const headSeqRef = useRef(0);

  // 显示顺序：窗口升序 → 反转成「最新在上」（§4.6）。
  const rows = useMemo(() => {
    const visible =
      group === 'all' ? feed.events : feed.events.filter((event) => kindGroup(event.kind) === group);
    return [...visible].reverse();
  }, [feed.events, group]);

  // 新事件到达：停在顶部就继续跟随（贴住最新），否则只累计浮标数（不打断阅读）。
  useEffect(() => {
    const newest = feed.events[feed.events.length - 1]?.seq ?? 0;
    const delta = newest - headSeqRef.current;
    headSeqRef.current = newest;
    if (delta <= 0) return;
    if (atTopRef.current) {
      setPending(0);
      return;
    }
    setPending((current) => current + delta);
  }, [feed.events]);

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
    if (atTop) setPending(0);
  }, []);

  const jumpToNewest = useCallback(() => {
    const element = scrollRef.current;
    if (element !== null) element.scrollTop = 0;
    atTopRef.current = true;
    setPending(0);
  }, []);

  const toggleRow = useCallback((seq: number) => {
    setExpandedSeq((current) => (current === seq ? null : seq));
  }, []);

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
          run {feed.runId ?? '—'}
        </span>
        <span className="ml-auto flex-none [font-variant-numeric:tabular-nums]">
          显示 {rows.length}/{feed.events.length} · 窗口 {EVENTS_WINDOW_SIZE} 条
        </span>
      </div>
      <div className="relative min-h-0 flex-1">
        <div
          ref={scrollRef}
          onScroll={onScroll}
          data-od-id="event-stream-scroll"
          className="h-full overflow-auto"
        >
          {body()}
        </div>
        {pending > 0 ? (
          <button
            type="button"
            data-od-id="event-new-events"
            onClick={jumpToNewest}
            className="absolute left-1/2 top-s2 h-6 -translate-x-1/2 rounded border border-hair-2 bg-ivory px-s3 font-mono text-micro text-ink-2 shadow-lift"
          >
            ↑ {pending} 条新事件
          </button>
        ) : null}
      </div>
      <p className="flex-none border-t border-hair px-s3 py-[3px] font-mono text-micro text-ink-4">
        {did} · 事件与阶段耗时都来自真实归档
      </p>
    </section>
  );
}
