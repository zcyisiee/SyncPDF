/**
 * bbox 拖拽编辑器：叠加在 `BboxLayer` 之上，只对**选中段**生效（翻译等 layout 视图）。
 *
 * 交互（DESIGN.md §4.5「选中 = 2px 墨蓝 outline + 8 个 6×6 拖拽手柄」）：
 * - 8 个手柄（四角 + 四边中点）与框体本身都可拖（Pointer Events + `setPointerCapture`，
 *   与 `Gutter` 同一套拖拽模式）；
 * - 拖拽期间只更新**屏幕矩形**（实时预览），松手才做 `screenToPdfBox` 逆变换并回调
 *   `onCommit(box)`（父级据此 PATCH 草稿的 `layout.box`）——拖拽过程不发请求；
 * - 拖拽期间在整块视口上盖一层透明命中层（`data-od-id="bbox-editor-shield"`）+
 *   指针捕获，双保险挡住底层 BboxLayer 的 rect 点击（不会误选到别的段）；
 * - ESC 取消本次拖拽（回到拖拽前的矩形，不回调）。
 *
 * 坐标换算**只在**纯函数 `screenToPdfBox` 里做（view口逆变换 + y 向上排序），
 * 这里不出现任何 scale=1 假设或手写加减 cropbox 的逻辑。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import type { PointerEvent as ReactPointerEvent } from 'react';

import type { Box, CropBox } from '../../lib/preview';
import { cn } from '../../lib/cn';
import { screenToPdfBox, type PdfPointViewport, type ScreenRect, type ScreenViewport } from '../preview/BboxLayer';

/** 八个手柄：四角 + 四边中点（§4.5）。`nw` = 左上，`e` = 右边中点…… */
export const HANDLES = ['nw', 'n', 'ne', 'e', 'se', 's', 'sw', 'w'] as const;
export type HandleId = (typeof HANDLES)[number];

/** 手柄对应的 CSS 光标（用系统光标，不引第二套视觉）。 */
const HANDLE_CURSORS: Record<HandleId, string> = {
  nw: 'nwse-resize',
  n: 'ns-resize',
  ne: 'nesw-resize',
  e: 'ew-resize',
  se: 'nwse-resize',
  s: 'ns-resize',
  sw: 'nesw-resize',
  w: 'ew-resize',
};

/** 拖拽后允许的最小边长（CSS px）：太小的框无法再抓回来。 */
export const MIN_BOX_PX = 8;

/**
 * 纯函数：手柄拖拽 `(dx, dy)` 后的新屏幕矩形。
 * 只动被拖的边（`nw` 动左上两条边、`e` 只动右边……）；`body` 整体平移。
 * 越过对边时按 `MIN_BOX_PX` 夹住（不翻转、不留 0 宽高的死框）。
 */
export function resizeBox(
  rect: ScreenRect,
  handle: HandleId | 'body',
  dx: number,
  dy: number,
): ScreenRect {
  if (handle === 'body') return { ...rect, x: rect.x + dx, y: rect.y + dy };
  const left = rect.x;
  const top = rect.y;
  const right = rect.x + rect.width;
  const bottom = rect.y + rect.height;
  const west = handle === 'nw' || handle === 'w' || handle === 'sw';
  const east = handle === 'ne' || handle === 'e' || handle === 'se';
  const north = handle === 'nw' || handle === 'n' || handle === 'ne';
  const south = handle === 'sw' || handle === 's' || handle === 'se';
  const nextLeft = west ? Math.min(left + dx, right - MIN_BOX_PX) : left;
  const nextRight = east ? Math.max(right + dx, left + MIN_BOX_PX) : right;
  const nextTop = north ? Math.min(top + dy, bottom - MIN_BOX_PX) : top;
  const nextBottom = south ? Math.max(bottom + dy, top + MIN_BOX_PX) : bottom;
  return {
    x: nextLeft,
    y: nextTop,
    width: nextRight - nextLeft,
    height: nextBottom - nextTop,
  };
}

