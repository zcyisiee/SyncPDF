/**
 * 叠加框合并：段落框可点，被段落覆盖的版面区域不重复画，未覆盖的区域只画框，
 * 行内公式始终画成虚线；隐藏类型不出现。
 * @vitest-environment node
 */
import { describe, expect, it } from 'vitest';
import type { ParagraphRecord } from '@shared/library';
import type { LayoutRegion, Rect } from '@shared/protocol';
import { overlayItems } from '@/pdf/overlay';

const r = (x0: number, y0: number, x1: number, y1: number): Rect => ({ x0, y0, x1, y1 });

function paragraph(id: string, box: Rect, overrides: Partial<ParagraphRecord> = {}): ParagraphRecord {
  return {
    paragraph_id: id,
    page: 1,
    status: 'typeset',
    boxes: [box],
    coord_system: 'pdf_user',
    translated_html: '<p>译</p>',
    kind: 'text',
    source_text: 'src',
    ...overrides,
  };
}

const region = (kind: LayoutRegion['kind'], bbox: Rect, inline = false): LayoutRegion => ({ kind, inline, bbox });

describe('overlayItems', () => {
  it('段落覆盖的区域只保留段落框，未覆盖的区域以不可点的框保留', () => {
    const items = overlayItems(
      [region('text', r(50, 500, 550, 700)), region('figure', r(50, 100, 550, 400))],
      [paragraph('P01-001', r(56, 510, 540, 690))],
      [],
    );
    expect(items.map((i) => [i.kind, i.paragraphId])).toEqual([
      ['text', 'P01-001'],
      ['figure', null],
    ]);
  });

  it('行内公式即使落在段落内也画出，且标记为 inline', () => {
    const items = overlayItems(
      [region('formula', r(200, 600, 260, 612), true)],
      [paragraph('P01-001', r(56, 510, 540, 690))],
      [],
    );
    expect(items.find((i) => i.inline)).toMatchObject({ kind: 'formula', paragraphId: null });
  });

  it('image_top_left 段落框不参与 pdf_user 区域的覆盖判断', () => {
    const items = overlayItems(
      [region('text', r(50, 500, 550, 700))],
      [paragraph('P01-001', r(56, 510, 540, 690), { coord_system: 'image_top_left' })],
      [],
    );
    expect(items).toHaveLength(2);
  });

  it('隐藏类型的段落与区域都不出现', () => {
    const items = overlayItems(
      [region('header', r(50, 760, 550, 780)), region('figure', r(50, 100, 550, 400))],
      [paragraph('P01-001', r(56, 510, 540, 690)), paragraph('P01-000', r(50, 760, 550, 780), { kind: 'header' })],
      ['header', 'figure'],
    );
    expect(items.map((i) => i.kind)).toEqual(['text']);
  });
});
