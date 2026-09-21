/**
 * 右侧面板的「进度」tab（W06）：**双时间线** + 顶部单行阶段条。
 *
 * 翻译与编译/成页是并行的两条流水（流式预览），一条串行事件流只能看到翻译、
 * 看不到页面在什么时候出来：
 * - 左列「翻译」：run 归档窗口（`useEventWindow`，kind 分组过滤 + 载入更早 +
 *   新事件浮标），最新在上；
 * - 右列「编译」：持久事件流（`usePersistentEvents` 返回的窗口：
 *   translation_block_completed / preview_ready / preview_failed / compile_float），
 *   只显示（用户可读的）编译侧事件，同样最新在上。
 *
 * 阶段条（`stage-strip`）在双列之上：7 段真实状态（`useTimelineStages`：stage-state 基线 +
 * 事件流的 live 段），右侧是 live 秒表或上一次结论。它是**同一份查询**的第二个订阅方，
 * 不新起请求口径、也不做假百分比/假 ETA。
 *
 * 性能红线：两列各自只渲染窗口里的行（翻译 200 + 编译 200），3758 条 run 不做
 * 虚拟化也够（§4.6 的精神：最多保留 N 条 + 溢出滚动）。
 * 两列的行几何与高度约束链由 `EventRow.tsx` 的 `TL_ROW_CLASS` / `TL_COL_*` 共享。
 * 换 run（重新开始翻译）：左列滚动/浮标/展开行按 run 重置；右列游标是文档级
 * 持久游标，跨 run 连续累计，不重置。
 */
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';

import { describeApiError } from '../../lib/api';
import { cn } from '../../lib/cn';
import { EVENTS_WINDOW_SIZE, KIND_GROUPS, kindGroup, type KindGroup, type RunEvent } from '../../lib/events';
import {
  formatDuration,
  formatStageDuration,
  STAGE_LABELS,
  stageStatusLabel,
} from '../../lib/humanize';
import { activeJob } from '../../lib/jobs';
import { useJobs } from '../../lib/queries';
import type { SegmentState } from '../../lib/timeline';
import type { PersistentEvent } from '../../lib/usePersistentEvents';
import { Icon } from '../icons';
import { Button } from '../ui/Button';
import { Chip } from '../ui/Chip';
import { EventRow, LocateLink, TL_COL_BODY_CLASS, TL_COL_CLASS, TL_COL_SCROLL_CLASS, TL_ROW_CLASS, TlNode, TlTime, type TlMark } from './EventRow';
import type { EventFeed } from './useEventWindow';
import type { SseStatus } from './useEventStream';
import { useTimelineStages, type TimelineData } from './useTimelineStages';

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

/**
 * 阶段条每段的填充色（§1 令牌，不新增颜色）：ok=pass / err=err / live=run+脉冲 / not_run=hair-2。
 * 段宽均分——**不**按耗时比例造假（真实耗时的比例尺在底部时间线里）。
 */
const SEGMENT_FILL: Record<SegmentState, string> = {
  ok: 'bg-pass',
  err: 'bg-err',
  live: 'bg-run pulse-dot',
  not_run: 'bg-hair-2',
};

/** 段 title 的耗时文案：live 用秒表（刚起步 → 「刚启动」），其余用实测耗时（拿不到 → 「—」）。 */
function segmentDurationText(state: SegmentState, durationS: number | null, elapsedS: number | null): string {
  return state === 'live' ? formatDuration(elapsedS) : formatStageDuration(durationS);
}

/**
 * 单行阶段条（§2.7 头部）：左 7 段状态色，右**动态状态**。
 * 非 live 的结论按「失败 > 全部完成 > 未在运行」定序（失败优先，全 ok 才说完成）。
 */
