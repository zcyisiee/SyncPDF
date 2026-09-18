import { useState } from 'react';

import { activeJob } from '../../lib/jobs';
import { useDocument, useJobs } from '../../lib/queries';
import type { WorkbenchView } from '../../lib/routing';
import { cn } from '../../lib/cn';
import { useUiStore } from '../../stores/ui';
import { ArchiveSummary } from '../archive/ArchiveSummary';
import { ParagraphEditor } from '../edit/ParagraphEditor';
import { EventStreamPanel } from '../events/EventStreamPanel';
import type { EventFeed } from '../events/useEventWindow';

/** 面板 tab：`paragraph` = W10 段落编辑器，`events` = W06 事件流，`archive` = W12 归档摘要。 */
type InspectorTab = 'paragraph' | 'events' | 'archive';

/** tab → 文案（tab 栏渲染的唯一来源）。 */
const TAB_LABELS: Record<InspectorTab, string> = {
  paragraph: '段落',
  events: '事件流',
  archive: '归档',
};

/**
 * 右侧面板 360px（§3 栅格）：顶部一行「已选中段落」（W05 的选中联动，任何视图/任何 tab 都可见），
 * 下面是**每个视图自己的 tab 集**——
 *
 * - 进度：事件流是唯一内容（W06 语义不变，不渲染 tab 栏）；
 * - 翻译 / 识别 / 检查：段落编辑器在前（默认），事件流退为次要 tab，W12 的归档摘要常驻第三 tab
 *   （它只是摘要 + 「查看全部」链接到归档视图，不重复列表；数据与归档视图同一个 query key）；
 * - 归档：归档摘要在前（默认，与预览区的版本列表同屏），事件流在后。
 *
 * 编辑只读判据与服务端一致（api.md §3.3）：`compile.status=running` 或该文档有活动 job 时
 * 草稿写端点会 409 `document_busy`，所以这里提前把编辑器置只读并说明原因。
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
  const documentQuery = useDocument(did);
  const jobsQuery = useJobs(did);
  const compile = documentQuery.data?.compile ?? null;
  const busyJob = activeJob(jobsQuery.data);
  const compileRunning = compile?.status === 'running';
  const editingLocked = compileRunning || (busyJob !== null && busyJob.effective_scope !== 'block');
  const editingLockedReason = compileRunning
    ? '编译中…：编译结束后可继续编辑'
    : busyJob === null
      ? undefined
      : `该文档有活动任务 ${busyJob.job_id}（${busyJob.action}），任务期间草稿只读`;

  const defaultTab: InspectorTab =
    view === 'progress' ? 'events' : view === 'archive' ? 'archive' : 'paragraph';
  const [tab, setTab] = useState<InspectorTab>(defaultTab);
  // 换视图 → 回到该视图的默认 tab（渲染期调整 state，不在 effect 里同步）
  const [lastView, setLastView] = useState(view);
  if (view !== lastView) {
    setLastView(view);
    setTab(defaultTab);
  }

  const showEditor = view !== 'progress' && view !== 'archive';
  // 每个视图的 tab 集：进度视图不渲染 tab 栏（事件流是唯一内容）。
  const tabs: InspectorTab[] =
    view === 'archive' ? ['archive', 'events'] : ['paragraph', 'events', 'archive'];

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
              {editingLocked ? (
                <span className="ml-auto font-mono text-micro text-run-ink" data-od-id="editor-locked">
                  只读（{compileRunning ? '编译中' : '有任务在跑'}）
                </span>
              ) : null}
            </div>
          )}
          {view === 'progress' ? null : (
            <div
              role="tablist"
              aria-label="右侧面板"
              data-od-id="inspector-tabs"
              className="flex flex-none items-center gap-s2 border-b border-hair bg-ivory px-s3 py-[4px]"
            >
              {tabs.map((item) => (
                <TabButton
                  key={item}
                  odId={`inspector-tab-${item}`}
                  active={tab === item}
                  onClick={() => setTab(item)}
                >
                  {TAB_LABELS[item]}
                </TabButton>
              ))}
            </div>
          )}
          {tab === 'paragraph' && showEditor ? (
            <div role="tabpanel" aria-label="段落" className="min-h-0 flex-1">
              <ParagraphEditor
                did={did}
                paragraphId={selectedParagraphId}
                disabled={editingLocked}
                disabledReason={editingLockedReason}
              />
            </div>
          ) : null}
          {tab === 'archive' && view !== 'progress' ? (
            <div role="tabpanel" aria-label="归档" className="min-h-0 flex-1">
              <ArchiveSummary did={did} />
            </div>
          ) : null}
          {tab === 'events' || view === 'progress' ? (
            <div role="tabpanel" aria-label="事件流" className="min-h-0 flex-1">
              <EventStreamPanel did={did} feed={feed} />
            </div>
          ) : null}
        </div>
      )}
    </aside>
  );
}

function TabButton({
  odId,
  active,
  onClick,
  children,
}: {
  odId: string;
  active: boolean;
  onClick: () => void;
  children: string;
}) {
  return (
    <button
      type="button"
      role="tab"
      data-od-id={odId}
      aria-selected={active}
      onClick={onClick}
      className={cn(
        'h-6 rounded px-[9px] text-tiny leading-none tracking-[0.02em] transition-colors',
        active ? 'bg-sand text-ink shadow-ring' : 'text-ink-4 hover:bg-sand hover:text-ink-2',
      )}
    >
      {children}
    </button>
  );
}