/** 手柄在矩形上的锚点（手柄中心，视口坐标）。 */
function handlePoint(rect: ScreenRect, handle: HandleId): { cx: number; cy: number } {
  const midX = rect.x + rect.width / 2;
  const midY = rect.y + rect.height / 2;
  const right = rect.x + rect.width;
  const bottom = rect.y + rect.height;
  switch (handle) {
    case 'nw':
      return { cx: rect.x, cy: rect.y };
    case 'n':
      return { cx: midX, cy: rect.y };
    case 'ne':
      return { cx: right, cy: rect.y };
    case 'e':
      return { cx: right, cy: midY };
    case 'se':
      return { cx: right, cy: bottom };
    case 's':
      return { cx: midX, cy: bottom };
    case 'sw':
      return { cx: rect.x, cy: bottom };
    default:
      return { cx: rect.x, cy: midY };
  }
}

export interface BboxEditorProps {
  /** 选中段的 id（用于 data-od-id / aria-label）。 */
  id: string;
  /** 选中段的初始屏幕矩形（由 `pdfToScreen(item.box, viewport, 'pdf_native', cropbox)` 得来）。 */
  rect: ScreenRect;
  viewport: ScreenViewport & PdfPointViewport;
  cropbox?: CropBox | null;
  /** 松手后的新 box（PDF 坐标 y 向上，`[x, y, x2, y2]`）。 */
  onCommit: (box: Box) => void;
  /** 只读态（编译中 / 活动 job）：手柄不渲染，框仍显示为选中态。 */
  disabled?: boolean;
  disabledReason?: string;
}

