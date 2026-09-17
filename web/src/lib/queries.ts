/**
 * TanStack Query hooks：文件库列表、文档详情、产物清单、bbox 几何，以及 W06 的进度层
 * （事件分页 / 首拉尾部窗口 / 阶段状态）。
 *
 * geometry 的 404（`snapshot_unavailable` / `geometry_unavailable`）不是错误：产物缺失
 * 时该页照样能预览，只是没有 bbox，所以 query 归一成 `data = null` 由 UI 显示小条。
 * 事件分页的 404（`events_unavailable`）也不是“没事件”——是“没有 run 归档”，由
 * `useEventWindow` 归一成空态（`hasArchive=false`），不建 SSE。
 */
import { useQuery } from '@tanstack/react-query';

import type {
  ArtifactItem,
  DocumentDetail,
  DocumentListItem,
  EventsPage,
  GeometryResponse,
  StageStateResponse,
} from '../api/types';
import { ApiError, apiGet } from './api';
import {
  EVENTS_PAGE_LIMIT,
  EVENTS_TAIL_MAX_PAGES,
  EVENTS_WINDOW_SIZE,
  mergeTailWindow,
  runEventsFromPage,
  type RunEvent,
} from './events';
import { hasRunningDocument } from './humanize';
import type { BboxMode } from './preview';

/** 列表/详情的自动刷新间隔（只在真有 running 阶段时才开）。 */
export const DOCUMENTS_LIVE_REFETCH_MS = 3_000;
export const DOCUMENT_LIVE_REFETCH_MS = 2_000;

/** `/events` 分页默认条数（api.md §1.3 的默认值）。 */
export const EVENTS_DEFAULT_LIMIT = 500;

export const queryKeys = {
  documents: ['documents'] as const,
  document: (did: string) => ['documents', did] as const,
  artifacts: (did: string) => ['documents', did, 'artifacts'] as const,
  geometry: (did: string, kind: BboxMode, page: number) =>
    ['documents', did, 'geometry', kind, page] as const,
  stageState: (did: string) => ['documents', did, 'stage-state'] as const,
  eventTail: (did: string) => ['documents', did, 'events', 'tail'] as const,
  events: (did: string, afterSeq: number, limit: number, kind: string, stage: string) =>
    ['documents', did, 'events', afterSeq, limit, kind, stage] as const,
};

/**
 * 文件库列表。有任一文档真在跑时按 3s 轮询（brief 的列表刷新口径），否则不轮询；
 * 判据是 `stage_summary`（拿不到事件流的页面只能用这个）。
 */
export function useDocuments() {
  return useQuery({
    queryKey: queryKeys.documents,
    queryFn: () => apiGet<DocumentListItem[]>('/documents'),
    staleTime: 10_000,
    refetchInterval: (query) =>
      hasRunningDocument(query.state.data ?? []) ? DOCUMENTS_LIVE_REFETCH_MS : false,
  });
}

/**
 * 文档详情。`refetchMs > 0` 时按它轮询（进度视图在时间线出现 live 段时给 2s）；
 * 默认不轮询（列表页/预览区等只读场景）。
 */
export function useDocument(did: string | null, options: { refetchMs?: number } = {}) {
  const refetchMs = options.refetchMs ?? 0;
  return useQuery({
    queryKey: queryKeys.document(did ?? ''),
    queryFn: () => apiGet<DocumentDetail>(`/documents/${encodeURIComponent(did ?? '')}`),
    staleTime: 5_000,
    refetchInterval: refetchMs > 0 ? refetchMs : false,
    enabled: did !== null && did !== '',
  });
}

/** 产物清单（`GET /documents/{did}/artifacts`）；预览用它判断有没有可渲染的 PDF。 */
export function useArtifacts(did: string | null) {
  return useQuery({
    queryKey: queryKeys.artifacts(did ?? ''),
    queryFn: () => apiGet<ArtifactItem[]>(`/documents/${encodeURIComponent(did ?? '')}/artifacts`),
    staleTime: 60_000,
    enabled: did !== null && did !== '',
  });
}

/**
 * 该页 bbox 几何。`kind = null` 时（bbox 图层关掉）不发请求；404 归一成 `null`：
 * `isSuccess && data === null` ⇔ 服务端明确说“这一页/这类产物不可用”。
 */
export function useGeometry(
  did: string | null,
  kind: Exclude<BboxMode, 'off'> | null,
  page: number,
) {
  return useQuery({
    queryKey: queryKeys.geometry(did ?? '', kind ?? 'off', page),
    queryFn: async () => {
      const path = `/documents/${encodeURIComponent(did ?? '')}/geometry?kind=${kind}&page=${page}`;
      try {
        return await apiGet<GeometryResponse>(path);
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) return null;
        throw error;
      }
    },
    staleTime: 60_000,
    enabled: did !== null && did !== '' && kind !== null && page > 0,
  });
}

