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
import { categoryColor, categoryLabel, categoryVisible, DEFAULT_VISIBILITY, type BboxVisibility } from '../../lib/bbox';
import { cn } from '../../lib/cn';

/** pdf.js `PageViewport` 的结构子集（只用到换算需要的部分，便于单测注入替身）。 */
export interface ScreenViewport {
  /** 视口宽（CSS px，已含 scale/rotation 的结果）。 */
  readonly width: number;
  readonly height: number;
  /** CSS px / PDF 点（pdf.js viewport 的 `scale`）。 */
  readonly scale: number;
  /** 页面旋转角度（0/90/180/270）；换算不需要它（变换矩阵已含），仅用于提示。 */
  readonly rotation?: number;
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

/**
 * 逆变换所需的视口子集（真 `PageViewport` 天然满足）。
 *
 * **pdf.js 4.10 的 `PageViewport` 只暴露点级逆变换 `convertToPdfPoint(x, y)`**
 * （内部就是 `Util.applyInverseTransform([x, y], this.transform)`），没有
 * `convertToPdfRectangle`；所以逆变换按矩形的两个对角点各调一次，再在
 * :func:`screenToPdfBox` 里按 min/max 归一。绝不假设 scale=1，也不直接减 cropbox。
 */
export interface PdfPointViewport {
  convertToPdfPoint(x: number, y: number): number[];
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

/**
 * :func:`pdfToScreen` 的**逆变换**：拖拽后的屏幕矩形 → 输入坐标系的 `[x, y, x2, y2]`
 * （升序、`x2 > x` / `y2 > y`，正是服务端 `layout.box` 要求的形状）。
 *
 * 两步的逆序执行：
 * 1. **视口逆变换**：两个对角点 → PDF user space（含 scale/rotation/viewBox 偏移的完整变换，
 *    与正向共用同一个 viewport，所以 `screenToPdfBox(pdfToScreen(box)) ≈ box`）；
 * 2. **反 cropbox 偏移 + 反 y 翻转**：`pdf_native`（y 向上）直接减 `cropbox` 原点；
 *    `pdf_topleft`（y 向下）用 `cropbox.y1 - y` 翻回去。
 *
 * 视口坐标 y 向下、PDF user space y 向上，所以「屏幕矩形上边」对应 user space 里更大的 y：
 * 排序时 `y2 = max(uy)`、`y = min(uy)`。
 */
export function screenToPdfBox(
  rect: ScreenRect,
  viewport: ScreenViewport & PdfPointViewport,
  coordSystem: CoordSystem,
  cropbox?: CropBox | null,
): Box {
  const crop = cropbox ?? cropBoxFromViewport(viewport);
  const [ax, ay] = viewport.convertToPdfPoint(rect.x, rect.y);
  const [bx, by] = viewport.convertToPdfPoint(rect.x + rect.width, rect.y + rect.height);
  const left = Math.min(ax, bx) - crop.x0;
  const right = Math.max(ax, bx) - crop.x0;
  const top = Math.max(ay, by);
  const bottom = Math.min(ay, by);
  if (coordSystem === 'pdf_topleft') {
    return [left, crop.y1 - top, right, crop.y1 - bottom];
  }
  return [left, bottom - crop.y0, right, top - crop.y0];
}

/** 段落选中回调；`shift` = 多选修饰（只在真实鼠标点击时带，键盘激活不传）。 */
export type BboxSelectHandler = (id: string, opts?: { shift?: boolean }) => void;

export interface BboxLayerProps {
  /** 当前页的 bbox（输入坐标系，来自 geometry 响应）。 */
  boxes: readonly BboxItem[];
  /** 该页渲染 viewport（与 canvas 共用同一个，保证叠加层像素对齐）。 */
  viewport: ScreenViewport;
  /**
   * 叠加层语义（也是 ``data-bbox-mode`` 与无障碍名的来源）：
   * `parse` = 源侧识别框（`pdf_topleft`）/ `layout` = 译文套版几何框（`pdf_native`，可拖拽）
   * / `target` = 译文侧重新识别的版面框（`pdf_topleft`，只读）。
   */
  mode: 'parse' | 'layout' | 'target';
  cropbox?: CropBox | null;
  /** 单选判定（多选集合未传时的回退路径，样式与旧版一致）。 */
  selectedId?: string | null;
  /** shift 多选集合（按点击顺序）；给定时覆盖 `selectedId` 的单选判定。 */
  selectedIds?: readonly string[];
  onSelect?: BboxSelectHandler;
  className?: string;
  visibility?: BboxVisibility;
  strokeWidth?: number;
  fillOpacity?: number;
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
  selectedIds,
  onSelect,
  className,
  visibility = DEFAULT_VISIBILITY,
  strokeWidth = 1.5,
  fillOpacity = 0.08,
}: BboxLayerProps) {
  const coordSystem = coordSystemOfMode(mode);
  const visible = boxes.filter((item) => categoryVisible(visibility, item.label));
  const blocks = new Set(visible.filter((item) => item.kind === 'block').map((item) => item.id));
  // A visible parent already outlines its text. Keep formula/image/table spans independent.
  const rects = visible.filter((item) => !(item.kind === 'span' && item.label === 'text'
    && item.parentId && blocks.has(item.parentId))).map((item) => ({
    item,
    rect: pdfToScreen(item.box, viewport, coordSystem, cropbox),
  }));
  if (rects.length === 0) return null;
  const layerLabel =
    mode === 'layout' ? '版面框' : mode === 'target' ? '译文识别框' : '识别框';
  // 主选中 = 多选集合的最后一个（与 store 的 selectedParagraphId 同步）；未传集合时单选即主选中
  const primaryId = selectedIds !== undefined ? selectedIds[selectedIds.length - 1] ?? null : selectedId;
  const multi = selectedIds !== undefined && selectedIds.length > 1;
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
        const targetId = item.paragraphId === undefined ? item.id : item.paragraphId;
        const selectable = targetId !== null;
        const selected =
          selectable && (selectedIds !== undefined ? selectedIds.includes(targetId) : targetId === selectedId);
        // 多选 >1 时非主选中用更轻的虚线变体，主选中（右栏跟随的那段）保持单选样式
        const secondary = selected && multi && targetId !== primaryId;
        return (
          <rect
            key={item.id}
            data-od-id={`bbox-${item.id}`}
            data-bbox-id={item.id}
            data-bbox-label={item.label ?? ''}
            data-bbox-kind={item.kind ?? 'paragraph'}
            role={selectable ? 'button' : 'img'}
            tabIndex={selectable ? 0 : undefined}
            aria-pressed={selectable ? selected : undefined}
            aria-label={`${selectable ? `段落 ${targetId}` : item.kind ?? 'bbox'} · ${categoryLabel(item.label)}`}
            x={rect.x}
            y={rect.y}
            width={rect.width}
            height={rect.height}
            rx={3}
            stroke={categoryColor(item.label)}
            fill={categoryColor(item.label)}
            fillOpacity={fillOpacity}
            strokeWidth={selected ? (secondary ? strokeWidth : strokeWidth + 1) : strokeWidth}
            strokeDasharray={selected ? (secondary ? '2 2' : '4 2') : undefined}
            className={selectable ? 'pointer-events-auto cursor-pointer transition-colors' : 'pointer-events-none'}
            onClick={(event) => {
              if (targetId === null) return;
              // 只有真实 shift 点击才带修饰（键盘 Enter/Space 等同普通单选）
              if (event.shiftKey) onSelect?.(targetId, { shift: true });
              else onSelect?.(targetId);
            }}
            onKeyDown={(event) => {
              if (targetId === null || (event.key !== 'Enter' && event.key !== ' ')) return;
              event.preventDefault();
              onSelect?.(targetId);
            }}
          >
            <title>{`${item.id}${item.label === null ? '' : ` · ${item.label}`}`}</title>
          </rect>
        );
      })}
    </svg>
  );
}
