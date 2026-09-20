import type { KeyboardEvent, PointerEvent } from 'react';

import { cn } from '../../lib/cn';
import { LAYOUT_SPECS, useUiStore, widthFromDelta, type GutterId } from '../../stores/ui';

/**
 * 可拖分隔条：固定 6px，Pointer Events + `setPointerCapture`，
 * `role="separator"` + 方向键可达（列 16px）。宽度变量与范围在 stores/ui.ts。
 * 放置（grid 列/行）由调用方通过 className 决定。
 */
export function Gutter({ id, className }: { id: GutterId; className?: string }) {
  const spec = LAYOUT_SPECS[id];
  const value = useUiStore((state) => state[spec.key]);
  const dragging = useUiStore((state) => state.dragging === id);
  const setLayoutWidth = useUiStore((state) => state.setLayoutWidth);
  const setDragging = useUiStore((state) => state.setDragging);
  const vertical = spec.axis === 'x';

  const moveTo = (delta: number) => {
    setLayoutWidth(id, widthFromDelta(id, value, delta));
  };

  const handlePointerDown = (event: PointerEvent<HTMLDivElement>) => {
    if (event.pointerType === 'mouse' && event.button !== 0) return;
    const target = event.currentTarget;
    const origin = vertical ? event.clientX : event.clientY;
    event.preventDefault();
    target.setPointerCapture(event.pointerId);
    setDragging(id);
    const handleMove = (moveEvent: globalThis.PointerEvent) => {
      if (moveEvent.pointerId !== event.pointerId) return;
      moveTo((vertical ? moveEvent.clientX : moveEvent.clientY) - origin);
    };
    const handleEnd = (endEvent: globalThis.PointerEvent) => {
      if (endEvent.pointerId !== event.pointerId) return;
      target.removeEventListener('pointermove', handleMove);
      target.removeEventListener('pointerup', handleEnd);
      target.removeEventListener('pointercancel', handleEnd);
      setDragging(null);
    };
    target.addEventListener('pointermove', handleMove);
    target.addEventListener('pointerup', handleEnd);
    target.addEventListener('pointercancel', handleEnd);
  };

  const handleKeyDown = (event: KeyboardEvent<HTMLDivElement>) => {
    let direction = 0;
    if (vertical) {
      if (event.key === 'ArrowRight') direction = 1;
      else if (event.key === 'ArrowLeft') direction = -1;
    } else {
      if (event.key === 'ArrowDown') direction = 1;
      else if (event.key === 'ArrowUp') direction = -1;
    }
    if (direction === 0) return;
    event.preventDefault();
    moveTo(direction * spec.step);
  };

  return (
    <div
      role="separator"
      tabIndex={0}
      aria-orientation={vertical ? 'vertical' : 'horizontal'}
      aria-label={spec.label}
      aria-valuenow={value}
      aria-valuemin={spec.min}
      aria-valuemax={spec.max}
      data-od-id={`gutter-${id}`}
      data-dragging={dragging}
      onPointerDown={handlePointerDown}
      onKeyDown={handleKeyDown}
      className={cn(
        'group relative z-[6] touch-none bg-parchment',
        vertical ? 'cursor-col-resize' : 'cursor-row-resize',
        'hover:bg-[color-mix(in_oklch,var(--accent)_7%,var(--parchment))]',
        'focus-visible:outline-offset-[-2px]',
        className,
      )}
    >
      <span
        aria-hidden="true"
        className={cn(
          'pointer-events-none absolute rounded-[1px] bg-accent opacity-0 transition-opacity',
          'group-hover:opacity-100 group-focus-visible:opacity-100',
          dragging && 'opacity-100',
          vertical ? 'bottom-0 left-[2px] top-0 w-[2px]' : 'left-0 right-0 top-[2px] h-[2px]',
        )}
      />
    </div>
  );
}
