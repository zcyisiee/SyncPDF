/**
 * bbox SVG 叠加层 + 坐标系换算（**契约红线**：服务端两套坐标系统一不转换，换算全在前端）。
 *
 * `pdfToScreen` 是本模块的核心纯函数：把 geometry 端点给回的 bbox 换算成预览画布上的
 * CSS px 矩形。换算分两步——
 *
 * 1. 输入坐标 → **PDF user space**（cropbox 左下原点、y 向上）：
 *    - `pdf_topleft`（parse 快照）：`x_user = cropbox.x0 + x`，`y_user = cropbox.y1 - y`
 *      （y 向下 → 翻转；cropbox 原点非 0 时靠 x0/y1 归位）；
 *    - `pdf_native`（layout 几何）：`x_user = cropbox.x0 + x`，`y_user = cropbox.y0 + y`
 *      （已是 y 向上，只补 cropbox 原点偏移）。
 *    两者在同一段上必须落回同一个 user space 矩形——样本 21 页 PDF 里 parse 与 layout
 *    给出的 P01-001 框正好互为 `792 - y` 镜像（见 tests/preview-coords.test.ts）。
 * 2. PDF user space → 屏幕：交给 pdf.js `viewport.convertToViewportRectangle`，
 *    它包含 scale / rotation / viewBox 偏移的**完整变换**，所以不要假设「PDF 点 == CSS px」，
 *    也不要自己算 scale。
 */
import type { BboxItem, Box, CoordSystem, CropBox } from '../../lib/preview';
import { coordSystemOfMode } from '../../lib/preview';
import { cn } from '../../lib/cn';

/** pdf.js `PageViewport` 的结构子集（只用到换算需要的部分，便于单测注入替身）。 */
export interface ScreenViewport {
  /** 视口宽（CSS px，已含 scale/rotation 的结果）。 */
  readonly width: number;
  readonly height: number;
  /** CSS px / PDF 点（pdf.js viewport 的 `scale`）。 */
  readonly scale: number;
  /** 页面 viewBox（= cropbox，PDF user space 绝对坐标，未乘 scale）。 */
  readonly viewBox?: readonly number[];
  /** PDF user space → 视口坐标（左上原点、y 向下）的完整变换。 */
  convertToViewportRectangle(rect: number[]): number[];
}

export interface ScreenRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

/**
 * 没有 `page_info` 时的 cropbox 兜底：能用 pdf.js viewport 的 `viewBox`（就是 cropbox）
 * 就用它；拿不到 viewBox（单测替身）才退回「原点 0、页高 = 视口高 / scale」。
 * 后者只在 rotation=0 且 cropbox origin=0 时成立（样本全是 rotation=0；rotation≠0 时预览区显示警告条）。
 */
export function cropBoxFromViewport(viewport: ScreenViewport): CropBox {
  const viewBox = viewport.viewBox;
  if (viewBox !== undefined && viewBox.length === 4) {
    const [x0, y0, x1, y1] = viewBox.map(Number);
    return { x0, y0, x1, y1 };
  }
  return {
    x0: 0,
    y0: 0,
    x1: viewport.width / viewport.scale,
    y1: viewport.height / viewport.scale,
  };
}

/** bbox（输入坐标系）→ 预览画布上的 CSS px 矩形。纯函数，rotation 交给 pdf.js 变换处理。 */
export function pdfToScreen(
  box: Box,
  viewport: ScreenViewport,
  coordSystem: CoordSystem,
  cropbox?: CropBox | null,
): ScreenRect {
  const crop = cropbox ?? cropBoxFromViewport(viewport);
  const left = crop.x0 + Math.min(box[0], box[2]);
  const right = crop.x0 + Math.max(box[0], box[2]);
  const top =
    coordSystem === 'pdf_topleft'
      ? crop.y1 - Math.min(box[1], box[3])
      : crop.y0 + Math.max(box[1], box[3]);
  const bottom =
    coordSystem === 'pdf_topleft'
      ? crop.y1 - Math.max(box[1], box[3])
      : crop.y0 + Math.min(box[1], box[3]);
  const [ax, ay, bx, by] = viewport.convertToViewportRectangle([left, top, right, bottom]);
  return {
    x: Math.min(ax, bx),
    y: Math.min(ay, by),
    width: Math.abs(bx - ax),
    height: Math.abs(by - ay),
  };
}

export interface BboxLayerProps {
  /** 当前页的 bbox（输入坐标系，来自 geometry 响应）。 */
  boxes: readonly BboxItem[];
  /** 该页渲染 viewport（与 canvas 共用同一个，保证叠加层像素对齐）。 */
  viewport: ScreenViewport;
  /** `parse` = 识别框（pdf_topleft）/ `layout` = 版面框（pdf_native）。 */
  mode: 'parse' | 'layout';
  cropbox?: CropBox | null;
  selectedId?: string | null;
  onSelect?: (id: string) => void;
  className?: string;
}

/**
 * 绝对定位在 canvas 之上的 SVG 层：只有 rect 命中点击（拖拽工具 W09 才需要更复杂的
 * 命中策略，所以 svg 根 `pointer-events-none`、rect `pointer-events-auto`）。
 */
export function BboxLayer({
  boxes,
  viewport,
  mode,
  cropbox,
  selectedId,
  onSelect,
  className,
}: BboxLayerProps) {
  const coordSystem = coordSystemOfMode(mode);
  const rects = boxes.map((item) => ({
    item,
    rect: pdfToScreen(item.box, viewport, coordSystem, cropbox),
  }));
  if (rects.length === 0) return null;
  const layerLabel = mode === 'parse' ? '识别框' : '版面框';
  return (
    <svg
      className={cn('pointer-events-none absolute left-0 top-0', className)}
      width={viewport.width}
      height={viewport.height}
      viewBox={`0 0 ${viewport.width} ${viewport.height}`}
      role="group"
      aria-label={`${layerLabel}叠加层（${rects.length} 个）`}
      data-od-id="bbox-layer"
      data-bbox-mode={mode}
    >
      {rects.map(({ item, rect }) => {
        const selected = item.id === selectedId;
        return (
          <rect
            key={item.id}
            data-od-id={`bbox-${item.id}`}
            data-bbox-id={item.id}
            role="button"
            tabIndex={0}
            aria-pressed={selected}
            aria-label={`段落 ${item.id}${item.label === null ? '' : ` · ${item.label}`}`}
            x={rect.x}
            y={rect.y}
            width={rect.width}
            height={rect.height}
            strokeWidth={selected ? 2 : 1}
            className={cn(
              'pointer-events-auto cursor-pointer transition-colors',
              selected
                ? 'fill-tint-2 stroke-accent'
                : 'fill-tint stroke-transparent hover:fill-tint-2 hover:stroke-accent',
            )}
            onClick={() => onSelect?.(item.id)}
            onKeyDown={(event) => {
              if (event.key !== 'Enter' && event.key !== ' ') return;
              event.preventDefault();
              onSelect?.(item.id);
            }}
          >
            <title>{`${item.id}${item.label === null ? '' : ` · ${item.label}`}`}</title>
          </rect>
        );
      })}
    </svg>
  );
}