function StageStrip({ timeline }: { timeline: TimelineData }) {
  if (timeline.isError) {
    return (
      <p
        data-od-id="stage-strip"
        className="flex h-7 flex-none items-center border-b border-hair px-s3 font-mono text-micro text-ink-4"
      >
        阶段状态不可用
      </p>
    );
  }
  const live = timeline.segments.find((segment) => segment.state === 'live') ?? null;
  const failed = timeline.segments.find((segment) => segment.state === 'err') ?? null;
  const allOk = timeline.segments.every((segment) => segment.state === 'ok');

  let tone = 'text-ink-4';
  let status = '未在运行';
  if (live !== null) {
    tone = 'text-run-ink';
    // 拿不到起点（stage-state 没有 started_at）就不编造秒数
    status = live.elapsedS === null
      ? `${STAGE_LABELS[live.stage]}中`
      : `${STAGE_LABELS[live.stage]}中 · ${live.elapsedS}s`;
  } else if (failed !== null) {
    tone = 'text-err-ink';
    status = `${STAGE_LABELS[failed.stage]}失败`;
  } else if (allOk) {
    tone = 'text-pass-ink';
    status = `${timeline.segments.length} 阶段完成`;
  }

  return (
    <div
      data-od-id="stage-strip"
      className="flex h-7 flex-none items-center gap-s2 border-b border-hair px-s3"
    >
      <span className="flex min-w-0 flex-1 items-center gap-[2px]">
        {timeline.segments.map((segment) => (
          <span
            key={segment.stage}
            data-od-id="stage-strip-segment"
            data-stage={segment.stage}
            data-state={segment.state}
            title={`${STAGE_LABELS[segment.stage]} · ${stageStatusLabel(segment.status)} · ${segmentDurationText(
              segment.state,
              segment.durationS,
              segment.elapsedS,
            )}`}
            className={cn('h-[7px] min-w-[6px] flex-1 rounded-[1px]', SEGMENT_FILL[segment.state])}
          />
        ))}
      </span>
      <span
        data-od-id="stage-strip-status"
        data-live={live !== null}
        className={cn('flex flex-none items-center gap-[5px] font-mono text-micro', tone)}
      >
        {live === null ? null : (
          <span aria-hidden="true" className="pulse-dot h-[5px] w-[5px] flex-none rounded-full bg-current" />
        )}
        {status}
      </span>
    </div>
  );
}

/**
 * 编译侧事件类型 → 节点语气色（两列共用 `TlNode`：ok=成页 / err=预览失败 / idle=其余）。
 * 原来这里是自定义的 `border-*` 描边色，与左列 `bg-*` 填充色两套写法 → 统一成语义名。
 */
const COMPILE_MARK: Record<'ok' | 'err' | 'info', TlMark> = {
  ok: 'ok',
  err: 'err',
  info: 'idle',
};

/** 编译侧事件类型 → 类型 chip（§2.7 `.tl-type`）。 */
const COMPILE_LABEL: Record<string, string> = {
  translation_block_completed: '译文提交',
  preview_ready: '成页',
  preview_failed: '预览失败',
  block_not_replaced: '保留原文',
  compile_float: '贴片浮动',
};

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
    case 'block_not_replaced':
      // 按设计不替换（标题/单行）：原文就是正确结果，不是错误。
      return { text: `${block ?? '?'} 保留原文`, tone: 'info' };
    case 'compile_float':
      return { text: `${block ?? '?'} 贴片浮动 → 第 ${page ?? '?'} 页`, tone: 'info' };
    default:
      return { text: event.type, tone: 'info' };
  }
}

