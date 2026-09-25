/**
 * 一页叠加框的来源合并（纯函数），按侧取几何：
 * - 段落框：每段一个、可点击、带段落 ID。原文侧用源段落外接框；译文侧用排版后
 *   各行框的并集（译文未发布时引擎给的就是源框）；
 * - 版面区域：没有段落覆盖的区域（图、表、独立公式，或段落尚未到达）只画框，
 *   覆盖判断一律用源段落框（区域是原文版面）；
 * - 行内公式：细虚线，只画在原文侧——译文重排后它随正文移动，源位置不再有效。
 * 被隐藏的类型不出现。
 */
import type { ParagraphRecord } from '@shared/library';
import type { CoordSystem, LayoutRegion, ParagraphId, Rect, RegionKind } from '@shared/protocol';

export type OverlaySide = 'source' | 'target';

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

function union(rects: readonly Rect[]): Rect | null {
  if (rects.length === 0) return null;
  return {
    x0: Math.min(...rects.map((r) => Math.min(r.x0, r.x1))),
    y0: Math.min(...rects.map((r) => Math.min(r.y0, r.y1))),
    x1: Math.max(...rects.map((r) => Math.max(r.x0, r.x1))),
    y1: Math.max(...rects.map((r) => Math.max(r.y0, r.y1))),
  };
}

function paragraphRect(side: OverlaySide, p: ParagraphRecord): { rect: Rect; coordSystem: CoordSystem } | null {
  if (side === 'source') return { rect: p.source_bbox, coordSystem: 'pdf_user' };
  const rect = union(p.boxes ?? []);
  return rect === null ? null : { rect, coordSystem: p.coord_system };
}

export function overlayItems(
  side: OverlaySide,
  regions: readonly LayoutRegion[],
  paragraphs: readonly ParagraphRecord[],
  hiddenKinds: readonly RegionKind[],
): OverlayItem[] {
  const visible = (kind: RegionKind): boolean => !hiddenKinds.includes(kind);
  const items: OverlayItem[] = [];
  for (const p of paragraphs) {
    const placed = visible(p.kind) ? paragraphRect(side, p) : null;
    if (placed === null) continue;
    items.push({ key: p.paragraph_id, kind: p.kind, ...placed, paragraphId: p.paragraph_id, inline: false });
  }
  for (const [index, region] of regions.entries()) {
    if (!visible(region.kind)) continue;
    if (region.inline ? side === 'target' : paragraphs.some((p) => contains(p.source_bbox, center(region.bbox)))) continue;
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
