import type { BboxItem } from '../../lib/preview';
import { categoryColor, categoryKey, categoryLabel, categoryVisible, pageCategories } from '../../lib/bbox';
import { readVisibility, useBboxStore } from '../../stores/bbox';

export function BboxLegend({ did, page, boxes }: { did: string; page: number; boxes: readonly BboxItem[] }) {
  const state = useBboxStore();
  const visibility = state.documents[did] ?? readVisibility(did);
  return <details className="flex-none border-b border-hair px-s5 py-s2 text-tiny">
    <summary className="cursor-pointer">框类别 · 当前页 {page}（{boxes.length} 个）</summary>
    <div className="max-h-56 overflow-auto py-s2" aria-label="框类别设置">
      <p>仅控制预览，不改变识别结果或翻译范围。计数为当前页。</p>
      <div className="my-s2 flex flex-wrap gap-s3">
        <button type="button" onClick={() => state.setAll(did, true)}>全选类别</button>
        <button type="button" onClick={() => state.setAll(did, false)}>全不选类别</button>
        <label>描边粗细
          <input aria-label="描边粗细" type="range" min={0.5} max={4} step={0.5} value={state.strokeWidth}
            onChange={(event) => state.setStyle({ strokeWidth: Number(event.target.value) })} /> {state.strokeWidth}px
        </label>
        <label>填充透明度
          <input aria-label="填充透明度" type="range" min={0} max={0.4} step={0.02} value={state.fillOpacity}
            onChange={(event) => state.setStyle({ fillOpacity: Number(event.target.value) })} /> {Math.round(state.fillOpacity * 100)}%
        </label>
      </div>
      <div className="flex flex-wrap gap-s3">
        {pageCategories(boxes).map(([label, count]) => <label key={categoryKey(label)} className="flex items-center gap-s2">
          <input type="checkbox" checked={categoryVisible(visibility, label)}
            onChange={(event) => state.setCategory(did, label, event.target.checked)} />
          <span aria-hidden="true" style={{ background: categoryColor(label), width: 12, height: 12 }} />
          {categoryLabel(label)}（{count}）
        </label>)}
        {boxes.length === 0 ? <span>当前页无框类别</span> : null}
      </div>
    </div>
  </details>;
}
