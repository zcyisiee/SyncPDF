/**
 * 一页叠加框的来源合并（纯函数）：
 * - 段落框：可点击，带段落 ID；
 * - 版面区域：没有段落覆盖的区域（图、表、独立公式，或段落尚未到达）只画框；
 * - 行内公式：细虚线，始终画（它们随正文移动，不单独成段）。
 * 被隐藏的类型不出现。
 */
import type { ParagraphRecord } from '@shared/library';
import type { CoordSystem, LayoutRegion, ParagraphId, Rect, RegionKind } from '@shared/protocol';

export interface OverlayItem {
  key: string;
  kind: RegionKind;
  rect: Rect;
  coordSystem: CoordSystem;
  paragraphId: ParagraphId | null;
  inline: boolean;
}

const center = (r: Rect): [number, number] => [(r.x0 + r.x1) / 2, (r.y0 + r.y1) / 2];

function contains(r: Rect, [x, y]: [number, number]): boolean {
  return x >= Math.min(r.x0, r.x1) && x <= Math.max(r.x0, r.x1) && y >= Math.min(r.y0, r.y1) && y <= Math.max(r.y0, r.y1);
}

export function overlayItems(
  regions: readonly LayoutRegion[],
  paragraphs: readonly ParagraphRecord[],
  hiddenKinds: readonly RegionKind[],
): OverlayItem[] {
  const visible = (kind: RegionKind): boolean => !hiddenKinds.includes(kind);
  const items: OverlayItem[] = [];
  const paragraphBoxes: Rect[] = [];
  for (const p of paragraphs) {
    for (const [index, rect] of (p.boxes ?? []).entries()) {
      // 版面区域是 pdf_user；只有同坐标系的段落框参与覆盖判断
      if (p.coord_system === 'pdf_user') paragraphBoxes.push(rect);
      if (visible(p.kind)) {
        items.push({
          key: `${p.paragraph_id}:${index}`,
          kind: p.kind,
          rect,
          coordSystem: p.coord_system,
          paragraphId: p.paragraph_id,
          inline: false,
        });
      }
    }
  }
  for (const [index, region] of regions.entries()) {
    if (!visible(region.kind)) continue;
    if (!region.inline && paragraphBoxes.some((box) => contains(box, center(region.bbox)))) continue;
    items.push({
      key: `r${index}`,
      kind: region.kind,
      rect: region.bbox,
      coordSystem: 'pdf_user',
      paragraphId: null,
      inline: region.inline,
    });
  }
  return items;
}
