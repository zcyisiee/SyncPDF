import { QueryClientProvider } from '@tanstack/react-query';
import { render } from '@testing-library/react';
import type { ReactElement } from 'react';
import { vi } from 'vitest';

import { createQueryClient } from '../src/app/App';
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
    dragging: null,
  });
}

export function renderWithQuery(ui: ReactElement) {
  return render(<QueryClientProvider client={createQueryClient()}>{ui}</QueryClientProvider>);
}
