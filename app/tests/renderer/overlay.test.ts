/**
 * 叠加框合并：每段一个可点框，原文侧用源段落框、译文侧用译文行框并集；
 * 被段落覆盖的版面区域不重复画，未覆盖的区域只画框；行内公式只在原文侧画成虚线；
 * 隐藏类型不出现。
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
    source_bbox: box,
    ...overrides,
  };
}

const region = (kind: LayoutRegion['kind'], bbox: Rect, inline = false): LayoutRegion => ({ kind, inline, bbox });

describe('overlayItems', () => {
  // 回归：两侧共用译文行框，原文侧画出的是译文版面，且一段拆成逐行的框
  it('原文侧用源段落框、译文侧用译文行框并集，每段一个框', () => {
    const source = r(50, 500, 550, 700);
    const lines = [r(50, 680, 548, 692), r(50, 666, 550, 678), r(50, 652, 300, 664)];
    const p = paragraph('P01-001', source, { boxes: lines });
    expect(overlayItems('source', [], [p], [])).toEqual([
      expect.objectContaining({ key: 'P01-001', rect: source, paragraphId: 'P01-001' }),
    ]);
    expect(overlayItems('target', [], [p], [])).toEqual([
      expect.objectContaining({ key: 'P01-001', rect: r(50, 652, 550, 692), paragraphId: 'P01-001' }),
    ]);
  });

  it('译文侧没有框（null 或 []）的段落不画，原文侧照画', () => {
    const paragraphs = [
      paragraph('P01-001', r(50, 500, 550, 700), { boxes: null }),
      paragraph('P01-002', r(50, 300, 550, 400), { boxes: [] }),
    ];
    expect(overlayItems('target', [], paragraphs, [])).toEqual([]);
    expect(overlayItems('source', [], paragraphs, [])).toHaveLength(2);
  });

  it('区域覆盖按源段落框判断：译文行框移开后，被段落占据的区域在译文侧也不重复画', () => {
    const p = paragraph('P01-001', r(56, 510, 540, 690), { boxes: [r(56, 100, 540, 120)] });
    for (const side of ['source', 'target'] as const) {
      const items = overlayItems(side, [region('text', r(50, 500, 550, 700)), region('figure', r(50, 100, 550, 400))], [p], []);
      expect(items.map((i) => [i.kind, i.paragraphId])).toEqual([
        ['text', 'P01-001'],
        ['figure', null],
      ]);
    }
  });

  it('行内公式只在原文侧画且标记为 inline：译文重排后源位置无效', () => {
    const regions = [region('formula', r(200, 600, 260, 612), true)];
    const paragraphs = [paragraph('P01-001', r(56, 510, 540, 690))];
    expect(overlayItems('source', regions, paragraphs, []).find((i) => i.inline)).toMatchObject({
      kind: 'formula',
      paragraphId: null,
    });
    expect(overlayItems('target', regions, paragraphs, []).some((i) => i.inline)).toBe(false);
  });

  it('译文侧沿用段落框的坐标系', () => {
    const p = paragraph('P01-001', r(56, 510, 540, 690), { coord_system: 'image_top_left' });
    expect(overlayItems('target', [], [p], [])[0]).toMatchObject({ coordSystem: 'image_top_left' });
    expect(overlayItems('source', [], [p], [])[0]).toMatchObject({ coordSystem: 'pdf_user' });
  });

  it('隐藏类型的段落与区域都不出现', () => {
    const items = overlayItems(
      'source',
      [region('header', r(50, 760, 550, 780)), region('figure', r(50, 100, 550, 400))],
      [paragraph('P01-001', r(56, 510, 540, 690)), paragraph('P01-000', r(50, 760, 550, 780), { kind: 'header' })],
      ['header', 'figure'],
    );
    expect(items.map((i) => i.kind)).toEqual(['text']);
  });
});
