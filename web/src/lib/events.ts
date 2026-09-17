/**
 * 事件流的纯逻辑层（无 React、无网络）：事件形状归一化、SSE 帧解析、live 判定、
 * kind 分组、尾部窗口合并。网络层在 `queries.ts`（分页）与
 * `components/events/useEventStream.ts`（原生 EventSource）。
 *
 * 契约：`docs/frontend/api.md` §1.3（分页游标是**扫描位置**）/ §1.4（SSE 帧
 * `event: <kind>` + `id: <run_id>:<seq>` + `data: <事件 JSON>`）。单 run 内 `seq`
 * 从 1 递增，所以窗口/去重都按 `seq` 做（跨 run 才需要 `(run_id, seq)`）。
 */
import type { EventsPage } from '../api/types';
import { API_BASE } from './api';
import { FINAL_STAGE } from './humanize';

/** 单条事件（`{seq, at, stage, kind, data}`）；`data` 原样保留（只做摘要显示）。 */
export interface RunEvent {
  seq: number;
  at: string;
  stage: string;
  kind: string;
  data: Record<string, unknown>;
}

/** 事件面板默认保留的尾部条数（§4.6：最多保留 N 条 + 溢出滚动）。 */
export const EVENTS_WINDOW_SIZE = 200;

/** 「载入更早」一次并入的条数（brief：每次 500）。 */
export const EVENTS_EARLIER_PAGE = 500;

/** 服务端分页上限（api.md §1.3：默认 500，上限 2000）。 */
export const EVENTS_PAGE_LIMIT = 2000;

/**
 * 首拉尾部窗口最多翻几页。正向翻页是分页接口唯一的读法（`after_seq` 只能往前走），
 * 所以要拿"尾部"必须从 0 一路扫到尾：单 run ≤ 3758 条时 2 页足够。
 * 超过上限（>16000 条）时窗口不是真正的尾部——这是本实现的已知边界（见报告）。
 */
export const EVENTS_TAIL_MAX_PAGES = 8;

/** `undefined` / 坏行 → null：归档里的半行与不合法形状一律跳过，不当成事件。 */
export function toRunEvent(raw: unknown): RunEvent | null {
  if (typeof raw !== 'object' || raw === null) return null;
  const event = raw as Record<string, unknown>;
  if (typeof event.seq !== 'number' || !Number.isInteger(event.seq)) return null;
  const data = event.data;
  return {
    seq: event.seq,
    at: typeof event.at === 'string' ? event.at : '',
    stage: typeof event.stage === 'string' ? event.stage : '',
    kind: typeof event.kind === 'string' ? event.kind : '',
    data:
      typeof data === 'object' && data !== null && !Array.isArray(data)
        ? (data as Record<string, unknown>)
        : {},
  };
}

/** 分页响应的事件数组 → 归一化事件（保持服务器给的顺序）。 */
export function runEventsFromPage(page: EventsPage): RunEvent[] {
  return page.events.map(toRunEvent).filter((event): event is RunEvent => event !== null);
}

/** SSE `data:` 行（事件 JSON）→ RunEvent；坏 JSON / 形状不对 → null（丢这一条，不炸流）。 */
export function parseStreamEvent(payload: string): RunEvent | null {
  try {
    return toRunEvent(JSON.parse(payload));
  } catch {
    return null;
  }
}

export interface SseId {
  runId: string;
  seq: number;
}

/** SSE `id: <run_id>:<seq>` → 结构化；不合法 → null（`Last-Event-ID` 同形状）。 */
export function parseSseId(id: string | null | undefined): SseId | null {
  if (typeof id !== 'string') return null;
  const separator = id.lastIndexOf(':');
  if (separator <= 0) return null;
  const runId = id.slice(0, separator);
  const rawSeq = id.slice(separator + 1);
  if (runId === '' || rawSeq === '') return null;
  const seq = Number(rawSeq);
  if (!Number.isInteger(seq) || seq < 0) return null;
  return { runId, seq };
}

/**
 * SSE 订阅地址：**相对路径**（同源，生产由 serve 托管、dev 由 Vite 代理 `/api` 转发），
 * 固定带 `run_id`（换 run 必须换它，否则续传游标会指向另一个 run）。
 */
export function eventSourceUrl(did: string, runId: string, afterSeq: number): string {
  const after = Number.isFinite(afterSeq) ? Math.max(0, Math.floor(afterSeq)) : 0;
  const params = new URLSearchParams({ run_id: runId, after_seq: String(after) });
  return `${API_BASE}/documents/${encodeURIComponent(did)}/events/stream?${params.toString()}`;
}

/**
 * 事件流层面「这个 run 还在跑」的判定（brief 冻结规则）：
 * **最后一条事件的 kind 不是 `stage_finished`，或它的 stage 不是最后一个阶段（`report`）→ live。**
 *
 * 输入必须是**升序**（最旧 → 最新）的事件数组。局限（必须知道）：
 * - 事件归档被截断的 run（legacy replay 只覆盖到 `check`，而 `run_state` 里 `review`/`report`
 *   有真实耗时）会被判成 live —— 所以调用方一律让 stage-state 基线优先
 *   （见 `lib/timeline.ts`：live 段只在基线没有定论的阶段上成立）；
 * - 失败/中断的 run（末条是 `stage_error`）同样算出 live；
 * - 空数组 → false（没有事件不等于在跑）。
 */
