/**
 * 段落框叠加层（M2-06）：把 `paragraph` 事件里的 bbox 画到页面画布上。
 *
 * 坐标换算全在 `geometry.boxToViewRect`（规约 #6：只此一处）。
 * 层本身 `pointer-events: none`，只有框自己接事件，避免挡住画布上的文本选择。
 */
import { useMemo } from 'react';
import type { Box, CoordSystem, ParagraphStatus } from '@shared/protocol';
import { boxToViewRect, isValidBox, type ViewportLike, type ViewRect } from './geometry';

/** 一个段落在本页上的全部框。 */
export interface ParagraphBoxes {
  id: string;
  boxes: Box[];
  coordSystem: CoordSystem;
  status: ParagraphStatus;
}

export interface BoxLayerProps {
  viewport: ViewportLike;
  paragraphs: readonly ParagraphBoxes[];
  selectedId: string | null;
  hoveredId: string | null;
  onSelect: (id: string) => void;
  onHover: (id: string | null) => void;
  /** 只读模式（译文栏）：不接鼠标事件，仅高亮选中段。 */
  readOnly?: boolean;
}

/** 段落状态 → 框线颜色（与段落树图标同一套语义）。 */
export function boxColor(status: ParagraphStatus): string {
  switch (status) {
    case 'translated':
      return 'var(--vscode-charts-green, #89d185)';
    case 'typeset':
      return 'var(--vscode-charts-blue, #75beff)';
    case 'not_replaced':
      return 'var(--vscode-editorWarning-foreground, #cca700)';
    case 'fallback':
      return 'var(--vscode-charts-orange, #d18616)';
  }
}

/** 段落 → 本页上的矩形列表（过滤非法框）。 */
export function layoutBoxes(
  paragraphs: readonly ParagraphBoxes[],
  viewport: ViewportLike,
): Array<{ paragraph: ParagraphBoxes; rects: ViewRect[] }> {
  return paragraphs.map((paragraph) => ({
    paragraph,
    rects: paragraph.boxes
      .filter((box) => isValidBox(box))
      .map((box) => boxToViewRect(box, paragraph.coordSystem, viewport))
      .filter((rect) => rect.width > 0.5 && rect.height > 0.5),
  }));
}

export function BoxLayer({
  viewport,
  paragraphs,
  selectedId,
  hoveredId,
  onSelect,
  onHover,
  readOnly = false,
}: BoxLayerProps): JSX.Element {
  const laid = useMemo(() => layoutBoxes(paragraphs, viewport), [paragraphs, viewport]);

  return (
    <div
      aria-hidden={readOnly}
      style={{
        position: 'absolute',
        inset: 0,
        pointerEvents: 'none',
        width: viewport.width,
        height: viewport.height,
      }}
    >
      {laid.map(({ paragraph, rects }) => {
        const selected = paragraph.id === selectedId;
        const hovered = paragraph.id === hoveredId;
        if (readOnly && !selected) return null;
        const color = boxColor(paragraph.status);
        return rects.map((rect, index) => (
          <div
            key={`${paragraph.id}-${index}`}
            role={readOnly ? undefined : 'button'}
            tabIndex={readOnly ? undefined : -1}
            aria-label={readOnly ? undefined : `段落 ${paragraph.id}`}
            data-paragraph-id={paragraph.id}
            onMouseEnter={readOnly ? undefined : () => onHover(paragraph.id)}
            onMouseLeave={readOnly ? undefined : () => onHover(null)}
            onClick={
              readOnly
                ? undefined
                : (domEvent) => {
                    domEvent.stopPropagation();
                    onSelect(paragraph.id);
                  }
            }
            style={{
              position: 'absolute',
              left: rect.left,
              top: rect.top,
              width: rect.width,
              height: rect.height,
              boxSizing: 'border-box',
              border: `1px solid ${selected || hovered ? color : 'transparent'}`,
              outline: selected ? `1px solid ${color}` : 'none',
              background: selected
                ? 'color-mix(in srgb, var(--vscode-editor-selectionBackground, #264f78) 35%, transparent)'
                : hovered
                  ? 'color-mix(in srgb, var(--vscode-editor-selectionBackground, #264f78) 18%, transparent)'
                  : 'transparent',
              borderRadius: 2,
              cursor: readOnly ? 'default' : 'pointer',
              pointerEvents: readOnly ? 'none' : 'auto',
              transition: 'background 90ms linear',
            }}
          />
        ));
      })}
    </div>
  );
}
