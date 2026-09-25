/**
 * 论文库与当前论文（渲染进程）。
 *
 * - `docs`：主进程 `library:changed` / `library:removed` 推送保持同步；
 * - `open`：当前打开论文的版面 / 段落 / 问题 = 主进程快照 + 之后到达的引擎事件；
 * - `timeline`：每篇论文本次会话收到的事件（事件流面板）；`logs`：引擎 stderr。
 *
 * 事件归约 `reduceOpenDoc` 是纯函数，单测直接喂事件。
 */
import { create } from 'zustand';
import type { DocSnapshot, IssueRecord, LibraryDoc, ParagraphRecord } from '@shared/library';
import type { BlockEditState, EngineEvent, LayoutRegion, ParagraphId } from '@shared/protocol';

export interface OpenDoc {
  id: string;
  /** 快照还没回来。 */
  loading: boolean;
  /** 1 基页号 → 版面区域。 */
  layout: Record<number, LayoutRegion[]>;
  paragraphs: Record<ParagraphId, ParagraphRecord>;
  issues: IssueRecord[];
  /** 单块覆盖（段 ID → 状态）；null = 快照与本次会话都还没给出。 */
  edits: Record<ParagraphId, BlockEditState> | null;
  /** 译文 PDF 修订号：引擎每回写一页 +1，译文栏据此重载。 */
  revision: number;
  /** 加载期间收到了新一轮 run_started：快照已过时，丢弃。 */
  restarted: boolean;
}

/** 跳转请求：PDF 栏滚到该页 / 该段（nonce 保证同一目标可重复触发）。 */
export interface RevealRequest {
  page: number;
  paragraphId: ParagraphId | null;
  nonce: number;
}

const TIMELINE_LIMIT = 5000;
const LOG_LIMIT = 2000;

export function emptyOpenDoc(id: string): OpenDoc {
  return { id, loading: true, layout: {}, paragraphs: {}, issues: [], edits: null, revision: 0, restarted: false };
}

/** 引擎事件归约到当前论文。未涉及的事件原样返回同一对象。 */
export function reduceOpenDoc(open: OpenDoc, event: EngineEvent): OpenDoc {
  switch (event.type) {
    case 'run_started':
      return { ...open, layout: {}, paragraphs: {}, issues: [], restarted: open.loading };
    case 'layout':
      return { ...open, layout: { ...open.layout, [event.page]: event.regions } };
    case 'block_edits':
      return { ...open, edits: editsById(event.edits) };
    case 'paragraph': {
      const { seq: _seq, ts: _ts, type: _type, ...record } = event;
      return { ...open, paragraphs: { ...open.paragraphs, [event.paragraph_id]: record } };
    }
    case 'issue': {
      const { seq: _seq, ts: _ts, type: _type, ...record } = event;
      return { ...open, issues: [...open.issues, record] };
    }
    case 'page_ready':
    case 'run_finished':
      return { ...open, revision: open.revision + 1 };
    default:
      return open;
  }
}

const editsById = (edits: BlockEditState[]): Record<ParagraphId, BlockEditState> =>
  Object.fromEntries(edits.map((edit) => [edit.paragraph_id, edit]));

/** 快照到达：快照为底，加载期间已到的事件覆盖其上。 */
export function mergeSnapshot(open: OpenDoc, snapshot: DocSnapshot): OpenDoc {
  const edits = open.edits ?? editsById(snapshot.edits);
  if (open.restarted) return { ...open, loading: false, restarted: false, edits };
  const paragraphs: Record<ParagraphId, ParagraphRecord> = {};
  for (const p of snapshot.paragraphs) paragraphs[p.paragraph_id] = p;
  const seen = new Set(snapshot.issues.map((issue) => JSON.stringify(issue)));
  return {
    ...open,
    loading: false,
    edits,
    layout: { ...snapshot.layout, ...open.layout },
    paragraphs: { ...paragraphs, ...open.paragraphs },
    issues: [...snapshot.issues, ...open.issues.filter((issue) => !seen.has(JSON.stringify(issue)))],
  };
}

