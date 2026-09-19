import { useQueryClient } from '@tanstack/react-query';
import { useEffect } from 'react';

import { API_BASE } from './api';
import { queryKeys as keys } from './queries';

const kinds = ['translation_block_completed', 'preview_ready', 'preview_failed',
  'job_queued', 'job_started', 'job_finished', 'job_failed', 'job_canceled', 'job_interrupted'];

/** Committed cursor is document-scoped and independent of ephemeral debug runs. */
export function usePersistentEvents(did: string) {
  const client = useQueryClient();
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
      if (message.type === 'preview_ready') {
        try {
          const event = JSON.parse(message.data) as { data?: { page?: number; asset?: string; complete?: boolean; page_revision?: number; updated_at?: string }; page?: number; asset?: string; complete?: boolean; page_revision?: number; updated_at?: string };
          const data = event.data ?? event;
          if (data.page && data.asset) {
            client.setQueryData(keys.previewPages(did), (previous: { did: string; revision: number; pages: Array<{ page: number; asset: string; complete: boolean; page_revision?: number; updated_at: string }> } | undefined) => {
              if (!previous) return previous;
              const current = previous.pages.find((item) => item.page === data.page);
              if (current?.page_revision !== undefined && data.page_revision !== undefined && data.page_revision <= current.page_revision) return previous;
              const pages = previous.pages.filter((item) => item.page !== data.page);
              pages.push({ page: data.page!, asset: data.asset!, complete: data.complete ?? false, page_revision: data.page_revision, updated_at: data.updated_at ?? new Date().toISOString() });
              pages.sort((a, b) => a.page - b.page);
              return { ...previous, pages, revision: Math.max(previous.revision, data.page_revision ?? previous.revision) };
            });
          }
        } catch { /* malformed event: normal invalidation below repairs the cache */ }
      }
      void client.invalidateQueries({ queryKey: keys.document(did), exact: true });
      void client.invalidateQueries({ queryKey: keys.jobs(did) });
      void client.invalidateQueries({ queryKey: keys.paragraphs(did) });
    };
    for (const kind of kinds) source.addEventListener(kind, receive);
    return () => source.close();
  }, [client, did]);
}
