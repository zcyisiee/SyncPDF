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
      void client.invalidateQueries({ queryKey: keys.document(did) });
      void client.invalidateQueries({ queryKey: keys.jobs(did) });
      void client.invalidateQueries({ queryKey: keys.paragraphs(did) });
    };
    for (const kind of kinds) source.addEventListener(kind, receive);
    return () => source.close();
  }, [client, did]);
}
