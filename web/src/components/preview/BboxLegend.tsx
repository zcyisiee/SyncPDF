import type { BboxItem } from '../../lib/preview';
import { categoryColor, categoryKey, categoryLabel, categoryVisible, pageCategories } from '../../lib/bbox';
import { readVisibility, useBboxStore } from '../../stores/bbox';

export function BboxLegend({ did, boxes, labels }: {
  did: string;
  boxes: readonly BboxItem[];
  labels?: readonly { label: string | null }[];
}) {
  const state = useBboxStore();
  const visibility = state.documents[did] ?? readVisibility(did);
  const categories = labels ?? pageCategories(boxes).map(([label]) => ({ label }));
  if (categories.length === 0) return null;
  return <div className="flex max-h-56 flex-none flex-wrap gap-x-s4 gap-y-s2 overflow-auto border-b border-hair px-s5 py-s2 text-tiny" aria-label="框类别设置">
    {categories.map(({ label }) => <label key={categoryKey(label)} className="flex cursor-pointer items-center gap-s2">
      <input type="checkbox" checked={categoryVisible(visibility, label)}
        onChange={(event) => state.setCategory(did, label, event.target.checked)} />
      <span aria-hidden="true" className="h-3 w-3 rounded-sm border-2" style={{ borderColor: categoryColor(label), background: `color-mix(in srgb, ${categoryColor(label)} 8%, transparent)` }} />
      {categoryLabel(label)}
    </label>)}
  </div>;
}
