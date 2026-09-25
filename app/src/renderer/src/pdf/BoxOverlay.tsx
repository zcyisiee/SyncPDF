/**
 * 一页的叠加框：按版面类型着色，悬停显示类型与段落 ID，点击选中段落。
 */
import type { ParagraphId } from '@shared/protocol';
import { rectToViewRect } from './geometry';
import type { OverlayItem } from './overlay';
import type { PageViewport } from './pdfjs';
import { REGION_STYLE } from './regionKinds';

export function BoxOverlay({
  items,
  viewport,
  selected,
  onSelect,
}: {
  items: OverlayItem[];
  viewport: PageViewport;
  selected: ParagraphId | null;
  onSelect: (id: ParagraphId) => void;
}): JSX.Element {
  return (
    <div className="sp-overlay">
      {items.map((item) => {
        const view = rectToViewRect(item.rect, item.coordSystem, viewport);
        if (view.width < 1 || view.height < 1) return null;
        const { label, color } = REGION_STYLE[item.kind];
        const id = item.paragraphId;
        const classes = [
          'sp-box',
          id !== null ? 'is-paragraph' : '',
          item.inline ? 'is-inline' : '',
          id !== null && id === selected ? 'is-selected' : '',
        ];
        return (
          <div
            key={item.key}
            className={classes.join(' ')}
            data-pid={id ?? undefined}
            style={{ left: view.left, top: view.top, width: view.width, height: view.height, ['--kind' as string]: color }}
            onClick={id === null ? undefined : () => onSelect(id)}
          >
            {!item.inline && (
              <span className="sp-box-tag">
                {label}
                {id !== null && ` · ${id}`}
              </span>
            )}
          </div>
        );
      })}
    </div>
  );
}