export function BboxEditor({
  id,
  rect,
  viewport,
  cropbox,
  onCommit,
  disabled = false,
  disabledReason,
}: BboxEditorProps) {
  // 拖拽中的实时矩形；null = 没在拖（显示 props 给的矩形）
  const [dragging, setDragging] = useState<ScreenRect | null>(null);
  const rectRef = useRef(rect);
  const shown = dragging ?? rect;

  // 不在拖拽中时跟随 props（换页 / 选中另一段 / PATCH 返回的新值）；
  // 拖拽中不改基准，避免「拖着拖着被服务端回包挪走」。
  useEffect(() => {
    if (dragging === null) rectRef.current = rect;
  }, [rect, dragging]);

  const handlePointerDown = useCallback(
    (handle: HandleId | 'body') => (event: ReactPointerEvent<SVGRectElement>) => {
      if (disabled) return;
      if (event.pointerType === 'mouse' && event.button !== 0) return;
      event.preventDefault();
      event.stopPropagation();
      const target = event.currentTarget;
      const startRect = rectRef.current;
      const originX = event.clientX;
      const originY = event.clientY;
      target.setPointerCapture(event.pointerId);
      setDragging(startRect);

      const handleMove = (moveEvent: globalThis.PointerEvent) => {
        if (moveEvent.pointerId !== event.pointerId) return;
        setDragging(
          resizeBox(startRect, handle, moveEvent.clientX - originX, moveEvent.clientY - originY),
        );
      };
      const finish = (next: ScreenRect | null) => {
        target.removeEventListener('pointermove', handleMove);
        target.removeEventListener('pointerup', handleUp);
        target.removeEventListener('pointercancel', handleUp);
        window.removeEventListener('keydown', handleKey);
        setDragging(null);
        if (next === null) return; // ESC / 指针中断：不回调，退回原框
        rectRef.current = next;
        onCommit(screenToPdfBox(next, viewport, 'pdf_native', cropbox));
      };
      const handleUp = (upEvent: globalThis.PointerEvent) => {
        if (upEvent.pointerId !== event.pointerId) return;
        finish(
          resizeBox(startRect, handle, upEvent.clientX - originX, upEvent.clientY - originY),
        );
      };
      const handleKey = (keyEvent: KeyboardEvent) => {
        if (keyEvent.key !== 'Escape') return;
        keyEvent.preventDefault();
        finish(null);
      };
      target.addEventListener('pointermove', handleMove);
      target.addEventListener('pointerup', handleUp);
      target.addEventListener('pointercancel', handleUp);
      window.addEventListener('keydown', handleKey);
    },
    [cropbox, disabled, onCommit, viewport],
  );

  if (disabled) {
    return (
      <svg
        className="pointer-events-none absolute left-0 top-0"
        width={viewport.width}
        height={viewport.height}
        viewBox={`0 0 ${viewport.width} ${viewport.height}`}
        role="group"
        aria-label={`段落 ${id} 的框（只读：${disabledReason ?? '当前不可编辑'}）`}
        data-od-id="bbox-editor"
        data-editable="false"
      >
        <rect
          data-od-id={`bbox-editor-box-${id}`}
          x={shown.x}
          y={shown.y}
          width={shown.width}
          height={shown.height}
          strokeWidth={2}
          className="pointer-events-none fill-none stroke-accent opacity-60 [stroke-dasharray:4_3]"
        >
          <title>{`${id} · ${disabledReason ?? '当前不可编辑'}`}</title>
        </rect>
      </svg>
    );
  }

  // 根层 `pointer-events-none`（与 `BboxLayer` 同口径）：SVG 根的 fill 默认是黑色（painted），
  // 不加就会被整块 svg 抢走命中 —— 选中的段落会挡住**其它**段落框的点击（真浏览器冒烟实测）。
  // 命中只落在显式 `pointer-events-auto` 的子元素上（框体 / 手柄 / 拖拽盾）。
  return (
    <svg
      className="pointer-events-none absolute left-0 top-0"
      width={viewport.width}
      height={viewport.height}
      viewBox={`0 0 ${viewport.width} ${viewport.height}`}
      role="group"
      aria-label={`段落 ${id} 的框（可拖拽：8 个手柄）`}
      data-od-id="bbox-editor"
      data-editable="true"
      data-dragging={dragging !== null}
    >
      {dragging === null ? null : (
        // 拖拽期间盖住整块视口：底层 BboxLayer 的 rect 拿不到指针事件（不会误选其它段）。
        // 必须带上 `pointer-events-auto` + 非 none 的填充，否则 SVG 命中测试照样穿透。
        <rect
          data-od-id="bbox-editor-shield"
          x={0}
          y={0}
          width={viewport.width}
          height={viewport.height}
          className="pointer-events-auto fill-tint"
          onPointerDown={(event) => event.stopPropagation()}
          onClick={(event) => event.stopPropagation()}
        />
      )}
      <rect
        data-od-id={`bbox-editor-box-${id}`}
        x={shown.x}
        y={shown.y}
        width={shown.width}
        height={shown.height}
        strokeWidth={2}
        className={cn(
          'fill-none stroke-accent',
          dragging === null ? 'cursor-move pointer-events-auto' : 'pointer-events-none',
        )}
        onPointerDown={handlePointerDown('body')}
      >
        <title>{`${id} · 拖框移动（ESC 取消）`}</title>
      </rect>
      {HANDLES.map((handle) => {
        const { cx, cy } = handlePoint(shown, handle);
        return (
          <rect
            key={handle}
            data-od-id={`bbox-handle-${handle}`}
            data-handle={handle}
            role="button"
            tabIndex={-1}
            aria-label={`调整段落 ${id} 的框（${handle}）`}
            x={cx - 3}
            y={cy - 3}
            width={6}
            height={6}
            strokeWidth={1.5}
            style={{ cursor: HANDLE_CURSORS[handle] }}
            className="pointer-events-auto fill-ivory stroke-accent"
            onPointerDown={handlePointerDown(handle)}
          >
            <title>{`调整 ${handle}（ESC 取消）`}</title>
          </rect>
        );
      })}
    </svg>
  );
}
