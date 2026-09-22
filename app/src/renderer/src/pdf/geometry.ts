/**
 * 段落框坐标变换（规约 #6：PDF 用户空间 → 画布像素的换算只在这里做一次）。
 *
 * pdf.js 6 起 `PageViewport.convertToViewportRectangle` 已移除，因此这里直接用
 * `page.getViewport({ scale }).transform`（6 元仿射矩阵 `[a,b,c,d,e,f]`）做换算：
 *
 * ```
 * x' = a*x + c*y + e
 * y' = b*x + d*y + f
 * ```
 *
 * rotation=0 的普通页里 `d = -scale`，即 **y 轴在这一步被翻转**
 * （PDF 用户空间左下原点、y 向上；画布左上原点、y 向下），
 * 所以 `coord_system === 'pdf_native'` 的框不需要我们再手工翻 y——
 * 交给 transform，旋转页（90/180/270）才能一并正确。
 *
 * `coord_system === 'pdf_topleft'` 的框是"页左上原点、y 向下"，先按 viewBox
 * 折回用户空间再走同一条 transform 路径。
 *
 * 本模块是纯函数（不 import pdfjs），可在 vitest 里直接喂矩阵做单测。
 */
import type { Box, CoordSystem } from '@shared/protocol';

/** pdf.js 的 6 元仿射矩阵。 */
export type Matrix = readonly [number, number, number, number, number, number];

/** `PageViewport` 中本模块用到的部分（便于测试构造假对象）。 */
export interface ViewportLike {
  /** `[a,b,c,d,e,f]`。 */
  transform: number[];
  /** 视口宽（CSS px）。 */
  width: number;
  /** 视口高（CSS px）。 */
  height: number;
  /** `[xMin, yMin, xMax, yMax]`（PDF 用户空间）。 */
  viewBox: number[];
}

/** 画布 / CSS 像素矩形（左上原点）。 */
export interface ViewRect {
  left: number;
  top: number;
  width: number;
  height: number;
}

/** 矩阵非法时的兜底单位矩阵。 */
const IDENTITY: Matrix = [1, 0, 0, 1, 0, 0];

/** `number[]` → `Matrix`（长度不足时退化为单位矩阵，避免 NaN 扩散）。 */
export function toMatrix(transform: readonly number[]): Matrix {
  if (transform.length < 6) return IDENTITY;
  const values = transform.slice(0, 6);
  if (values.some((value) => !Number.isFinite(value))) return IDENTITY;
  return values as unknown as Matrix;
}

/** 点变换：`[x, y]` → `[x', y']`。 */
export function applyTransform(x: number, y: number, matrix: Matrix): [number, number] {
  const [a, b, c, d, e, f] = matrix;
  return [a * x + c * y + e, b * x + d * y + f];
}

/** 把任意方向的 bbox 归一成 `[minX, minY, maxX, maxY]`。 */
export function normalizeBox(box: Box): Box {
  const [x0, y0, x1, y1] = box;
  return [Math.min(x0, x1), Math.min(y0, y1), Math.max(x0, x1), Math.max(y0, y1)];
}

/**
 * `pdf_topleft` → `pdf_native`。
 * 页左上角在用户空间是 `(viewBox[0], viewBox[3])`，y 向下为正，故
 * `x_native = xMin + x`，`y_native = yMax - y`。
 */
export function topLeftToNative(box: Box, viewBox: readonly number[]): Box {
  const xMin = viewBox[0] ?? 0;
  const yMax = viewBox[3] ?? 0;
  const [x0, y0, x1, y1] = box;
  return [xMin + x0, yMax - y0, xMin + x1, yMax - y1];
}

/** 框是否可用（4 个有限数）。 */
export function isValidBox(box: readonly number[] | null | undefined): box is Box {
  return (
    Array.isArray(box) &&
    box.length === 4 &&
    box.every((value) => typeof value === 'number' && Number.isFinite(value))
  );
}

/**
 * 段落框（协议坐标系）→ 视口矩形（CSS px，左上原点）。
 * 返回的 width/height 恒为非负。
 */
export function boxToViewRect(
  box: Box,
  coordSystem: CoordSystem,
  viewport: ViewportLike,
): ViewRect {
  const native = coordSystem === 'pdf_topleft' ? topLeftToNative(box, viewport.viewBox) : box;
  const matrix = toMatrix(viewport.transform);
  const [ax, ay] = applyTransform(native[0], native[1], matrix);
  const [bx, by] = applyTransform(native[2], native[3], matrix);
  const left = Math.min(ax, bx);
  const top = Math.min(ay, by);
  return {
    left,
    top,
    width: Math.abs(bx - ax),
    height: Math.abs(by - ay),
  };
}

/** 多个框的外接矩形（段落跨栏 / 跨行时用来做"滚动到段落"）。 */
export function unionRects(rects: readonly ViewRect[]): ViewRect | null {
  if (rects.length === 0) return null;
  let left = Number.POSITIVE_INFINITY;
  let top = Number.POSITIVE_INFINITY;
  let right = Number.NEGATIVE_INFINITY;
  let bottom = Number.NEGATIVE_INFINITY;
  for (const rect of rects) {
    left = Math.min(left, rect.left);
    top = Math.min(top, rect.top);
    right = Math.max(right, rect.left + rect.width);
    bottom = Math.max(bottom, rect.top + rect.height);
  }
  return { left, top, width: right - left, height: bottom - top };
}

/**
 * 画布位图尺寸：CSS 尺寸 × devicePixelRatio，并夹到 `maxPixels`
 * （超大页 / 高倍屏下避免一次分配上百 MB 的位图）。
 * 返回的 `ratio` 是实际用于 `ctx.scale` 的倍率。
 */
export function canvasPixelSize(
  cssWidth: number,
  cssHeight: number,
  devicePixelRatio: number,
  maxPixels = 16_000_000,
): { width: number; height: number; ratio: number } {
  const dpr = Number.isFinite(devicePixelRatio) && devicePixelRatio > 0 ? devicePixelRatio : 1;
  const area = cssWidth * cssHeight * dpr * dpr;
  const ratio = area > maxPixels ? dpr * Math.sqrt(maxPixels / area) : dpr;
  return {
    width: Math.max(1, Math.floor(cssWidth * ratio)),
    height: Math.max(1, Math.floor(cssHeight * ratio)),
    ratio,
  };
}
