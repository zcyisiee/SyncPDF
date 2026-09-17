import { QueryClientProvider } from '@tanstack/react-query';
import { render } from '@testing-library/react';
import type { ReactElement } from 'react';
import { vi } from 'vitest';

import { createQueryClient } from '../src/app/App';
import type { ScreenViewport } from '../src/components/preview/BboxLayer';
import type { EventFeed } from '../src/components/events/useEventWindow';
import type { RunEvent } from '../src/lib/events';
import { createUiStore, uiStore } from '../src/stores/ui';

/** 最小 fetch 响应替身：api.ts 只读 ok/status/text()，不依赖全局 Response。 */
export function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

export function textResponse(body: string, status: number): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => body,
  } as unknown as Response;
}

/** 按路径分发 mock fetch；未命中的路径返回 404 错误信封。 */
export function mockApiFetch(routes: Record<string, () => Response | Promise<Response>>) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
    const url = String(input);
    const route = routes[url];
    if (route) return await route();
    return jsonResponse({ error: { code: 'not_found', message: `未 mock 的路径：${url}` } }, 404);
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

/** 组件用的是模块级 store 单例：每个用例前恢复数据字段（保留单例自己的 action）。 */
export function resetUiStore(): void {
  const fresh = createUiStore().getState();
  uiStore.setState({
    screen: 'library',
    viewrailWidth: fresh.viewrailWidth,
    inspectorWidth: fresh.inspectorWidth,
    timelineHeight: fresh.timelineHeight,
    inspectorCollapsed: fresh.inspectorCollapsed,
    previewMode: 'target',
    previewPage: 1,
    previewDid: null,
    bboxMode: 'parse',
    selectedParagraphId: null,
    dragging: null,
  });
}

export function renderWithQuery(ui: ReactElement) {
  return render(<QueryClientProvider client={createQueryClient()}>{ui}</QueryClientProvider>);
}

/**
 * 事件窗口 feed 的替身（W06）：组件只需要这份数据结构，SSE 生命周期由
 * `useEventWindow` 负责（那个用 stub EventSource 单测）。
 */
export function makeEventFeed(overrides: Partial<EventFeed> = {}): EventFeed {
  return {
    events: [],
    runId: null,
    isPending: false,
    hasArchive: true,
    error: null,
    connection: 'open',
    hasEarlier: false,
    isLoadingEarlier: false,
    loadEarlier: () => {},
    retry: () => {},
    ...overrides,
  };
}

/** 一条事件（`{seq, at, stage, kind, data}`）。 */
export function makeEvent(seq: number, overrides: Partial<RunEvent> = {}): RunEvent {
  return {
    seq,
    at: `2026-09-16T13:28:${String(seq % 60).padStart(2, '0')}.000+00:00`,
    stage: 'translate',
    kind: 'call_started',
    data: { attempt: 1 },
    ...overrides,
  };
}

/** pdf.js `PageViewport`（rotation=0）的替身：变换公式照抄 pdf.js 源码
 * （build/pdf.mjs `PageViewport` 构造器，rotation=0 时 `transform = [s,0,0,-s,-s*x0,s*y1]`）。
 * 单测不需要真 pdf.js（jsdom 无 canvas），但公式必须与库一致，否则换算测试自说自话。
 */
export function makeViewport({
  scale = 1,
  viewBox = [0, 0, 612, 792],
}: { scale?: number; viewBox?: [number, number, number, number] } = {}): ScreenViewport {
  const [x0, y0, x1, y1] = viewBox;
  const a = scale;
  const d = -scale;
  const e = -scale * x0;
  const f = scale * y1;
  return {
    width: (x1 - x0) * scale,
    height: (y1 - y0) * scale,
    scale,
    viewBox,
    convertToViewportRectangle(rect) {
      const map = (x: number, y: number) => [a * x + e, d * y + f];
      const [ax, ay] = map(rect[0], rect[1]);
      const [bx, by] = map(rect[2], rect[3]);
      return [ax, ay, bx, by];
    },
  };
}
