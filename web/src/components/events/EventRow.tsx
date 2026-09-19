/**
 * 单条事件行（§4.6）：`[seq][时间戳][阶段 chip][级别圆点] 人话叙述`，点击整行展开原始 JSON。
 * 等宽字体 + `tabular-nums`（§2.1：事件流一律不用衬线）。
 * 时间戳按**北京时间**显示（`HH:MM:SS`），完整 ISO 放 `title`。
 * 归档里的 `at` 是 UTC（`+00:00`），API 契约仍只发 UTC；这里是纯展示层换算。
 */
import { cn } from '../../lib/cn';
import { dataSummary, eventLevel, kindGroup, type EventLevel, type KindGroup, type RunEvent } from '../../lib/events';
import { kindLabel, stageLabel } from '../../lib/humanize';
import { Chip } from '../ui/Chip';

/** kind 分组的文字色（§1 令牌，不新增颜色）。 */
const GROUP_TEXT: Record<KindGroup, string> = {
  stage: 'text-ink-2',
  call: 'text-ink-3',
  cache: 'text-pass-ink',
  candidate: 'text-run-ink',
  compile: 'text-ink-2',
  other: 'text-ink-4',
};

/** §4.6 级别圆点：info = hair-2 / warn = run / err = err（run 级别的脉冲只给面板头部的「实时」）。 */
const LEVEL_DOT: Record<EventLevel, string> = {
  info: 'bg-hair-2',
  warn: 'bg-run',
  err: 'bg-err',
};

/**
 * 事件时刻 → 北京时间 `HH:MM:SS`。
 *
 * 归档时刻是带时区的 UTC ISO（`2026-09-19T09:16:25.710+00:00`），所以这里显式按
 * `Asia/Shanghai` 换算，**不跟随浏览器本地时区**（看板是给人核对进度的，口径要固定）。
 * 无时区的裸串按 UTC 解释（`job_events.created_at` 是 SQLite `CURRENT_TIMESTAMP`）；
 * 解析失败 → `—`。
 */
const BEIJING_TIME = new Intl.DateTimeFormat('zh-CN', {
  timeZone: 'Asia/Shanghai',
  hour12: false,
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
});

export function eventTime(at: string, region: 'utc' | 'beijing' = 'beijing'): string {
  const text = at.trim();
  if (text === '') return '—';
  // 带时区偏移/`Z` → 本身就是绝对时刻；裸串（无时区）按 UTC 补上，避免被当成浏览器本地时间。
  const absolute = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(text) ? text : `${text}Z`;
  const parsed = new Date(absolute);
  if (Number.isNaN(parsed.getTime())) return '—';
  if (region === 'utc') {
    const match = /(\d{2}:\d{2}:\d{2})/.exec(text);
    return match === null ? '—' : match[1];
  }
  return BEIJING_TIME.format(parsed);
}

export function EventRow({
  event,
  expanded,
  onToggle,
}: {
  event: RunEvent;
  expanded: boolean;
  onToggle: (seq: number) => void;
}) {
  const group = kindGroup(event.kind);
  return (
    <li data-od-id="event-row" data-seq={event.seq} data-kind-group={group}>
      <button
        type="button"
        aria-expanded={expanded}
        onClick={() => onToggle(event.seq)}
        className={cn(
          'grid w-full min-w-0 grid-cols-[40px_54px_auto_5px_minmax(0,1fr)] items-start gap-s2 px-s3 py-[3px] text-left transition-colors hover:bg-sand',
          expanded && 'bg-sand',
        )}
      >
        <span className="text-right font-mono text-micro text-ink-4 [font-variant-numeric:tabular-nums]">
          {event.seq}
        </span>
        <span
          className="font-mono text-micro text-ink-4 [font-variant-numeric:tabular-nums]"
          title={`${event.at}（UTC）· 北京时间 ${eventTime(event.at)}`}
        >
          {eventTime(event.at)}
        </span>
        <span className="flex-none">
          <Chip title={event.stage === '' ? '（没有阶段）' : event.stage}>
            {event.stage === '' ? '—' : stageLabel(event.stage)}
          </Chip>
        </span>
        <span
          aria-hidden="true"
          className={cn('mt-[7px] h-[5px] w-[5px] flex-none rounded-full', LEVEL_DOT[eventLevel(event)])}
        />
        <span className="min-w-0">
          <span className="flex min-w-0 items-baseline gap-[6px]">
            <span className={cn('flex-none font-mono text-micro', GROUP_TEXT[group])} title={event.kind}>
              {kindLabel(event.kind)}
            </span>
            <span className="min-w-0 truncate font-mono text-micro text-ink-3">
              {dataSummary(event.data)}
            </span>
          </span>
        </span>
        {expanded ? (
          <pre
            data-od-id="event-row-json"
            className="col-span-full mt-s1 max-h-[220px] overflow-auto rounded-[3px] border border-hair bg-parchment p-s2 font-mono text-micro leading-[1.5] text-ink-2"
          >
            {JSON.stringify(event, null, 2)}
          </pre>
        ) : null}
      </button>
    </li>
  );
}
