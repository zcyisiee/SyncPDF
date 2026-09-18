/**
 * 单条事件行（§4.6）：`[seq][时间戳][阶段 chip][级别圆点] 人话叙述`，点击整行展开原始 JSON。
 * 等宽字体 + `tabular-nums`（§2.1：事件流一律不用衬线）。
 * 时间戳显示归档里的 UTC 时刻（`HH:MM:SS`），完整 ISO 放 `title`（api.md §1：时间一律 UTC）。
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

/** 归档时刻 → `HH:MM:SS`（取 ISO 里的时间部分，不做时区换算）。 */
export function eventTime(at: string): string {
  const match = /(\d{2}:\d{2}:\d{2})/.exec(at);
  return match === null ? '—' : match[1];
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
          title={event.at}
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
