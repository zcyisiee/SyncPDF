/**
 * documentStore（M2-07）：文档、段落、事件流。沿用现版双 store 划分——
 * 本 store 只放与文档数据相关状态；布局 / 选中 / 面板在 uiStore。
 *
 * 事件 reducer 处理：run_started / stage_started / stage_finished / progress /
 * paragraph / page_ready / issue / document_finished / run_finished / error。
 * seq 续传（§9.3）：`lastSeq` 记录已消费的最大事件序号，重复 / 回退事件丢弃。
 */
import { createStore } from 'zustand/vanilla';
import type {
  DocumentFinishedEvent,
  EngineEvent,
  ErrorEvent,
  IssueEvent,
  PageReadyEvent,
  ParagraphEvent,
  ProgressEvent,
  RunFinishedEvent,
  RunStartedEvent,
  StageFinishedEvent,
  StageStartedEvent,
  StageName,
  ParagraphStatus,
  Box,
  CoordSystem,
} from '@shared/protocol';

/** 段落最新状态（paragraph 事件的累积结果）。 */
export interface ParagraphRecord {
  id: string;
  page: number | null;
  status: ParagraphStatus;
  boxes: Box[] | null;
  coordSystem: CoordSystem;
  translatedHtml: string;
}

/** 问题面板条目。 */
export interface IssueRecord {
  seq: number;
  severity: IssueEvent['severity'];
  code: string;
  paragraphId: string | undefined;
  page: number | undefined;
  message: string;
}

/** 最近错误（error 事件快照）。 */
export interface ErrorRecord {
  seq: number;
  fatal: boolean;
  code: string;
  message: string;
}

/** 页就绪记录（page_ready 事件；previewPath 可选）。 */
export interface PageReadyRecord {
  page: number;
  previewPath: string | undefined;
}

export type RunState =
  | 'idle'
  | 'running'
  | 'finished'
  | 'failed'
  | 'cancelled';

export interface DocumentState {
  /** 引擎事件应用后的文档快照。 */
  docId: string | null;
  pageCount: number | null;
  runState: RunState;
  stage: StageName | null;
  stageElapsedMs: Record<string, number>;
  /** 各阶段最新进度。 */
  progress: { stage: StageName; done: number; total: number } | null;
  /** 段落 id → 记录（Map 语义，改用普通对象 + 展开保持不可变更新）。 */
  paragraphs: Record<string, ParagraphRecord>;
  /** 段落插入顺序（树按页展示用）。 */
  paragraphOrder: string[];
  issues: IssueRecord[];
  pagesReady: Record<number, PageReadyRecord>;
  /** 输出与统计（document_finished）。 */
  output: string | null;
  stats: DocumentFinishedEvent['stats'] | null;
  /** 最近一条 error 事件（面板展示）。 */
  lastError: ErrorRecord | null;
  lastSeq: number;
  /** 引擎版本（run_started）。 */
  engineVersion: string | null;
}

export interface DocumentActions {
  /** 事件 reducer 入口：应用一个引擎事件（seq 去重）。 */
  applyEvent: (event: EngineEvent) => void;
  /** 换文档 / 关闭文档：清空全部状态。 */
  reset: (docId: string | null) => void;
}

export type DocumentStore = DocumentState & DocumentActions;

export const initialDocumentState: DocumentState = {
  docId: null,
  pageCount: null,
  runState: 'idle',
  stage: null,
  stageElapsedMs: {},
  progress: null,
  paragraphs: {},
  paragraphOrder: [],
  issues: [],
  pagesReady: {},
  output: null,
  stats: null,
  lastError: null,
  lastSeq: 0,
  engineVersion: null,
};

// ---------- 纯 reducer（可单测） ----------

