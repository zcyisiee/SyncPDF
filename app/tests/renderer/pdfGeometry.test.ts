/**
 * BoxLayer 坐标变换单测（M2-06 验收）：给定 pdf.js viewport 的 transform 矩阵，
 * 验证 pdf_native / pdf_topleft 两种坐标系都落到正确的画布像素位置。
 * @vitest-environment node
 */
import { describe, expect, it } from 'vitest';
import {
  applyTransform,
  boxToViewRect,
  canvasPixelSize,
  isValidBox,
  normalizeBox,
  toMatrix,
  topLeftToNative,
  unionRects,
  type ViewportLike,
} from '../../src/renderer/src/pdf/geometry';
import { layoutBoxes } from '../../src/renderer/src/pdf/BoxLayer';
import type { Box } from '../../src/shared/protocol';

/**
 * 复刻 pdf.js `PageViewport` 构造出的 transform（rotation=0 / 90 / 180 / 270）。
 * 与 node_modules/pdfjs-dist/build/pdf.mjs 的 PageViewport 构造完全同式。
 */
function makeViewport(
  viewBox: [number, number, number, number],
  scale: number,
  rotation: number,
): ViewportLike {
  const centerX = (viewBox[2] + viewBox[0]) / 2;
  const centerY = (viewBox[3] + viewBox[1]) / 2;
  let rotateA: number;
  let rotateB: number;
  let rotateC: number;
  let rotateD: number;
  switch (((rotation % 360) + 360) % 360) {
    case 180:
      [rotateA, rotateB, rotateC, rotateD] = [-1, 0, 0, 1];
      break;
    case 90:
      [rotateA, rotateB, rotateC, rotateD] = [0, 1, 1, 0];
      break;
    case 270:
      [rotateA, rotateB, rotateC, rotateD] = [0, -1, -1, 0];
      break;
    default:
      [rotateA, rotateB, rotateC, rotateD] = [1, 0, 0, -1];
  }
  let offsetCanvasX: number;
  let offsetCanvasY: number;
  let width: number;
  let height: number;
  if (rotateA === 0) {
    offsetCanvasX = Math.abs(centerY - viewBox[1]) * scale;
    offsetCanvasY = Math.abs(centerX - viewBox[0]) * scale;
    width = (viewBox[3] - viewBox[1]) * scale;
    height = (viewBox[2] - viewBox[0]) * scale;
  } else {
    offsetCanvasX = Math.abs(centerX - viewBox[0]) * scale;
    offsetCanvasY = Math.abs(centerY - viewBox[1]) * scale;
    width = (viewBox[2] - viewBox[0]) * scale;
    height = (viewBox[3] - viewBox[1]) * scale;
  }
  return {
    viewBox,
    width,
    height,
    transform: [
      rotateA * scale,
      rotateB * scale,
      rotateC * scale,
      rotateD * scale,
      offsetCanvasX - rotateA * scale * centerX - rotateC * scale * centerY,
      offsetCanvasY - rotateB * scale * centerX - rotateD * scale * centerY,
    ],
  };
}

const LETTER: [number, number, number, number] = [0, 0, 612, 792];

describe('applyTransform / toMatrix', () => {
  it('单位矩阵不变，平移正确', () => {
    expect(applyTransform(3, 4, [1, 0, 0, 1, 0, 0])).toEqual([3, 4]);
    expect(applyTransform(3, 4, [1, 0, 0, 1, 10, 20])).toEqual([13, 24]);
  });

  it('toMatrix 对非法输入退化为单位矩阵', () => {
    expect(toMatrix([1, 2, 3])).toEqual([1, 0, 0, 1, 0, 0]);
    expect(toMatrix([1, 0, 0, 1, Number.NaN, 0])).toEqual([1, 0, 0, 1, 0, 0]);
    expect(toMatrix([2, 0, 0, -2, 0, 100])).toEqual([2, 0, 0, -2, 0, 100]);
  });
});

