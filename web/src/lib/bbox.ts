import type { BboxItem } from './preview';

// JSON encoding keeps missing labels distinct from every possible raw label.
export const categoryKey = (label: string | null): string => JSON.stringify(label);
export function categoryLabel(label: string | null): string {
  return label ?? '未标注';
}
const colors: Record<string, string> = {
  text: '#7c9aff', title: '#c58fff', inline_equation: '#f4ba70',
  interline_equation: '#ff9775', equation: '#ff9775', page_number: '#ff8499',
};
/** Deterministic raw-label color, independent of page order or available categories. */
export function categoryColor(label: string | null): string {
  if (label !== null && Object.hasOwn(colors, label)) return colors[label];
  let hash = 0;
  for (const char of categoryKey(label)) hash = (Math.imul(hash, 31) + char.charCodeAt(0)) | 0;
  return `hsl(${((hash % 360) + 360) % 360} 65% 64%)`;
}
export interface BboxVisibility { defaultVisible: boolean; overrides: Record<string, boolean> }
export const DEFAULT_VISIBILITY: BboxVisibility = { defaultVisible: true, overrides: {} };
export function categoryVisible(visibility: BboxVisibility, label: string | null): boolean {
  const key = categoryKey(label);
  return Object.hasOwn(visibility.overrides, key) ? visibility.overrides[key] : visibility.defaultVisible;
}
export function pageCategories(boxes: readonly BboxItem[]) {
  const counts = new Map<string | null, number>();
  for (const box of boxes) counts.set(box.label, (counts.get(box.label) ?? 0) + 1);
  return [...counts].sort(([a], [b]) => (a ?? '').localeCompare(b ?? ''));
}