/** 编译侧事件的定位目标：只有真数字 `page` 才算（没有就不渲染链接，不猜）。 */
function compileLocate(event: PersistentEvent): { page: number; paragraphId: string | null } | null {
  const data = event.data ?? {};
  const page = typeof data.page === 'number' ? data.page : event.page;
  if (typeof page !== 'number' || !Number.isFinite(page)) return null;
  const paragraph = data.paragraph_id;
  return {
    page,
    paragraphId: typeof paragraph === 'string' && paragraph !== '' ? paragraph : null,
  };
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
    <div
      className={cn(TL_COL_CLASS, 'tl-col-compile border-l border-hair')}
      data-od-id="compile-timeline"
    >
      <div className="flex flex-none items-center gap-s2 border-b border-hair px-s2 py-[6px]">
        <Icon name="archive" className="h-[13px] w-[13px] text-ink-4" />
        <span className="font-serif text-sm font-medium leading-[1.35] text-ink-2">编译</span>
        <span className="ml-auto flex-none font-mono text-micro text-ink-4 [font-variant-numeric:tabular-nums]">
          块 {blocksDone} · 页 {pagesReady}
        </span>
      </div>
      <div
        ref={scrollRef}
        onScroll={onScroll}
        data-od-id="compile-timeline-scroll"
        className={TL_COL_SCROLL_CLASS}
      >
        {rows.length === 0 ? (
          <p className="px-s2 py-s2 text-micro text-ink-4" data-od-id="compile-timeline-empty">
            还没有编译/成页事件（翻译提交后这里逐块出现）
          </p>
        ) : (
          <ul className="flex flex-col pt-[6px]">
            {rows.map((event) => {
              const line = compileLine(event);
              const locate = compileLocate(event);
              return (
                <li
                  key={event.seq}
                  data-od-id="compile-timeline-row"
                  data-type={event.type}
                  title={event.at ?? undefined}
                  className={cn(
                    TL_ROW_CLASS,
                    line.tone === 'ok' ? 'text-run-ink' : line.tone === 'err' ? 'text-err-ink' : 'text-ink-3',
                  )}
                >
                  <TlNode mark={COMPILE_MARK[line.tone]} />
                  <span className="flex min-w-0 items-center gap-s2">
                    <Chip
                      title={event.type}
                      tone={line.tone === 'ok' ? 'pass' : line.tone === 'err' ? 'err' : 'default'}
                      className="flex-none"
                    >
                      {COMPILE_LABEL[event.type] ?? event.type}
                    </Chip>
                    <span className="min-w-0 break-words">{line.text}</span>
                  </span>
                  {/* 第二行：时间戳 + 行尾 seq（seq 原来只有左列有，两列口径统一）。 */}
                  <span className="mt-[2px] flex min-w-0 items-center gap-s2 text-ink-4">
                    <TlTime at={event.at} />
                    <span className="ml-auto flex-none [font-variant-numeric:tabular-nums]">{event.seq}</span>
                  </span>
                  {locate === null ? null : (
                    <LocateLink page={locate.page} paragraphId={locate.paragraphId} />
                  )}
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

  // 阶段条的数据：同一份 stage-state / jobs 查询（key 与 WorkbenchScreen 相同 → 共用缓存，
  // 数据由那边 2s 轮询刷新并传播到这里），只把新 run 的“job 驱动 live”接进来
  // （新 run 的 stage-state 还没落盘时，只有 job 记录说“这个阶段在跑”，口径见 lib/timeline.ts）。
  const jobs = useJobs(did, { refetchMs: 0 });
  const timeline = useTimelineStages(did, feed.events, { job: activeJob(jobs.data) });

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
      <ul className="flex flex-col pt-[6px]">
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
      <StageStrip timeline={timeline} />
      <div className="flex flex-none items-center gap-s2 border-b border-hair px-s3 py-[3px] font-mono text-micro text-ink-4">
        <span className="min-w-0 truncate" data-od-id="event-run-id" title={feed.runId ?? ''}>
          翻译 run {feed.runId ?? '—'}
        </span>
        <span className="ml-auto flex-none [font-variant-numeric:tabular-nums]">
          显示 {rows.length}/{feed.events.length} · 窗口 {EVENTS_WINDOW_SIZE} 条
        </span>
      </div>
      {/* 面板拖窄时（容器 < 320px）双列退回一列：容器查询见 globals.css 的 `.tl-two-col`。 */}
      <div style={{ containerType: 'inline-size' }} className="flex min-h-0 flex-1">
        <div className="tl-two-col grid min-h-0 min-w-0 flex-1 grid-cols-2 grid-rows-[minmax(0,1fr)]">
          <div className={TL_COL_CLASS}>
            <div className="flex flex-none items-center gap-s2 border-b border-hair px-s2 py-[6px]">
              <Icon name="translate" className="h-[13px] w-[13px] text-ink-4" />
              <span className="font-serif text-sm font-medium leading-[1.35] text-ink-2">翻译</span>
              <span className="ml-auto flex-none font-mono text-micro text-ink-4 [font-variant-numeric:tabular-nums]">
                {rows.length} 条
              </span>
            </div>
            <div className={TL_COL_BODY_CLASS}>
              <div
                ref={scrollRef}
                onScroll={onScroll}
                data-od-id="event-stream-scroll"
                className={TL_COL_SCROLL_CLASS}
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
          </div>
          <CompileTimeline events={compileEvents} />
        </div>
      </div>
      <p className="flex-none border-t border-hair px-s3 py-[3px] font-mono text-micro text-ink-4">
        {did} · 左：翻译 run 归档 · 右：编译/成页（文档级游标）
      </p>
    </section>
  );
}
