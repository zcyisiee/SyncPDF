import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { renderHook } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import type { ReactNode } from 'react';

import { usePersistentEvents } from '../src/lib/usePersistentEvents';

let listener: ((event: MessageEvent<string>) => void) | undefined;
const closed = vi.fn();
const connected = vi.fn();
class FakeSource {
  constructor(url: string) { connected(url); }
  addEventListener(_kind: string, callback: (event: MessageEvent<string>) => void) { listener = callback; }
  close() { closed(); }
}
afterEach(() => { vi.unstubAllGlobals(); vi.clearAllMocks(); sessionStorage.clear(); });

it('persists a document cursor, ignores duplicate delivery, and closes on unmount', () => {
  vi.stubGlobal('EventSource', FakeSource);
  sessionStorage.setItem('bdt.events.paper', '12');
  const client = new QueryClient();
  const invalidated = vi.spyOn(client, 'invalidateQueries');
  const wrapper = ({ children }: { children: ReactNode }) => <QueryClientProvider client={client}>{children}</QueryClientProvider>;
  const hook = renderHook(() => usePersistentEvents('paper'), { wrapper });
  expect(connected).toHaveBeenCalledWith(expect.stringContaining('persistent=true&after_seq=12'));
  listener?.(new MessageEvent('preview_ready', { data: '{}', lastEventId: '13' }));
  expect(sessionStorage.getItem('bdt.events.paper')).toBe('13');
  expect(invalidated).toHaveBeenCalledTimes(3);
  listener?.(new MessageEvent('preview_ready', { data: '{}', lastEventId: '13' }));
  expect(invalidated).toHaveBeenCalledTimes(3);
  hook.unmount();
  expect(closed).toHaveBeenCalledOnce();
});
