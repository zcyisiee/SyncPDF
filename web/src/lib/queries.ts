/** TanStack Query hooks：文件库列表（10s stale）与文档详情（5s stale，did 为空时不查）。 */
import { useQuery } from '@tanstack/react-query';

import type { DocumentDetail, DocumentListItem } from '../api/types';
import { apiGet } from './api';

export const queryKeys = {
  documents: ['documents'] as const,
  document: (did: string) => ['documents', did] as const,
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