/** 事件分页地址（api.md §1.3）：`after_seq` 是**扫描位置**，过滤只影响本页内容。 */
export function eventsPagePath(
  did: string,
  query: { afterSeq?: number; limit?: number; kind?: string; stage?: string; runId?: string } = {},
): string {
  const params = new URLSearchParams({
    after_seq: String(Math.max(0, Math.floor(query.afterSeq ?? 0))),
    limit: String(query.limit ?? EVENTS_DEFAULT_LIMIT),
  });
  if (query.kind !== undefined && query.kind !== '') params.set('kind', query.kind);
  if (query.stage !== undefined && query.stage !== '') params.set('stage', query.stage);
  if (query.runId !== undefined && query.runId !== '') params.set('run_id', query.runId);
  return `/documents/${encodeURIComponent(did)}/events?${params.toString()}`;
}

export function fetchEventsPage(
  did: string,
  query: { afterSeq?: number; limit?: number; kind?: string; stage?: string; runId?: string } = {},
): Promise<EventsPage> {
  return apiGet<EventsPage>(eventsPagePath(did, query));
}

export interface EventTail {
  runId: string;
  /** 尾部窗口（升序，最旧 → 最新；默认最后 200 条）。 */
  events: RunEvent[];
  /** SSE 起点：整个 run 扫过的最大 `seq`（不是窗口内最大的）。 */
  nextAfterSeq: number;
  /** 扫过的总条数（用于报告/日志，不是窗口长度）。 */
  scanned: number;
  /** 是否因 `EVENTS_TAIL_MAX_PAGES` 截断（截断时窗口不是真正的尾部）。 */
  truncated: boolean;
}

/**
 * 首拉尾部窗口。分页接口只能**正向**翻（只有 `after_seq`，没有 before/desc），所以拿尾部
 * 只能从 0 一路扫到 `has_more=false` —— 单 run ≤3758 条时 2 页（实测见报告）。
 */
export async function fetchEventTail(
  did: string,
  windowSize = EVENTS_WINDOW_SIZE,
): Promise<EventTail> {
  let afterSeq = 0;
  let runId = '';
  let scanned = 0;
  let truncated = false;
  let events: RunEvent[] = [];
  for (let page = 0; page < EVENTS_TAIL_MAX_PAGES; page += 1) {
    const response = await fetchEventsPage(did, { afterSeq, limit: EVENTS_PAGE_LIMIT });
    runId = response.run_id;
    scanned += response.events.length;
    events = mergeTailWindow(events, runEventsFromPage(response), Number.MAX_SAFE_INTEGER);
    const next = Math.max(afterSeq, response.next_after_seq);
    if (!response.has_more || next <= afterSeq || response.events.length === 0) {
      afterSeq = next;
      break;
    }
    afterSeq = next;
    if (page === EVENTS_TAIL_MAX_PAGES - 1) truncated = true;
  }
  const window = events.length > windowSize ? events.slice(events.length - windowSize) : events;
  return { runId, events: window, nextAfterSeq: afterSeq, scanned, truncated };
}

/** 首拉尾部窗口（每个 did 一次；SSE 增量由 `useEventWindow` 叠在这上面）。 */
export function useEventsTail(did: string | null) {
  return useQuery({
    queryKey: queryKeys.eventTail(did ?? ''),
    queryFn: () => fetchEventTail(did ?? ''),
    staleTime: 30_000,
    enabled: did !== null && did !== '',
  });
}

export interface UseEventsOptions {
  afterSeq?: number;
  limit?: number;
  /** 服务端 kind 过滤（只影响本页内容，不动游标）。 */
  kind?: string;
  /** 服务端 stage 过滤（同上）。 */
  stage?: string;
  /** manual 模式：默认不自动拉；调用方把游标写好后置 true（「载入更早」用）。 */
  enabled?: boolean;
}

/** 事件分页（默认 manual：`enabled=false`，由「载入更早」的游标驱动）。 */
export function useEvents(did: string | null, options: UseEventsOptions = {}) {
  const afterSeq = options.afterSeq ?? 0;
  const limit = options.limit ?? EVENTS_DEFAULT_LIMIT;
  const kind = options.kind ?? '';
  const stage = options.stage ?? '';
  return useQuery({
    queryKey: queryKeys.events(did ?? '', afterSeq, limit, kind, stage),
    queryFn: () => fetchEventsPage(did ?? '', { afterSeq, limit, kind, stage }),
    staleTime: 0,
    enabled: (options.enabled ?? false) && did !== null && did !== '',
  });
}

/** 7 阶段状态与真实耗时（时间线基线）。`refetchMs > 0` 时按它轮询（live 时 2s）。 */
export function useStageState(did: string | null, refetchMs = 0) {
  return useQuery({
    queryKey: queryKeys.stageState(did ?? ''),
    queryFn: () =>
      apiGet<StageStateResponse>(`/documents/${encodeURIComponent(did ?? '')}/stage-state`),
    staleTime: 5_000,
    refetchInterval: refetchMs > 0 ? refetchMs : false,
    enabled: did !== null && did !== '',
  });
}
