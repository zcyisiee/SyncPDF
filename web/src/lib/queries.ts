/**
 * TanStack Query hooks：文件库列表（10s stale）、文档详情（5s stale）、产物清单与
 * bbox 几何（各 60s stale，did 为空时不查）。
 *
 * geometry 的 404（`snapshot_unavailable` / `geometry_unavailable`）不是错误：产物缺失
 * 时该页照样能预览，只是没有 bbox，所以 query 归一成 `data = null` 由 UI 显示小条。
 */
import { useQuery } from '@tanstack/react-query';

import type { ArtifactItem, DocumentDetail, DocumentListItem, GeometryResponse } from '../api/types';
import { ApiError, apiGet } from './api';
import type { BboxMode } from './preview';

export const queryKeys = {
  documents: ['documents'] as const,
  document: (did: string) => ['documents', did] as const,
  artifacts: (did: string) => ['documents', did, 'artifacts'] as const,
  geometry: (did: string, kind: BboxMode, page: number) =>
    ['documents', did, 'geometry', kind, page] as const,
};

export function useDocuments() {
  return useQuery({
    queryKey: queryKeys.documents,
    queryFn: () => apiGet<DocumentListItem[]>('/documents'),
    staleTime: 10_000,
  });
}

export function useDocument(did: string | null) {
  return useQuery({
    queryKey: queryKeys.document(did ?? ''),
    queryFn: () => apiGet<DocumentDetail>(`/documents/${encodeURIComponent(did ?? '')}`),
    staleTime: 5_000,
    enabled: did !== null && did !== '',
  });
}

/** 产物清单（`GET /documents/{did}/artifacts`）；预览用它判断有没有可渲染的 PDF。 */
export function useArtifacts(did: string | null) {
  return useQuery({
    queryKey: queryKeys.artifacts(did ?? ''),
    queryFn: () => apiGet<ArtifactItem[]>(`/documents/${encodeURIComponent(did ?? '')}/artifacts`),
    staleTime: 60_000,
    enabled: did !== null && did !== '',
  });
}

/**
 * 该页 bbox 几何。`kind = null` 时（bbox 图层关掉）不发请求；404 归一成 `null`：
 * `isSuccess && data === null` ⇔ 服务端明确说“这一页/这类产物不可用”。
 */
export function useGeometry(
  did: string | null,
  kind: Exclude<BboxMode, 'off'> | null,
  page: number,
) {
  return useQuery({
    queryKey: queryKeys.geometry(did ?? '', kind ?? 'off', page),
    queryFn: async () => {
      const path = `/documents/${encodeURIComponent(did ?? '')}/geometry?kind=${kind}&page=${page}`;
      try {
        return await apiGet<GeometryResponse>(path);
      } catch (error) {
        if (error instanceof ApiError && error.status === 404) return null;
        throw error;
      }
    },
    staleTime: 60_000,
    enabled: did !== null && did !== '' && kind !== null && page > 0,
  });
}
