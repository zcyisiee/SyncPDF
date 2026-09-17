/**
 * TanStack Query hooks：文件库列表、文档详情、产物清单、bbox 几何、W06 的进度层
 * （事件分页 / 首拉尾部窗口 / 阶段状态），W08 的上传与 job（提交/轮询/取消）、profiles，
 * 以及 W10 的草稿（读 + 乐观并发写入 + 手动编译）与段落面板数据。
 *
 * geometry 的 404（`snapshot_unavailable` / `geometry_unavailable`）不是错误：产物缺失
 * 时该页照样能预览，只是没有 bbox，所以 query 归一成 `data = null` 由 UI 显示小条。
 * 事件分页的 404（`events_unavailable`）也不是“没事件”——是“没有 run 归档”，由
 * `useEventWindow` 归一成空态（`hasArchive=false`），不建 SSE。
 */
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';

import type {
  ArtifactItem,
  DocumentDetail,
  DocumentListItem,
  DocumentUploaded,
  DraftPatchRequest,
  DraftResponse,
  EventsPage,
  GeometryResponse,
  JobAccepted,
  JobCreateRequest,
  JobRecord,
  ParagraphItem,
  ProfileListItem,
  StageStateResponse,
} from '../api/types';
import { ApiError, apiGet, apiPatch, apiPost, apiUpload } from './api';
import {
  EVENTS_PAGE_LIMIT,
  EVENTS_TAIL_MAX_PAGES,
  EVENTS_WINDOW_SIZE,
  mergeTailWindow,
  runEventsFromPage,
  type RunEvent,
} from './events';
import { hasRunningDocument } from './humanize';
import { jobsRefetchInterval } from './jobs';
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
  jobs: (did: string) => ['documents', did, 'jobs'] as const,
  profiles: ['profiles'] as const,
  draft: (did: string) => ['documents', did, 'draft'] as const,
  paragraphs: (did: string) => ['documents', did, 'paragraphs'] as const,
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
 * 默认不轮询（列表页/预览区等只读场景）。`refetchWhen` 是**条件轮询**：只有它为真才轮询
 * （W10 用它把「编译未落定」也纳入 2s 轮询，编译一落定就自动停）。
 */
export function useDocument(
  did: string | null,
  options: {
    refetchMs?: number;
    refetchWhen?: (doc: DocumentDetail | undefined) => boolean;
  } = {},
) {
  const refetchMs = options.refetchMs ?? 0;
  const refetchWhen = options.refetchWhen;
  return useQuery({
    queryKey: queryKeys.document(did ?? ''),
    queryFn: () => apiGet<DocumentDetail>(`/documents/${encodeURIComponent(did ?? '')}`),
    staleTime: 5_000,
    refetchInterval: (query) => {
      if (refetchMs <= 0) return false;
      if (refetchWhen !== undefined && !refetchWhen(query.state.data)) return false;
      return refetchMs;
    },
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

/**
 * 段落面板数据（原文/译文/排版 join）。404（`paragraphs_unavailable`）归一成 `null`：
 * 四份段落产物都不存在 ≠ 请求失败，面板显示「该文档还没有段落产物」。
 */
export function useParagraphs(did: string | null) {
  return useQuery({
    queryKey: queryKeys.paragraphs(did ?? ''),
    queryFn: async () => {
      try {
        return await apiGet<ParagraphItem[]>(
          `/documents/${encodeURIComponent(did ?? '')}/paragraphs`,
        );
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) return null;
        throw error;
      }
    },
    staleTime: 30_000,
    enabled: did !== null && did !== '',
  });
}

// --------------------------------------------------------------------------- #
// W10：草稿（api.md §3.3）
// --------------------------------------------------------------------------- #

/**
 * 当前草稿（`GET /documents/{did}/draft`）：从没写过 → `revision=0` + 空 `paragraphs`。
 * `staleTime` 5s：草稿是**乐观并发**资源，不做轮询——写入路径自己更新缓存
 * （`usePatchDraftMutation` 的 `setQueryData`），跨标签页冲突由 409 提示刷新。
 */
export function useDraft(did: string | null) {
  return useQuery({
    queryKey: queryKeys.draft(did ?? ''),
    queryFn: () =>
      apiGet<DraftResponse>(`/documents/${encodeURIComponent(did ?? '')}/draft`),
    staleTime: 5_000,
    enabled: did !== null && did !== '',
  });
}

export interface PatchDraftVariables {
  /** 客户端期望的当前 revision（乐观并发；不匹配 → 409 `revision_conflict`）。 */
  baseRevision: number;
  /** `{段落 id: {target?, layout?}}`；`null` = 删字段、整段 `null` = 删该段覆盖。 */
  paragraphs: Record<string, Record<string, unknown> | null>;
}

/**
 * 写草稿（`PATCH /documents/{did}/draft`）：成功即把响应当作新草稿写进缓存
 * （服务端已 `revision+1`），并让详情/列表失效——详情里 `compile.stale` 会随之变化。
 * 服务端返回的两类冲突由调用方分支：409 `revision_conflict`（刷新草稿重试）、
 * 409 `document_busy`（活动 job 期间只读）、422 `draft_invalid`（字段/范围不合法）。
 */
export function usePatchDraftMutation(did: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: ({ baseRevision, paragraphs }: PatchDraftVariables) => {
      const body: DraftPatchRequest = { base_revision: baseRevision, paragraphs };
      return apiPatch<DraftResponse>(`/documents/${encodeURIComponent(did)}/draft`, body);
    },
    onSuccess: (draft) => {
      client.setQueryData(queryKeys.draft(did), draft);
      void client.invalidateQueries({ queryKey: queryKeys.document(did) });
      void client.invalidateQueries({ queryKey: queryKeys.documents });
    },
  });
}