/** paragraph 事件 → paragraphs/paragraphOrder 增量更新。 */
function reduceParagraph(
  state: DocumentState,
  event: ParagraphEvent,
): Pick<DocumentState, 'paragraphs' | 'paragraphOrder'> {
  const record: ParagraphRecord = {
    id: event.paragraph_id,
    // 页号从 id 前缀 `P{page:02}` 提取（段 id 与页绑定）
    page: pageNumberFromId(event.paragraph_id),
    status: event.status,
    boxes: event.boxes,
    coordSystem: event.coord_system,
    translatedHtml: event.translated_html,
  };
  const exists = state.paragraphs[event.paragraph_id] !== undefined;
  return {
    paragraphs: { ...state.paragraphs, [event.paragraph_id]: record },
    paragraphOrder: exists
      ? state.paragraphOrder
      : [...state.paragraphOrder, event.paragraph_id],
  };
}

/** `P05-002` → 5；解析失败 → null。 */
export function pageNumberFromId(id: string): number | null {
  const match = /^P(\d+)-/.exec(id);
  if (match === null) return null;
  const page = Number.parseInt(match[1], 10);
  return Number.isFinite(page) ? page : null;
}

/** 事件 reducer：返回增量 patch（含 seq 去重）。不修改入参。 */
export function reduceEvent(state: DocumentState, event: EngineEvent): Partial<DocumentState> | null {
  // seq 续传（§9.3）：单调递增，重复 / 回退直接丢弃
  if (event.seq <= state.lastSeq) return null;
  const base: Pick<DocumentState, 'lastSeq'> = { lastSeq: event.seq };
  switch (event.type) {
    case 'run_started':
      return {
        ...base,
        docId: (event as RunStartedEvent).doc_id,
        pageCount: (event as RunStartedEvent).pages,
        runState: 'running',
        paragraphs: {},
        paragraphOrder: [],
        issues: [],
        pagesReady: {},
        output: null,
        stats: null,
        stage: null,
        progress: null,
        engineVersion: (event as RunStartedEvent).engine_version,
      };
    case 'stage_started':
      return { ...base, stage: (event as StageStartedEvent).stage };
    case 'stage_finished': {
      const stageEvent = event as StageFinishedEvent;
      return {
        ...base,
        stageElapsedMs: {
          ...state.stageElapsedMs,
          [stageEvent.stage]: stageEvent.elapsed_ms,
        },
      };
    }
    case 'progress': {
      const progress = event as ProgressEvent;
      return { ...base, progress: { stage: progress.stage, done: progress.done, total: progress.total } };
    }
    case 'paragraph':
      return { ...base, ...reduceParagraph(state, event as ParagraphEvent) };
    case 'page_ready': {
      const pageEvent = event as PageReadyEvent;
      return {
        ...base,
        pagesReady: {
          ...state.pagesReady,
          [pageEvent.page]: { page: pageEvent.page, previewPath: pageEvent.preview_path },
        },
      };
    }
    case 'issue': {
      const issue = event as IssueEvent;
      return {
        ...base,
        issues: [
          ...state.issues,
          {
            seq: issue.seq,
            severity: issue.severity,
            code: issue.code,
            paragraphId: issue.paragraph_id,
            page: issue.page,
            message: issue.message,
          },
        ],
      };
    }
    case 'document_finished': {
      const finished = event as DocumentFinishedEvent;
      return { ...base, output: finished.output, stats: finished.stats };
    }
    case 'run_finished': {
      const runFinished = event as RunFinishedEvent;
      return { ...base, runState: runFinished.ok ? 'finished' : 'failed' };
    }
    case 'error': {
      const error = event as ErrorEvent;
      const record: ErrorRecord = {
        seq: error.seq,
        fatal: error.fatal,
        code: error.code,
        message: error.message,
      };
      return {
        ...base,
        lastError: record,
        runState: error.fatal ? 'failed' : state.runState,
      };
    }
  }
}

// ---------- store ----------

export function createDocumentStore() {
  return createStore<DocumentStore>()((set, get) => ({
    ...initialDocumentState,
    applyEvent: (event) => {
      const patch = reduceEvent(get(), event);
      if (patch === null) return;
      set(patch);
    },
    reset: (docId) => {
      set({ ...initialDocumentState, docId });
    },
  }));
}

export const documentStore = createDocumentStore();
