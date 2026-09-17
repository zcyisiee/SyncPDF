import { useUiStore } from '../../stores/ui';
import type { WorkbenchView } from '../../lib/routing';
import { EventStreamPanel } from '../events/EventStreamPanel';
import type { EventFeed } from '../events/useEventWindow';

/**
 * 右侧面板 360px（§3 栅格）。进度视图渲染事件流（§4.6，W06）；识别/翻译/检查视图仍是 W05 的
 * 段落占位（段落属性面板 W10 接入）。顶部保留一行「已选中段落」——预览里点框选中的段落 id
 * 在**任何**视图下都可见（W05 的选中联动不因为 W06 占了面板而消失）。
 */
export function InspectorPanel({
  did,
  view,
  feed,
}: {
  did: string;
  view: WorkbenchView;
  feed: EventFeed;
}) {
  const collapsed = useUiStore((state) => state.inspectorCollapsed);
  const selectedParagraphId = useUiStore((state) => state.selectedParagraphId);
  return (
    <aside
      aria-label="右侧面板"
      data-od-id="inspector"
      className="col-start-5 row-start-1 min-h-0 overflow-hidden border-l border-hair bg-ivory"
    >
      {collapsed ? null : (
        <div className="flex h-full min-h-0 flex-col" data-od-id="inspector-body">
          {selectedParagraphId === null ? null : (
            <div
              className="flex flex-none items-baseline gap-s2 border-b border-hair bg-sand px-s3 py-[6px]"
              data-od-id="inspector-selection"
            >
              <span className="text-tiny text-ink-3">已选中段落</span>
              <span
                className="font-mono text-sm text-ink-2"
                data-od-id="selected-paragraph-id"
              >
                {selectedParagraphId}
              </span>
            </div>
          )}
          {view === 'progress' ? (
            <EventStreamPanel did={did} feed={feed} />
          ) : (
            <div
              className="h-full overflow-auto p-s5"
              data-od-id="inspector-placeholder"
            >
              <p className="font-serif text-md font-medium leading-[1.4] text-ink-2">
                段落属性
              </p>
              <p className="mt-s2 text-tiny text-ink-4">
                {selectedParagraphId === null
                  ? '点击预览里的段落框查看该段（段落属性编辑 W10 接入）'
                  : '原文 / 译文 / 排版参数在 W10 接入'}
              </p>
            </div>
          )}
        </div>
      )}
    </aside>
  );
}
