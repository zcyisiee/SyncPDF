/**
 * 单条事件**节点**（§4.6 + 设计稿 §2.7 `.tl-node`）：`[级别圆标] [类型 chip][北京时间][seq]`、
 * 一行叙述（`阶段 · data 摘要`）、「展开详情」切换原始 JSON；事件里带 `page` 时给「在预览中定位」。
 * 等宽字体 + `tabular-nums`（§2.1：事件流一律不用衬线）。
 * 时间戳按**北京时间**显示（`HH:MM:SS`），完整 ISO 放 `title`。
 * 归档里的 `at` 是 UTC（`+00:00`），API 契约仍只发 UTC；这里是纯展示层换算。
 */
import { cn } from '../../lib/cn';
import { dataSummary, eventLevel, kindGroup, type EventLevel, type KindGroup, type RunEvent } from '../../lib/events';
import { kindLabel, stageLabel } from '../../lib/humanize';
import { useUiStore } from '../../stores/ui';
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

/**
 * §4.6 节点圆标（20px 描边圆，替换旧版的行首小圆点）：
 * info = hair-2 描边 / warn = run 描边 / err = err 填充。级别只改颜色，不换形状。
 */
const LEVEL_MARK: Record<EventLevel, string> = {
  info: 'border-hair-2',
  warn: 'border-run',
  err: 'border-err bg-err',
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

/**
 * 事件 data 里的定位目标：`page` 是数字才算（有的 kind 没有页概念）。
 * 段落 id 有就给（没有不猜：不拿 `index`/`seq` 冒充段落 id）。
 */
export function eventLocateTarget(
  data: Record<string, unknown>,
): { page: number; paragraphId: string | null } | null {
  const page = data.page;
  if (typeof page !== 'number' || !Number.isFinite(page)) return null;
  const paragraph = data.paragraph_id;
  return { page, paragraphId: typeof paragraph === 'string' && paragraph !== '' ? paragraph : null };
}

/**
 * 「在预览中定位」（带 `page` 的事件节点）：经 ui store 的定位桥让预览滚到那一页，
 * 段落 id 有就一并选中。翻译列与编译列共用（同一个控件、同一个 `data-od-id`）。
 */
export function LocateLink({ page, paragraphId }: { page: number; paragraphId: string | null }) {
  const locateInPreview = useUiStore((state) => state.locateInPreview);
  return (
    <button
      type="button"
      data-od-id="event-locate"
      title={`在预览中定位第 ${page} 页${paragraphId === null ? '' : `（${paragraphId}）`}`}
      onClick={() => locateInPreview(page, paragraphId ?? undefined)}
      className="mt-[5px] block font-mono text-micro text-ink-4 underline-offset-2 hover:text-accent hover:underline"
    >
      在预览中定位
    </button>
  );
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
  const locate = eventLocateTarget(event.data);
  const summary = dataSummary(event.data);
  return (
    // 节点底纹/连接线走 CSS：`before:` 画竖向流水线（最后一个节点不画）。
    <li
      data-od-id="event-row"
      data-seq={event.seq}
      data-kind-group={group}
      className={cn(
        'relative pb-s3 pl-[26px] pr-s2 pt-[2px] last:pb-[4px]',
        'before:absolute before:bottom-0 before:left-[10px] before:top-[22px] before:w-px before:bg-hair before:content-[""]',
        'last:before:hidden',
      )}
    >
      <span
        aria-hidden="true"
        className={cn(
          'absolute left-0 top-[2px] h-5 w-5 rounded-full border bg-ivory',
          LEVEL_MARK[eventLevel(event)],
        )}
      />
      <button
        type="button"
        aria-expanded={expanded}
        onClick={() => onToggle(event.seq)}
        className={cn(
          'block w-full min-w-0 rounded-[2px] text-left transition-colors hover:bg-sand',
          expanded && 'bg-sand',
        )}
      >
        <span className="flex min-w-0 items-center gap-s2">
          <Chip title={event.kind} className={GROUP_TEXT[group]}>
            {kindLabel(event.kind)}
          </Chip>
          <span
            className="flex-none font-mono text-micro text-ink-4 [font-variant-numeric:tabular-nums]"
            title={`${event.at}（UTC）· 北京时间 ${eventTime(event.at)}`}
          >
            {eventTime(event.at)}
          </span>
          <span className="ml-auto flex-none font-mono text-micro text-ink-4 [font-variant-numeric:tabular-nums]">
            {event.seq}
          </span>
        </span>
        <span className="mt-[3px] block min-w-0 break-words font-mono text-micro text-ink-3">
          {event.stage === '' ? summary : `${stageLabel(event.stage)} · ${summary}`}
        </span>
        <span className="mt-[5px] block font-mono text-micro text-ink-4">
          {expanded ? '收起详情' : '展开详情'}
        </span>
      </button>
      {locate === null ? null : <LocateLink page={locate.page} paragraphId={locate.paragraphId} />}
      {expanded ? (
        <pre
          data-od-id="event-row-json"
          className="mt-s1 max-h-[220px] overflow-auto rounded-[3px] border border-hair bg-parchment p-s2 font-mono text-micro leading-[1.5] text-ink-2"
        >
          {JSON.stringify(event, null, 2)}
        </pre>
      ) : null}
    </li>
  );
}
