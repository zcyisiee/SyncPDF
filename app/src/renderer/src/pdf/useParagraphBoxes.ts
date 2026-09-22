/**
 * documentStore 的段落记录 → 按页分组的段落框（BoxLayer 输入）。
 *
 * 页号约定：引擎事件里的 `page` 与段落 id 前缀 `P{page:02}` 一致，
 * 与 pdf.js 的 1 基页号对齐（fake-sidecar 12 页 → page 1..12）。
 * `boxes === null`（未识别）与 `boxes === []`（识别了但无框，规约 #12/#13）都不画框。
 */
import { useMemo } from 'react';
import { useDocumentStore } from '../store/documentStoreStoreHooks';
import type { ParagraphBoxes } from './BoxLayer';

export function useParagraphBoxes(): Map<number, ParagraphBoxes[]> {
  const paragraphs = useDocumentStore((state) => state.paragraphs);
  const order = useDocumentStore((state) => state.paragraphOrder);

  return useMemo(() => {
    const byPage = new Map<number, ParagraphBoxes[]>();
    for (const id of order) {
      const record = paragraphs[id];
      if (record === undefined) continue;
      if (record.boxes === null || record.boxes.length === 0) continue;
      const page = record.page;
      if (page === null) continue;
      const entry: ParagraphBoxes = {
        id: record.id,
        boxes: record.boxes,
        coordSystem: record.coordSystem,
        status: record.status,
      };
      const list = byPage.get(page);
      if (list === undefined) byPage.set(page, [entry]);
      else list.push(entry);
    }
    return byPage;
  }, [paragraphs, order]);
}