/** 事件流不收 progress（高频，状态栏已展示）。 */
const inTimeline = (event: EngineEvent): boolean => event.type !== 'progress';

export interface LibraryState {
  docs: Record<string, LibraryDoc>;
  openId: string | null;
  open: OpenDoc | null;
  selected: ParagraphId | null;
  reveal: RevealRequest | null;
  timeline: Record<string, EngineEvent[]>;
  logs: string[];
  /** 原文栏当前页与总页数（状态栏用）。 */
  currentPage: number;
  pageCount: number;

  init: () => () => void;
  openDoc: (id: string) => void;
  closeDoc: () => void;
  select: (paragraphId: ParagraphId | null, reveal?: boolean) => void;
  revealPage: (page: number) => void;
  setCurrentPage: (page: number) => void;
  setPageCount: (count: number) => void;
}

export const useLibrary = create<LibraryState>()((set, get) => ({
  docs: {},
  openId: null,
  open: null,
  selected: null,
  reveal: null,
  timeline: {},
  logs: [],
  currentPage: 1,
  pageCount: 0,

  /** 订阅主进程推送并拉取论文列表；返回取消订阅。 */
  init: () => {
    const api = window.syncpdf;
    void api.library.list().then((list) => {
      set({ docs: Object.fromEntries(list.map((doc) => [doc.id, doc])) });
    });
    const offs = [
      api.library.onChanged((doc) => set((s) => ({ docs: { ...s.docs, [doc.id]: doc } }))),
      api.library.onRemoved((id) =>
        set((s) => {
          const { [id]: _removed, ...docs } = s.docs;
          const { [id]: _events, ...timeline } = s.timeline;
          return s.openId === id
            ? { docs, timeline, openId: null, open: null, selected: null }
            : { docs, timeline };
        }),
      ),
      api.engine.onEvent(({ docId, event }) =>
        set((s) => {
          if (docId === null) return {};
          const patch: Partial<LibraryState> = {};
          if (inTimeline(event)) {
            const events = [...(s.timeline[docId] ?? []), event];
            if (event.type === 'run_started') events.splice(0, events.length - 1);
            patch.timeline = { ...s.timeline, [docId]: events.slice(-TIMELINE_LIMIT) };
          }
          if (s.open !== null && s.open.id === docId) {
            const open = reduceOpenDoc(s.open, event);
            // 选中保留：同一篇重跑（编辑后）段 ID 不变，块详情待新事件到达后恢复
            if (open !== s.open) patch.open = open;
          }
          return patch;
        }),
      ),
      api.engine.onLog((line) => set((s) => ({ logs: [...s.logs, line].slice(-LOG_LIMIT) }))),
    ];
    return () => offs.forEach((off) => off());
  },

  openDoc: (id) => {
    if (get().openId === id) return;
    set({ openId: id, open: emptyOpenDoc(id), selected: null, reveal: null, currentPage: 1, pageCount: 0 });
    void window.syncpdf.library.markOpened(id);
    void window.syncpdf.library.snapshot(id).then((snapshot) =>
      set((s) => (s.open?.id === id ? { open: mergeSnapshot(s.open, snapshot) } : {})),
    );
  },

  closeDoc: () => set({ openId: null, open: null, selected: null, reveal: null }),

  select: (paragraphId, reveal = false) =>
    set((s) => {
      const paragraph = paragraphId === null ? undefined : s.open?.paragraphs[paragraphId];
      if (!reveal || paragraph === undefined) return { selected: paragraphId };
      return {
        selected: paragraphId,
        reveal: { page: paragraph.page, paragraphId, nonce: (s.reveal?.nonce ?? 0) + 1 },
      };
    }),

  revealPage: (page) =>
    set((s) => ({ reveal: { page, paragraphId: null, nonce: (s.reveal?.nonce ?? 0) + 1 } })),

  setCurrentPage: (currentPage) => set({ currentPage }),
  setPageCount: (pageCount) => set({ pageCount }),
}));

/** 当前打开的论文条目。 */
export const useOpenDocMeta = (): LibraryDoc | null =>
  useLibrary((s) => (s.openId === null ? null : (s.docs[s.openId] ?? null)));
