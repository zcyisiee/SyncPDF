/**
 * `useArtifacts` / `useGeometry` 的错误降级：geometry 的 404（snapshot/geometry_unavailable）
 * 必须归一成 `data = null`（预览仍可用、只显示小条），其它错误照旧是 error。
 */
import { QueryClientProvider } from '@tanstack/react-query';
import { renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { describe, expect, it } from 'vitest';

import { createQueryClient } from '../src/app/App';
import { useArtifacts, useGeometry } from '../src/lib/queries';
import { jsonResponse, mockApiFetch } from './helpers';

const DID = 'ccs3764-dyn';

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={createQueryClient()}>{children}</QueryClientProvider>;
}

const MONO = {
  name: 'output/paper.no_watermark.zh.mono.pdf',
  path: 'output/paper.no_watermark.zh.mono.pdf',
  kind: 'pdf',
  size: 6257602,
  mtime: '2026-09-15T17:03:14.105Z',
};

describe('useArtifacts', () => {
  it('200 → 产物清单', async () => {
    mockApiFetch({ [`/api/v1/documents/${DID}/artifacts`]: () => jsonResponse([MONO]) });
    const { result } = renderHook(() => useArtifacts(DID), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual([MONO]);
  });

  it('文档不存在（404）→ error（不是空清单，不假装没有产物）', async () => {
    mockApiFetch({
      [`/api/v1/documents/${DID}/artifacts`]: () =>
        jsonResponse({ error: { code: 'document_not_found', message: '文档不存在' } }, 404),
    });
    const { result } = renderHook(() => useArtifacts(DID), { wrapper });
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.data).toBeUndefined();
  });
});

describe('useGeometry', () => {
  it('200 → 原样返回（含 coord_system）', async () => {
    mockApiFetch({
      [`/api/v1/documents/${DID}/geometry?kind=parse&page=3`]: () =>
        jsonResponse({
          did: DID,
          kind: 'parse',
          coord_system: 'pdf_topleft',
          page: 3,
          entities: [],
          relations: [],
        }),
    });
    const { result } = renderHook(() => useGeometry(DID, 'parse', 3), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.coord_system).toBe('pdf_topleft');
  });

  it('404 snapshot_unavailable → isSuccess 且 data 为 null（降级，不是错误）', async () => {
    mockApiFetch({
      [`/api/v1/documents/${DID}/geometry?kind=parse&page=1`]: () =>
        jsonResponse(
          { error: { code: 'snapshot_unavailable', message: '没有 parse 段落快照' } },
          404,
        ),
    });
    const { result } = renderHook(() => useGeometry(DID, 'parse', 1), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toBeNull();
    expect(result.current.isError).toBe(false);
  });

  it('404 geometry_unavailable → 同样降级成 null', async () => {
    mockApiFetch({
      [`/api/v1/documents/${DID}/geometry?kind=layout&page=2`]: () =>
        jsonResponse({ error: { code: 'geometry_unavailable', message: '缺 layout 几何' } }, 404),
    });
    const { result } = renderHook(() => useGeometry(DID, 'layout', 2), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toBeNull();
  });

  it('500 → 真错误（500 不是“产物缺失”）', async () => {
    mockApiFetch({
      [`/api/v1/documents/${DID}/geometry?kind=parse&page=1`]: () =>
        jsonResponse({ error: { code: 'internal_error', message: '炸了' } }, 500),
    });
    const { result } = renderHook(() => useGeometry(DID, 'parse', 1), { wrapper });
    await waitFor(() => expect(result.current.isError).toBe(true));
  });

  it('kind 为 null（图层关）→ 不发请求', async () => {
    const fetchMock = mockApiFetch({});
    const { result } = renderHook(() => useGeometry(DID, null, 1), { wrapper });
    expect(result.current.fetchStatus).toBe('idle');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('query key 含 kind 与 page（换页/换图层各查各的）', async () => {
    const fetchMock = mockApiFetch({
      [`/api/v1/documents/${DID}/geometry?kind=parse&page=1`]: () =>
        jsonResponse({ did: DID, kind: 'parse', coord_system: 'pdf_topleft', page: 1 }),
      [`/api/v1/documents/${DID}/geometry?kind=parse&page=2`]: () =>
        jsonResponse({ did: DID, kind: 'parse', coord_system: 'pdf_topleft', page: 2 }),
    });
    const { result, rerender } = renderHook(({ page }) => useGeometry(DID, 'parse', page), {
      wrapper,
      initialProps: { page: 1 },
    });
    await waitFor(() => expect(result.current.data?.page).toBe(1));
    rerender({ page: 2 });
    await waitFor(() => expect(result.current.data?.page).toBe(2));
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