/**
 * 手动编译（`POST /documents/{did}/jobs`，`action=compile`）：草稿比 PDF 新时用户点了才跑。
 * 不传 `profile`（compile 不调翻译/审查）；`base_revision` = 当前草稿 revision，
 * 服务端不符时 409 `revision_conflict`（点之前草稿又变了）。
 */
export function useCompileDraftMutation(did: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (baseRevision: number) =>
      apiPost<JobAccepted>(`/documents/${encodeURIComponent(did)}/jobs`, {
        action: 'compile',
        scope: 'full',
        base_revision: baseRevision,
        // 生成的类型把 `dual` 标成必填（服务端有默认值 false）：显式给 false，compile 不产出 dual
        dual: false,
      } satisfies JobCreateRequest),
    onSuccess: () => {
      invalidateJobViews(client, did);
    },
  });
}

// --------------------------------------------------------------------------- #
// W08：上传 / profiles / jobs
// --------------------------------------------------------------------------- #

/** profile 列表（`GET /profiles`）：只有 id/label/has_*，命令字符串永不出现。 */
export function useProfiles() {
  return useQuery({
    queryKey: queryKeys.profiles,
    queryFn: () => apiGet<ProfileListItem[]>('/profiles'),
    staleTime: 60_000,
  });
}

/**
 * 该文档的 job 列表（`GET /documents/{did}/jobs`，新 → 旧）。
 *
 * 轮询口径（brief）：有 `queued`/`running` 时 2s，否则 30s —— job 是"run 归档还没出现"
 * 那段时间里唯一的真实信号（事件流要等 run 归档建好才活）。`refetchMs` 显式给定时覆盖它
 * （W10 的编译状态条在编译进行中要更快看到终态）。
 */
export function useJobs(did: string | null, options: { refetchMs?: number } = {}) {
  const refetchMs = options.refetchMs;
  return useQuery({
    queryKey: queryKeys.jobs(did ?? ''),
    queryFn: () => apiGet<JobRecord[]>(`/documents/${encodeURIComponent(did ?? '')}/jobs`),
    staleTime: 1_000,
    refetchInterval: (query) => refetchMs ?? jobsRefetchInterval(query.state.data),
    enabled: did !== null && did !== '',
  });
}

/** 上传一个 PDF（`POST /documents`）；成功后文件库列表失效（服务端已建 did）。 */
export function useUploadMutation() {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (file: File) => apiUpload<DocumentUploaded>('/documents', file),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: queryKeys.documents });
    },
  });
}

/**
 * 提交 job（`POST /documents/{did}/jobs`）。
 *
 * `JobCreateRequest`（生成的 OpenAPI 类型）里**没有** translator/reviewer/timeout 字段：
 * 那些由服务端从 profile 解析，带了会被 422 `forbidden_field` 拒掉。
 */
export function useCreateJobMutation(did: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (body: JobCreateRequest) =>
      apiPost<JobAccepted>(`/documents/${encodeURIComponent(did)}/jobs`, body),
    onSuccess: () => {
      invalidateJobViews(client, did);
    },
  });
}

/** 取消 job（`POST /jobs/{jid}/cancel`，幂等：已终态返回 200 + 当前状态）。 */
export function useCancelJobMutation(did: string) {
  const client = useQueryClient();
  return useMutation({
    mutationFn: (jobId: string) =>
      apiPost<JobRecord>(`/jobs/${encodeURIComponent(jobId)}/cancel`),
    onSuccess: () => {
      invalidateJobViews(client, did);
    },
  });
}

/** 提交/取消后让所有"受 job 影响"的视图重新取：job 列表 + 文档详情 + 列表卡片。 */
function invalidateJobViews(client: ReturnType<typeof useQueryClient>, did: string): void {
  void client.invalidateQueries({ queryKey: queryKeys.jobs(did) });
  void client.invalidateQueries({ queryKey: queryKeys.document(did) });
  void client.invalidateQueries({ queryKey: queryKeys.stageState(did) });
  void client.invalidateQueries({ queryKey: queryKeys.documents });
}

/** 供 UI 判断"这次失败是不是文件太大"（413 单独给文案）。 */
export function isFileTooLarge(error: unknown): boolean {
  return error instanceof ApiError && (error.code === 'file_too_large' || error.status === 413);
}
