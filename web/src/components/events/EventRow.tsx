/**
 * 单条事件**节点**（§4.6 + 设计稿 §2.7 `.tl-node`）：`[级别圆标] [类型 chip][一行叙述]`，
 * 第二行 `[北京时间] [展开详情] [seq]`；展开后追加原始 JSON；事件里带 `page` 时给「在预览中定位」。
 * 等宽字体 + `tabular-nums`（§2.1：事件流一律不用衬线）。
 *
 * 行几何（`TL_ROW_CLASS` / `TL_COL_*` / `TlNode` / `TlTime`）也在这里导出：翻译列与
 * 编译列的**行结构、节点圆标、时间戳与高度约束链**都走同一套拼写，避免两边各写一份
 * 再次漂移（漂移一次就出现「左列没滚动条」「右列逐行横线」这类差异）。
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
 * 时间线**行几何**（§2.7，两列共用）：紧凑等宽行 + 一条**稀疏**竖向流水线。
 *
 * 横线口径按翻译列（不逐行画横线，只留一条连接线）——编译列原来每行都有
 * `border-b`，视觉密度明显高于左列；两列统一成同一份 class 就不会再分叉。
 * 竖线从节点上沿一直拉到行底（`::before` 画在子节点之下），下一行接着画，
 * 所以线是连续的；节点用实心圆盖住线，只有 `:last-child` 不画线。
 */
export const TL_ROW_CLASS = cn(
  'relative py-[3px] pl-[18px] pr-s2 font-mono text-micro leading-[1.5]',
  'before:absolute before:bottom-0 before:left-[3px] before:top-[3px] before:w-px before:bg-hair before:content-[""]',
  'last:before:hidden',
);

/**
 * 时间线**列**几何（§2.7，两列共用）：头部固定 + 滚动区吃掉剩余高度。
 *
 * 高度约束链必须整条写在一处：列（`min-h-0 flex-1`）→ 滚动区包裹（`min-h-0 flex-1`）
 * → 滚动体（`h-full overflow-auto`）。两列各写一份时左列曾漏掉其中一环，内容长到
 * 内容高度、滚动条永远不出现（`clientHeight === scrollHeight`）。
 * 滚动区包裹层带 `relative`：左列的「↑ N 条新事件」浮标挂在它上面。
 */
export const TL_COL_CLASS = 'flex min-h-0 min-w-0 flex-1 flex-col';
export const TL_COL_BODY_CLASS = 'relative flex min-h-0 min-w-0 flex-1 flex-col';
export const TL_COL_SCROLL_CLASS = 'min-h-0 flex-1 overflow-auto';

/** 节点语气：两列共用一套词（翻译列按事件级别、编译列按事件类型映射）。 */
export type TlMark = 'idle' | 'ok' | 'warn' | 'err';

/** 节点圆标颜色（§1 令牌，不新增颜色）：级别只改颜色，不换形状。 */
const TL_MARK: Record<TlMark, string> = {
  idle: 'bg-hair-2',
  ok: 'bg-pass',
  warn: 'bg-run',
  err: 'bg-err',
};

/** 事件级别（翻译列）→ 节点语气。 */
export const EVENT_MARK: Record<EventLevel, TlMark> = {
  info: 'idle',
  warn: 'warn',
  err: 'err',
};

/**
 * 行首节点圆标（§4.6）：**8px 实心小点**（旧版 20px 描边圆太大，两列还各写一份
 * 16px/20px）。只改颜色区分级别，两列尺寸与位置完全一致。
 */
export function TlNode({ mark, className }: { mark: TlMark; className?: string }) {
  return (
    <span
      aria-hidden="true"
      data-od-id="tl-node"
      data-mark={mark}
      className={cn('absolute left-0 top-[3px] h-2 w-2 rounded-full', TL_MARK[mark], className)}
    />
  );
}

/**
 * 行内时间戳（两列共用）：北京时间 `HH:MM:SS`，完整时刻（含 UTC 原文）放 `title`。
 * 两列都显示同一口径——编译列的持久事件 `at` 可能为空，那时不渲染（不显示 `—` 噪声）。
 */
export function TlTime({ at }: { at: string | null }) {
  if (at === null || at.trim() === '') return null;
  return (
    <span
      data-od-id="tl-time"
      className="flex-none font-mono text-micro text-ink-4 [font-variant-numeric:tabular-nums]"
      title={`${at}（UTC）· 北京时间 ${eventTime(at)}`}
    >
      {eventTime(at)}
    </span>
  );
}

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
    // 节点与连接线走共用行几何（`TL_ROW_CLASS` + `TlNode`）。
    <li
      data-od-id="event-row"
      data-seq={event.seq}
      data-kind-group={group}
      className={TL_ROW_CLASS}
    >
      <TlNode mark={EVENT_MARK[eventLevel(event)]} />
      <button
        type="button"
        aria-expanded={expanded}
        onClick={() => onToggle(event.seq)}
        className={cn(
          'block w-full min-w-0 rounded-[2px] text-left transition-colors hover:bg-sand',
          expanded && 'bg-sand',
        )}
      >
        {/* 两列同一行结构：第 1 行 = chip + 一行文案；第 2 行 = 时间戳 + 交互 + 行尾 seq。
            （旧版左列把时间/seq 放 chip 行、右列把文案放 chip 行，同一份信息在两列位置不同。） */}
        <span className="flex min-w-0 items-start gap-s2">
          <Chip title={event.kind} className={cn('flex-none', GROUP_TEXT[group])}>
            {kindLabel(event.kind)}
          </Chip>
          <span className="min-w-0 flex-1 break-words text-ink-3">
            {event.stage === '' ? summary : `${stageLabel(event.stage)} · ${summary}`}
          </span>
        </span>
        <span className="mt-[2px] flex min-w-0 items-center gap-s2 text-ink-4">
          <TlTime at={event.at} />
          <span className="flex-none">{expanded ? '收起详情' : '展开详情'}</span>
          <span className="ml-auto flex-none [font-variant-numeric:tabular-nums]">{event.seq}</span>
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
