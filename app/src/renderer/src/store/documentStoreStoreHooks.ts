/**
 * documentStore 的 React 绑定（zustand vanilla store + useStore selector）。
 * 放独立文件避免 documentStore.ts（纯逻辑，测试引入）依赖 React。
 */
import { useStore } from 'zustand';
import { documentStore } from './documentStore';
import type { DocumentStore } from './documentStore';

export function useDocumentStore<T>(selector: (state: DocumentStore) => T): T {
  return useStore(documentStore, selector);
}
