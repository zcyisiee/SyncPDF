import type { BboxItem } from './preview';

// JSON encoding keeps missing labels distinct from every possible raw label.
export const categoryKey = (label: string | null): string => JSON.stringify(label);
const names: Record<string, string> = {
  title: '标题', text: '正文', plain_text: '正文', abandon: '忽略区域', figure: '插图',
  figure_caption: '图注', table: '表格', table_caption: '表题', table_footnote: '表格脚注',
  isolate_formula: '独立公式', formula_caption: '公式编号', caption: '说明',
  header: '页眉', footer: '页脚', footnote: '脚注', reference: '参考文献',
};
export function categoryLabel(label: string | null): string {
  if (label === null) return '未标注（null）';
  return `${Object.hasOwn(names, label) ? names[label] : '其他类别'} · ${label}`;
}
/** Deterministic raw-label color, independent of page order or available categories. */
export function categoryColor(label: string | null): string {
  let hash = 0;
  for (const char of categoryKey(label)) hash = (Math.imul(hash, 31) + char.charCodeAt(0)) | 0;
  return `hsl(${((hash % 360) + 360) % 360} 65% 38%)`;
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
