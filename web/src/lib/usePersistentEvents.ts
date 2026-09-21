import { useQueryClient } from '@tanstack/react-query';
import { useEffect, useState } from 'react';

import { API_BASE } from './api';
import { queryKeys as keys } from './queries';

const kinds = ['translation_block_completed', 'preview_ready', 'preview_failed', 'block_not_replaced',
  'compile_float',
  'job_queued', 'job_started', 'job_finished', 'job_failed', 'job_canceled', 'job_interrupted'];

/** 持久事件流里属于「编译/成页」时间线的事件类型（翻译时间线走 run 归档窗口）。 */
export const COMPILE_EVENT_KINDS = [
  'translation_block_completed',
  'preview_ready',
  'preview_failed',
  'block_not_replaced',
  'compile_float',
] as const;

export type PersistentEvent = {
  seq: number;
  type: string;
  blockId: string | null;
  page: number | null;
  at: string | null;
  data: Record<string, unknown> | null;
};

/** 编译时间线窗口保留的最近事件数（与翻译侧 EVENTS_WINDOW_SIZE 同量级）。 */
const COMPILE_WINDOW_SIZE = 200;

/**
 * Committed cursor is document-scoped and independent of ephemeral debug runs.
 *
 * 副作用：`preview_ready` 直接更新 `previewPages` 缓存（页面 URL 变化触发
 * TransitioningPdfPage 动画）；缓存还没被首次拉取时退化为 invalidate，保证
 * 页面一定会重新取到。返回值：最近一小窗编译侧事件（右栏时间线用）。
 */
export function usePersistentEvents(did: string): { events: PersistentEvent[] } {
  const client = useQueryClient();
  const [events, setEvents] = useState<PersistentEvent[]>([]);
  useEffect(() => {
    if (typeof EventSource === 'undefined') return;
    const key = `bdt.events.${did}`;
    let cursor = Number(sessionStorage.getItem(key)) || 0;
    const source = new EventSource(`${API_BASE}/documents/${encodeURIComponent(did)}/events/stream?persistent=true&after_seq=${cursor}`);
    const receive = (message: MessageEvent<string>) => {
      const next = Number(message.lastEventId);
      if (!Number.isSafeInteger(next) || next <= cursor) return;
      cursor = next;
      sessionStorage.setItem(key, String(cursor));
      let parsed: Record<string, unknown> | null = null;
      try {
        parsed = JSON.parse(message.data) as Record<string, unknown>;
      } catch {
        parsed = null;
      }
      const payload = (parsed?.data ?? parsed) as Record<string, unknown> | null;
      setEvents((current) => {
        const entry: PersistentEvent = {
          seq: next,
          type: message.type,
          blockId: typeof parsed?.block_id === 'string' ? parsed.block_id : null,
          page: typeof payload?.page === 'number' ? payload.page : null,
          at: typeof parsed?.created_at === 'string' ? parsed.created_at : null,
          data: payload && typeof payload === 'object' ? payload : null,
        };
        const merged = [...current, entry];
        return merged.length > COMPILE_WINDOW_SIZE ? merged.slice(merged.length - COMPILE_WINDOW_SIZE) : merged;
      });
      if (message.type === 'preview_ready') {
        const data = payload as { page?: number; asset?: string; complete?: boolean; page_revision?: number; updated_at?: string } | null;
        if (data?.page && data.asset) {
          client.setQueryData<{ did: string; revision: number; pages: Array<{ page: number; asset: string; complete: boolean; page_revision?: number; updated_at: string }> } | undefined>(
            keys.previewPages(did),
            (previous) => {
              if (!previous) return previous;
              const current = previous.pages.find((item) => item.page === data.page);
              if (current?.page_revision !== undefined && data.page_revision !== undefined && data.page_revision <= current.page_revision) return previous;
              const pages = previous.pages.filter((item) => item.page !== data.page);
              pages.push({ page: data.page!, asset: data.asset!, complete: data.complete ?? false, page_revision: data.page_revision, updated_at: data.updated_at ?? new Date().toISOString() });
              pages.sort((a, b) => a.page - b.page);
              return { ...previous, pages, revision: Math.max(previous.revision, data.page_revision ?? previous.revision) };
            },
          );
          // 缓存还没首拉（比如文档刚打开就开跑）：setQueryData 是 no-op，必须
          // invalidate 让 usePreviewPages 真的去取一次，否则页面永远不出现。
          if (client.getQueryData(keys.previewPages(did)) === undefined) {
            void client.invalidateQueries({ queryKey: keys.previewPages(did) });
          }
        }
      }
      void client.invalidateQueries({ queryKey: keys.document(did), exact: true });
      void client.invalidateQueries({ queryKey: keys.jobs(did) });
      void client.invalidateQueries({ queryKey: keys.paragraphs(did) });
    };
    for (const kind of kinds) source.addEventListener(kind, receive);
    return () => source.close();
  }, [client, did]);
  return { events };
}
