/**
 * 论文库的主进程 ↔ 渲染进程共享类型。数据落在 `~/.sp/library.db`（主进程 `library.ts`）。
 */
import type { EngineEvent, EventOf, LayoutRegion } from './protocol';

/** new = 已加入未翻译；queued/running 在串行队列里；其余为上次运行的结局。 */
export type DocStatus = 'new' | 'queued' | 'running' | 'done' | 'failed' | 'cancelled';

/** 标题 / 作者的来源，优先级从低到高。 */
export const META_SOURCES = ['filename', 'pdf_info', 'layout', 'user'] as const;
export type MetaSource = (typeof META_SOURCES)[number];

export interface LibraryDoc {
  id: string;
  sha256: string;
  fileName: string;
  title: string;
  titleSource: MetaSource;
  authors: string | null;
  authorsSource: MetaSource;
  /** 页数（首次运行后才知道）。 */
  pages: number | null;
  /** 原文字节数。 */
  size: number;
  status: DocStatus;
  /** 翻译进度 0..1（translating 阶段的 done/total）。 */
  progress: number;
  /** 上次完成的回退段数。 */
  fallbacks: number;
  error: string | null;
  elapsedMs: number | null;
  model: string | null;
  addedAt: number;
  updatedAt: number;
  openedAt: number | null;
  /** 原文 PDF（`blobs/<sha>.pdf`）。 */
  sourcePath: string;
  /** 译文 PDF（`docs/<id>/translated.pdf`）；尚不存在时为 null。 */
  translatedPath: string | null;
}

export type ParagraphRecord = Omit<EventOf<'paragraph'>, 'seq' | 'ts' | 'type'>;
export type IssueRecord = Omit<EventOf<'issue'>, 'seq' | 'ts' | 'type'>;

/** 打开一篇论文时需要的缓存（无需重跑引擎）。 */
export interface DocSnapshot {
  /** 1 基页号 → 版面区域。 */
  layout: Record<number, LayoutRegion[]>;
  paragraphs: ParagraphRecord[];
  issues: IssueRecord[];
}

/** 主进程推给渲染进程的引擎事件：`docId` 为当前任务（任务外的事件为 null）。 */
export interface DocEngineEvent {
  docId: string | null;
  event: EngineEvent;
}