export function isRunLive(events: readonly RunEvent[]): boolean {
  const last = events[events.length - 1];
  if (last === undefined) return false;
  return last.kind !== 'stage_finished' || last.stage !== FINAL_STAGE;
}

/** 事件分组（过滤下拉用；只影响显示，不动游标）。 */
export type KindGroup = 'stage' | 'call' | 'cache' | 'candidate' | 'compile' | 'other';

const CALL_KINDS = new Set(['call_started', 'call_finished', 'provider_artifacts']);
const CACHE_KINDS = new Set(['cache_hit', 'cache_miss', 'cache_write', 'cache_bypass']);
const CANDIDATE_KINDS = new Set([
  'candidate_evaluated',
  'candidate_selected',
  'canonical_writeback',
  'writeback_saved',
  'text_version',
]);

export function kindGroup(kind: string): KindGroup {
  if (kind === 'stage_started' || kind === 'stage_finished' || kind === 'stage_error') {
    return 'stage';
  }
  if (CALL_KINDS.has(kind)) return 'call';
  if (CACHE_KINDS.has(kind)) return 'cache';
  if (CANDIDATE_KINDS.has(kind)) return 'candidate';
  if (kind.startsWith('compile_') || kind === 'artifact_bundle') return 'compile';
  return 'other';
}

export const KIND_GROUPS: readonly { id: KindGroup | 'all'; label: string }[] = [
  { id: 'all', label: '全部' },
  { id: 'stage', label: '阶段' },
  { id: 'call', label: '调用' },
  { id: 'cache', label: '缓存' },
  { id: 'candidate', label: '候选' },
  { id: 'compile', label: '编译' },
  { id: 'other', label: '其他' },
];

/** 行首圆点的级别（§4.6）：`api.md` §4 说事件里没有 level，只能由 kind + data 推导。 */
export type EventLevel = 'info' | 'warn' | 'err';

const ERROR_STATUSES = new Set(['error', 'failed', 'failure']);
const WARN_STATUSES = new Set(['interrupted']);
const WARN_KINDS = new Set(['cache_miss', 'compile_fallback', 'anchor_repair', 'stage_error']);

export function eventLevel(event: RunEvent): EventLevel {
  const status = event.data.status;
  if (typeof status === 'string' && ERROR_STATUSES.has(status)) return 'err';
  if (typeof event.data.error_code === 'string' && event.data.error_code !== '') return 'err';
  const returncode = event.data.returncode;
  if (typeof returncode === 'number' && returncode !== 0) return 'err';
  if (event.kind === 'stage_error') return 'err';
  if (WARN_KINDS.has(event.kind)) return 'warn';
  if (typeof status === 'string' && WARN_STATUSES.has(status)) return 'warn';
  return 'info';
}

const SUMMARY_ENTRIES = 3;
const SUMMARY_MAX_LENGTH = 80;

function summaryValue(value: unknown): string {
  if (value === null) return 'null';
  if (typeof value === 'string') return value;
  if (typeof value === 'number' || typeof value === 'boolean') return String(value);
  if (Array.isArray(value)) return `[${value.length}]`;
  if (typeof value === 'object') return '{…}';
  return String(value);
}

/**
 * `data` 摘要：压平成最多 3 个 `键=值`，超过 80 字符截断加省略号（§4.6 行的叙述行）。
 * 原始 JSON 原样保留在内存里（点击行展开 `<pre>`）。
 */
export function dataSummary(data: Record<string, unknown>, maxLength = SUMMARY_MAX_LENGTH): string {
  const parts = Object.entries(data)
    .slice(0, SUMMARY_ENTRIES)
    .map(([key, value]) => `${key}=${summaryValue(value)}`);
  const text = parts.join(' ');
  if (text.length <= maxLength) return text;
  return `${text.slice(0, Math.max(0, maxLength - 1))}…`;
}

function sortedUnique(events: readonly RunEvent[]): RunEvent[] {
  const bySeq = new Map<number, RunEvent>();
  for (const event of events) bySeq.set(event.seq, event);
  return [...bySeq.values()].sort((left, right) => left.seq - right.seq);
}

/**
 * 尾部窗口合并：升序、按 `seq` 去重（断线重连会重发 `after_seq` 之后的事件）、
 * 只保留最新 `maxSize` 条。**没有新增时返回原引用**，避免无谓重渲染。
 */
export function mergeTailWindow(
  prev: readonly RunEvent[],
  incoming: readonly RunEvent[],
  maxSize: number,
): RunEvent[] {
  if (incoming.length === 0) return prev as RunEvent[];
  const merged = sortedUnique([...prev, ...incoming]);
  return merged.length > maxSize ? merged.slice(merged.length - maxSize) : merged;
}

/**
 * 「载入更早」合并：并入更早的事件并**放宽**上限（默认窗口 200 + 已载入的更早条数）。
 * 返回的引用不变时调用方不该改 cap（见 `useEventWindow` 的做法）。
 */
export function mergeEarlierWindow(
  prev: readonly RunEvent[],
  earlier: readonly RunEvent[],
  keepTail: number,
): RunEvent[] {
  if (earlier.length === 0) return prev as RunEvent[];
  return mergeTailWindow(prev, earlier, Math.max(keepTail, prev.length + earlier.length));
}
