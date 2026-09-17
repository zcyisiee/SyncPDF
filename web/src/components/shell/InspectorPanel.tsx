import { useUiStore } from '../../stores/ui';

/**
 * 右侧面板 360px（§3 栅格）。本任务是 W05 占位：预览里点段落框 → 这里显示选中的段落 id
 * （真正的段落属性 / 事件流 / 检查问题分别由 W10 / W06 / W12 接入）。
 */
export function InspectorPanel() {
  const collapsed = useUiStore((state) => state.inspectorCollapsed);
  const selectedParagraphId = useUiStore((state) => state.selectedParagraphId);
  return (
    <aside
      aria-label="右侧面板"
      data-od-id="inspector"
      className="col-start-5 row-start-1 min-h-0 overflow-hidden border-l border-hair bg-ivory"
    >
      {collapsed ? null : (
        <div
          className="h-full overflow-auto p-s5"
          data-od-id="inspector-placeholder"
        >
          {selectedParagraphId === null ? (
            <p className="text-tiny text-ink-4">
              点击预览里的段落框查看该段（段落属性编辑 W10 接入）
            </p>
          ) : (
            <div className="flex flex-col gap-s2" data-od-id="inspector-selection">
              <p className="font-serif text-md font-medium leading-[1.4] text-ink-2">已选中段落</p>
              <p
                className="font-mono text-sm text-ink-3"
                data-od-id="selected-paragraph-id"
              >
                {selectedParagraphId}
              </p>
              <p className="text-tiny text-ink-4">原文 / 译文 / 排版参数在 W10 接入</p>
            </div>
          )}
        </div>
      )}
    </aside>
  );
}
