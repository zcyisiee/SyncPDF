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

const TABS: readonly InspectorTab[] = ['paragraph', 'events', 'archive'];

/**
 * 右侧面板 360px：一行头部（左：段落/事件流/归档 tab；右：已选中段落 + 只读标记），
 * 下面是当前 tab 的内容。三个 tab 任何视图都在（视图合并后不再按视图换 tab 集）——
 * 翻译进行中看「事件流」，微调结果看「段落」，版本历史看「归档」。
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

  // 默认 tab：归档视图给归档摘要（与预览区的版本列表同屏），其余给段落编辑器。
  const [tab, setTab] = useState<InspectorTab>(view === 'archive' ? 'archive' : 'paragraph');

  return (
    <aside
      aria-label="右侧面板"
      data-od-id="inspector"
      className="col-start-3 row-start-1 min-h-0 overflow-hidden border-l border-hair bg-ivory"
    >
      {collapsed ? null : (
        <div className="flex h-full min-h-0 flex-col" data-od-id="inspector-body">
          <div
            role="tablist"
            aria-label="右侧面板"
            data-od-id="inspector-tabs"
            className="flex flex-none items-center gap-s2 border-b border-hair bg-ivory px-s3 py-[4px]"
          >
            {TABS.map((item) => (
              <TabButton
                key={item}
                odId={`inspector-tab-${item}`}
                active={tab === item}
                onClick={() => setTab(item)}
              >
                {TAB_LABELS[item]}
              </TabButton>
            ))}
            {selectedParagraphId === null ? null : (
              <span className="ml-auto flex min-w-0 items-center gap-s2">
                {editingLocked ? (
                  <span className="font-mono text-micro text-run-ink" data-od-id="editor-locked">
                    只读（{compileRunning ? '编译中' : '有任务在跑'}）
                  </span>
                ) : null}
                <span
                  className="truncate font-mono text-micro text-ink-3"
                  data-od-id="selected-paragraph-id"
                >
                  {selectedParagraphId}
                </span>
              </span>
            )}
          </div>
          {tab === 'paragraph' ? (
            <div role="tabpanel" aria-label="段落" className="min-h-0 flex-1">
              <ParagraphEditor
                did={did}
                paragraphId={selectedParagraphId}
                disabled={editingLocked}
                disabledReason={editingLockedReason}
              />
            </div>
          ) : null}
          {tab === 'archive' ? (
            <div role="tabpanel" aria-label="归档" className="min-h-0 flex-1">
              <ArchiveSummary did={did} />
            </div>
          ) : null}
          {tab === 'events' ? (
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
