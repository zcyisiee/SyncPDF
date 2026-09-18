/**
 * 屏幕坐标 → PDF box 的逆变换单测（**W10 验收红线**）。
 *
 * `screenToPdfBox` 必须经 viewport 逆变换（pdf.js 4.10 只暴露点级 `convertToPdfPoint`，
 * 内部即 `Util.applyInverseTransform(this.transform)`），再反 cropbox 偏移与 y 翻转，
 * 与 W05 的 `pdfToScreen` **互为逆运算**。所以核心断言是 roundtrip：
 *
 *     screenToPdfBox(pdfToScreen(box, vp, cs, crop), vp, cs, crop) ≈ box
 *
 * 覆盖 scale ≠ 1、cropbox 原点 ≠ 0、rotation 0/90/180/270 —— 这些正是「假设 scale=1」
 * 或「直接减 cropbox」会写错的场景。
 */
import { describe, expect, it } from 'vitest';

import { pdfToScreen, screenToPdfBox } from '../src/components/preview/BboxLayer';
import type { Box, CoordSystem, CropBox } from '../src/lib/preview';
import { makeViewport } from './helpers';

/** 真样本（tmp/ccs3764-dyn page 1）：同一段落的 layout（pdf_native）与 parse（pdf_topleft）框。 */
const LAYOUT_BOX: Box = [66.585, 672.353, 544.64, 713.59];
const PARSE_BOX: Box = [66.585, 78.41, 544.64, 119.647];
const LETTER_CROPBOX: CropBox = { x0: 0, y0: 0, x1: 612, y1: 792 };

function expectBoxClose(actual: Box, expected: Box, digits = 6) {
  for (let index = 0; index < 4; index += 1) {
    expect(actual[index]).toBeCloseTo(expected[index], digits);
  }
}

describe('screenToPdfBox：与 pdfToScreen 互逆（roundtrip）', () => {
  const cases: { name: string; coordSystem: CoordSystem; box: Box }[] = [
    { name: 'layout（pdf_native，y 向上）', coordSystem: 'pdf_native', box: LAYOUT_BOX },
    { name: 'parse（pdf_topleft，y 向下）', coordSystem: 'pdf_topleft', box: PARSE_BOX },
  ];

  for (const { name, coordSystem, box } of cases) {
    it(`${name}：scale=1、cropbox 原点 0`, () => {
      const viewport = makeViewport({ scale: 1 });
      const rect = pdfToScreen(box, viewport, coordSystem, LETTER_CROPBOX);
      expectBoxClose(screenToPdfBox(rect, viewport, coordSystem, LETTER_CROPBOX), box);
    });

    it(`${name}：scale=1.5（逆变换不能假设 PDF 点 == CSS px）`, () => {
      const viewport = makeViewport({ scale: 1.5 });
      const rect = pdfToScreen(box, viewport, coordSystem, LETTER_CROPBOX);
      expectBoxClose(screenToPdfBox(rect, viewport, coordSystem, LETTER_CROPBOX), box);
    });

    it(`${name}：cropbox 原点 ≠ 0（偏移在换算里抵消）`, () => {
      const viewport = makeViewport({ viewBox: [10, 20, 622, 812], scale: 2 });
      const cropbox: CropBox = { x0: 10, y0: 20, x1: 622, y1: 812 };
      const rect = pdfToScreen(box, viewport, coordSystem, cropbox);
      expectBoxClose(screenToPdfBox(rect, viewport, coordSystem, cropbox), box);
    });

    for (const rotation of [90, 180, 270] as const) {
      it(`${name}：rotation=${rotation} 时交给同一个 viewport 变换`, () => {
        const viewport = makeViewport({ scale: 1.25, rotation });
        const rect = pdfToScreen(box, viewport, coordSystem, LETTER_CROPBOX);
        expectBoxClose(screenToPdfBox(rect, viewport, coordSystem, LETTER_CROPBOX), box);
      });
    }
  }
});

describe('screenToPdfBox：数值口径', () => {
  it('真实样本：拖拽矩形回到 layout_box 原值（y 向上、y2 是框顶）', () => {
    const viewport = makeViewport({ scale: 1 });
    const rect = pdfToScreen(LAYOUT_BOX, viewport, 'pdf_native', LETTER_CROPBOX);
    // 屏幕矩形与真值：x=66.585, y=78.41, w=478.055, h=41.237
    expect(rect.x).toBeCloseTo(66.585, 6);
    expect(rect.y).toBeCloseTo(78.41, 6);
    expect(screenToPdfBox(rect, viewport, 'pdf_native', LETTER_CROPBOX)).toEqual(LAYOUT_BOX);
  });

  it('屏幕矩形向下移 30px（scale=1）→ box 的 y 与 y2 都小 30（y 向上）', () => {
    const viewport = makeViewport({ scale: 1 });
    const rect = pdfToScreen(LAYOUT_BOX, viewport, 'pdf_native', LETTER_CROPBOX);
    const moved = { ...rect, y: rect.y + 30 };
    const box = screenToPdfBox(moved, viewport, 'pdf_native', LETTER_CROPBOX);
    expect(box[0]).toBeCloseTo(LAYOUT_BOX[0], 6);
    expect(box[1]).toBeCloseTo(LAYOUT_BOX[1] - 30, 6);
    expect(box[2]).toBeCloseTo(LAYOUT_BOX[2], 6);
    expect(box[3]).toBeCloseTo(LAYOUT_BOX[3] - 30, 6);
  });

  it('输出总是升序且满足服务端要求（x2 > x、y2 > y）', () => {
    const viewport = makeViewport({ scale: 1 });
    const box = screenToPdfBox({ x: 100, y: 200, width: 120, height: 40 }, viewport, 'pdf_native', LETTER_CROPBOX);
    expect(box[2]).toBeGreaterThan(box[0]);
    expect(box[3]).toBeGreaterThan(box[1]);
    expect(box).toEqual([100, 552, 220, 592]);
  });

  it('没有 page_info 的 cropbox 兜底走 viewport 的 viewBox（与 pdfToScreen 同一来源）', () => {
    const viewport = makeViewport({ scale: 2 });
    const rect = pdfToScreen(LAYOUT_BOX, viewport, 'pdf_native');
    expectBoxClose(screenToPdfBox(rect, viewport, 'pdf_native'), LAYOUT_BOX);
  });

  it('rotation=90 下拖拽 +20px 横向 → box 的 y 抬升 20（变换把轴换掉了）', () => {
    const viewport = makeViewport({ scale: 1, rotation: 90 });
    const rect = pdfToScreen(LAYOUT_BOX, viewport, 'pdf_native', LETTER_CROPBOX);
    const moved = { ...rect, x: rect.x + 20 };
    const box = screenToPdfBox(moved, viewport, 'pdf_native', LETTER_CROPBOX);
    expect(box[0]).toBeCloseTo(LAYOUT_BOX[0], 6);
    expect(box[1]).toBeCloseTo(LAYOUT_BOX[1] + 20, 6);
    expect(box[2]).toBeCloseTo(LAYOUT_BOX[2], 6);
    expect(box[3]).toBeCloseTo(LAYOUT_BOX[3] + 20, 6);
  });
});
