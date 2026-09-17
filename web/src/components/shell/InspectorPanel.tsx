import { useUiStore } from '../../stores/ui';

/** 右侧面板 360px（§3 栅格）。本任务是占位：段落属性 / 事件流 / 检查问题分别由 W05–W06 接入。 */
export function InspectorPanel() {
  const collapsed = useUiStore((state) => state.inspectorCollapsed);
  return (
    <aside
      aria-label="右侧面板"
      data-od-id="inspector"
      className="col-start-5 row-start-1 min-h-0 overflow-hidden border-l border-hair bg-ivory"
    >
      {collapsed ? null : (
        <div className="grid h-full place-items-center p-s5" data-od-id="inspector-placeholder">
          <p className="max-w-[30ch] text-center text-tiny text-ink-4">
            段落属性 / 事件流 / 检查问题将在 W05–W06 接入
          </p>
        </div>
      )}
    </aside>
  );
}