describe('boxToViewRect — pdf_native（y 由 transform 翻转）', () => {
  it('scale=1 的 Letter 页：transform 为 [1,0,0,-1,0,792]', () => {
    const viewport = makeViewport(LETTER, 1, 0);
    expect(viewport.transform).toEqual([1, 0, 0, -1, 0, 792]);
    // 用户空间 y=700..720（靠页面上方）→ 画布 top=72，高 20
    const rect = boxToViewRect([72, 700, 540, 720], 'pdf_native', viewport);
    expect(rect).toEqual({ left: 72, top: 72, width: 468, height: 20 });
  });

  it('scale=2 时矩形等比放大', () => {
    const viewport = makeViewport(LETTER, 2, 0);
    const rect = boxToViewRect([72, 700, 540, 720], 'pdf_native', viewport);
    expect(rect).toEqual({ left: 144, top: 144, width: 936, height: 40 });
  });

  it('框方向颠倒（y1 < y0）也得到非负宽高', () => {
    const viewport = makeViewport(LETTER, 1, 0);
    const normal = boxToViewRect([72, 700, 540, 720], 'pdf_native', viewport);
    const flipped = boxToViewRect([540, 720, 72, 700], 'pdf_native', viewport);
    expect(flipped).toEqual(normal);
  });

  it('非零 viewBox 原点（裁剪框不在 0,0）', () => {
    const viewport = makeViewport([20, 30, 620, 830], 1, 0);
    // 页高 800；y=830 是页顶 → top=0
    const rect = boxToViewRect([20, 830, 620, 730], 'pdf_native', viewport);
    expect(rect.left).toBeCloseTo(0, 6);
    expect(rect.top).toBeCloseTo(0, 6);
    expect(rect.width).toBeCloseTo(600, 6);
    expect(rect.height).toBeCloseTo(100, 6);
  });

  it('rotation=90 时框跟着转（宽高互换）', () => {
    const viewport = makeViewport(LETTER, 1, 90);
    expect(viewport.width).toBe(792);
    expect(viewport.height).toBe(612);
    const rect = boxToViewRect([72, 700, 540, 720], 'pdf_native', viewport);
    // 旋转 90° 后原来的"高 20 宽 468"变成"宽 20 高 468"
    expect(rect.width).toBeCloseTo(20, 6);
    expect(rect.height).toBeCloseTo(468, 6);
  });

  it('rotation=180：x 镜像、y 不再翻转（页整体倒过来）', () => {
    const viewport = makeViewport(LETTER, 1, 180);
    // transform = [-1,0,0,1,612,0]：x' = 612-x，y' = y
    expect(viewport.transform).toEqual([-1, 0, 0, 1, 612, 0]);
    const rect = boxToViewRect([72, 700, 540, 720], 'pdf_native', viewport);
    expect(rect.left).toBeCloseTo(612 - 540, 6);
    // 未旋转时靠页顶的内容，倒过来后落在画布下方
    expect(rect.top).toBeCloseTo(700, 6);
    expect(rect.width).toBeCloseTo(468, 6);
    expect(rect.height).toBeCloseTo(20, 6);
  });
});

describe('boxToViewRect — pdf_topleft（先折回用户空间）', () => {
  it('页左上原点 y 向下：与等价的 pdf_native 框结果一致', () => {
    const viewport = makeViewport(LETTER, 1, 0);
    // 同一块区域：topleft (72,72)-(540,92) ≡ native (72,720)-(540,700)
    const topLeft = boxToViewRect([72, 72, 540, 92], 'pdf_topleft', viewport);
    const native = boxToViewRect([72, 720, 540, 700], 'pdf_native', viewport);
    expect(topLeft).toEqual(native);
    expect(topLeft).toEqual({ left: 72, top: 72, width: 468, height: 20 });
  });

  it('topLeftToNative 用 viewBox 的 xMin/yMax 作原点', () => {
    expect(topLeftToNative([0, 0, 10, 10], [20, 30, 620, 830])).toEqual([20, 830, 30, 820]);
  });

  it('scale=1.5 的 topleft 框', () => {
    const viewport = makeViewport(LETTER, 1.5, 0);
    const rect = boxToViewRect([100, 200, 300, 260], 'pdf_topleft', viewport);
    expect(rect.left).toBeCloseTo(150, 6);
    expect(rect.top).toBeCloseTo(300, 6);
    expect(rect.width).toBeCloseTo(300, 6);
    expect(rect.height).toBeCloseTo(90, 6);
  });
});

describe('辅助函数', () => {
  it('normalizeBox 排序坐标', () => {
    expect(normalizeBox([5, 9, 1, 2])).toEqual([1, 2, 5, 9]);
  });

  it('isValidBox 拒绝 null / 长度不符 / NaN', () => {
    expect(isValidBox(null)).toBe(false);
    expect(isValidBox([1, 2, 3])).toBe(false);
    expect(isValidBox([1, 2, 3, Number.NaN])).toBe(false);
    expect(isValidBox([1, 2, 3, 4])).toBe(true);
  });

  it('unionRects 求外接矩形', () => {
    expect(
      unionRects([
        { left: 10, top: 10, width: 20, height: 5 },
        { left: 5, top: 30, width: 10, height: 5 },
      ]),
    ).toEqual({ left: 5, top: 10, width: 25, height: 25 });
    expect(unionRects([])).toBeNull();
  });

  it('canvasPixelSize 按 DPR 放大并在超大页时收敛', () => {
    expect(canvasPixelSize(100, 200, 2)).toEqual({ width: 200, height: 400, ratio: 2 });
    expect(canvasPixelSize(100, 200, 0)).toEqual({ width: 100, height: 200, ratio: 1 });
    const huge = canvasPixelSize(5000, 5000, 3, 1_000_000);
    expect(huge.ratio).toBeLessThan(3);
    expect(huge.width * huge.height).toBeLessThanOrEqual(1_000_001);
  });
});

describe('layoutBoxes（BoxLayer 输入 → 矩形）', () => {
  it('过滤非法框与零面积框，保留段落对应关系', () => {
    const viewport = makeViewport(LETTER, 1, 0);
    const boxes: Box[] = [
      [72, 700, 540, 720],
      [72, 700, 72, 700], // 零面积
      [1, 2, 3] as unknown as Box, // 非法
    ];
    const laid = layoutBoxes(
      [{ id: 'P01-001', boxes, coordSystem: 'pdf_native', status: 'translated' }],
      viewport,
    );
    expect(laid).toHaveLength(1);
    expect(laid[0].paragraph.id).toBe('P01-001');
    expect(laid[0].rects).toEqual([{ left: 72, top: 72, width: 468, height: 20 }]);
  });
});
