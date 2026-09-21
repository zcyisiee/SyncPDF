import { useState } from 'react';

import { activeJob } from '../../lib/jobs';
import { useDocument, useJobs } from '../../lib/queries';
import type { WorkbenchView } from '../../lib/routing';
import { cn } from '../../lib/cn';
import { useUiStore } from '../../stores/ui';
import { ArchiveSummary } from '../archive/ArchiveSummary';
import { VersionList } from '../archive/VersionList';
import { BatchPanel } from '../edit/BatchPanel';
import { ParagraphEditor } from '../edit/ParagraphEditor';
import { EventStreamPanel } from '../events/EventStreamPanel';
import { DownloadButton } from '../preview/DownloadButton';
import { ExportButton } from '../preview/ExportButton';
import type { EventFeed } from '../events/useEventWindow';
import type { PersistentEvent } from '../../lib/usePersistentEvents';

/** 面板 tab：`paragraph` = W10 段落编辑器，`events` = W06 事件流，`archive` = 版本归档（W12）。 */
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
 *
 * 归档 tab 是**版本与导出的唯一入口**（用户决策）：导出/下载按钮 + 归档摘要 + 版本列表
 * （`GET D/versions`）。`view==='archive'`（`#/d/:did/archive`）不再换中栏内容，只是让这里
 * 默认选中归档 tab。
 *
 * 文档任务控制（开始翻译 / 取消 / 重试 / 编译全文）**不在本面板**：它住左栏论文导航底部的
 * 操作区（`PaperNav` → `DocumentActionBar`，`data-od-id="action-bar"`），在那里所有视图与
 * 所有 tab 下都常驻。
 *
 * 栏宽（`--inspw`）由外壳的 `.wb-grid` 管，本面板不控自己宽度。
 */
export function InspectorPanel({
  did,
  view,
  feed,
  compileEvents,
}: {
  did: string;
  view: WorkbenchView;
  feed: EventFeed;
  compileEvents: PersistentEvent[];
}) {
  const selectedParagraphId = useUiStore((state) => state.selectedParagraphId);
  const selectedParagraphIds = useUiStore((state) => state.selectedParagraphIds);
  const focusParagraph = useUiStore((state) => state.focusParagraph);
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

  // 默认 tab：archive 视图（旧链接/下载按钮的「历史版本」）给归档 tab，其余给段落编辑器。
  const [tab, setTab] = useState<InspectorTab>(view === 'archive' ? 'archive' : 'paragraph');

  return (
    <aside
      aria-label="右侧面板"
      data-od-id="inspector"
      className="col-start-3 row-start-1 min-h-0 overflow-hidden border-l border-hair bg-ivory"
    >
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
          <div role="tabpanel" aria-label="段落" className="flex min-h-0 flex-1 flex-col">
            {/* 多选 ≥2：块切换 chips + 批量编译面板压在段落编辑器上方（编辑器跟随主选中段）。 */}
            {selectedParagraphIds.length > 1 ? (
              <>
                <ParagraphChips
                  blockIds={selectedParagraphIds}
                  activeId={selectedParagraphId}
                  onFocus={(id) => focusParagraph(id)}
                />
                <BatchPanel
                  did={did}
                  blockIds={selectedParagraphIds}
                  disabled={editingLocked}
                  disabledReason={editingLockedReason}
                />
              </>
            ) : null}
            <div className="min-h-0 flex-1">
              <ParagraphEditor
                did={did}
                paragraphId={selectedParagraphId}
                disabled={editingLocked}
                disabledReason={editingLockedReason}
              />
            </div>
          </div>
        ) : null}
        {tab === 'archive' ? (
          <div
            role="tabpanel"
            aria-label="归档"
            data-od-id="archive-tab"
            className="min-h-0 flex-1 overflow-auto"
          >
            {/* 右栏只有 340px 档宽：行距紧凑，导出/下载并排（窄时换行）。 */}
            <div className="flex flex-col gap-s4 p-s5">
              <div
                className="flex flex-wrap items-center gap-s2"
                data-od-id="archive-actions"
              >
                <ExportButton did={did} />
                <DownloadButton
                  did={did}
                  compile={compile}
                  quality={documentQuery.data?.quality ?? null}
                />
              </div>
              <ArchiveSummary did={did} />
              <VersionList did={did} compile={compile} />
            </div>
          </div>
        ) : null}
        {tab === 'events' ? (
          /* `EventStreamPanel` 根节点用 `flex-1` 撑高：容器必须是 flex 列，否则
             块级容器里 `flex-1` 失效，时间线按内容高度无限长大（实测 35171px），
             内层 `overflow-auto` 的 clientHeight == scrollHeight → 两栏都滚不动，
             超出 818px 的部分被 `inspector` 的 `overflow-hidden` 直接裁掉。 */
          <div role="tabpanel" aria-label="事件流" className="flex min-h-0 flex-1 flex-col">
            <EventStreamPanel did={did} feed={feed} compileEvents={compileEvents} />
          </div>
        ) : null}
      </div>
    </aside>
  );
}

/**
 * 多选（≥2 段）时的块切换 chips：每段一个 chip，当前主选中段（编辑器跟随的那段）高亮；
 * 点 chip → `focusParagraph(id)`（把该段挪到集合末尾，编辑器随之切过去）。
 * 集合只有一段时不渲染（单选没有可切换的对象）。
 */
function ParagraphChips({
  blockIds,
  activeId,
  onFocus,
}: {
  blockIds: string[];
  activeId: string | null;
  onFocus: (id: string) => void;
}) {
  return (
    <div
      data-od-id="paragraph-chips"
      className="flex flex-none flex-wrap items-center gap-s2 border-b border-hair bg-ivory px-s3 py-[6px]"
    >
      {blockIds.map((id) => (
        <button
          key={id}
          type="button"
          data-od-id={`paragraph-chip-${id}`}
          aria-pressed={id === activeId}
          onClick={() => onFocus(id)}
          className={cn(
            'h-[19px] rounded-[3px] px-[6px] font-mono text-micro leading-none tracking-[0.03em] transition-colors',
            id === activeId ? 'bg-accent-soft text-accent' : 'bg-sand text-ink-3 hover:text-ink',
          )}
        >
          {id}
        </button>
      ))}
    </div>
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
