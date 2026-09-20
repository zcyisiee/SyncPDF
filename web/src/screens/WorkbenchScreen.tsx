import { usePersistentEvents } from '../lib/usePersistentEvents';
import { describeApiError } from '../lib/api';
import { compileUnsettled } from '../lib/download';
import { isRunLive } from '../lib/events';
import { activeJob, jobCardMode } from '../lib/jobs';
import { DOCUMENT_LIVE_REFETCH_MS, useDocument, useJobs } from '../lib/queries';
import type { WorkbenchView } from '../lib/routing';
import { Button, LinkButton } from '../components/ui/Button';
import { ErrorCard } from '../components/ui/ErrorCard';
import { ScrollArea } from '../components/ui/ScrollArea';
import { DocumentStatusBadge } from '../components/ui/StatusBadge';
import { Gutter } from '../components/shell/Gutter';
import { InspectorPanel } from '../components/shell/InspectorPanel';
import { JobControls } from '../components/jobs/JobControls';
import { ArchiveView } from '../components/archive/ArchiveView';
import { PreviewArea } from '../components/preview/PreviewArea';
import { useEventWindow } from '../components/events/useEventWindow';
import { useJobUpdates } from '../components/events/useJobUpdates';
import { useTimelineStages } from '../components/events/useTimelineStages';
import { useUiStore } from '../stores/ui';
import type { CSSProperties } from 'react';

/** 事件流说 live 时 stage-state 的轮询间隔（brief：live 2s 否则不轮询）。 */
const STAGE_STATE_LIVE_REFETCH_MS = 2_000;

/**
 * `#/d/:did/*` 工作台的中栏 + 右栏（左栏论文导航由 App 常驻，不在这里）。
 *
 * 内层 `.wb-grid`：中栏 = 一行文档头（文档名 + 状态徽标 + 任务控制）+ 预览区（占满剩余），
 * 然后 gutter + 右侧面板（段落 / 事件流 / 归档）。底部时间线已删除：阶段状态由文档头的
 * `DocumentStatusBadge` 与事件流 tab 承担，`useTimelineStages` 只剩「live 判据」用途
 * （驱动 badge 与 2s 轮询）。
 */
export function WorkbenchScreen({ did, view }: { did: string; view: WorkbenchView }) {
  const persistent = usePersistentEvents(did);
  // job_update（W14）：收到就立刻失效对应查询，并把“刚推过”的时间戳交给 useJobs 做轮询节流。
  const jobUpdates = useJobUpdates(did);
  // jobs 轮询（W08/W14）：有 queued/running 时 5s（SSE 的 job_update 是快路径），否则 30s；
  // 刚收到推送的静默窗口内连这 5s 那一次都跳过（SSE 已经刷新过一轮）。
  const jobsQuery = useJobs(did, { quietSinceMs: jobUpdates.pushedAtMs });
  const cardMode = jobCardMode(jobsQuery.data);
  const latestJob = activeJob(jobsQuery.data) ?? jobsQuery.data?.[0] ?? null;
  const queued = cardMode === 'active' && latestJob?.status === 'queued';
  // 事件窗口（首拉 + SSE）：事件流的 live 与 stage-state 的轮询都由它裁决。
  // 有活动 job 时首拉按 5s 重拉：新 run 归档只能这样被发现（SSE 订阅的是首拉拿到的 run）。
  const feed = useEventWindow(did, {
    onJobUpdate: jobUpdates.onJobUpdate,
    activeJob: cardMode === 'active',
  });
  // 实时逐页预览不走 run 归档事件流：preview_ready 在持久事件流里（见
  // usePersistentEvents），页面刷新由它直接更新 previewPages 缓存驱动。
  // 两路并存、任一 live 就快轮询：事件流还在增长，或有活动 job（前者要 run 归档才活）。
  const eventsLive = isRunLive(feed.events);
  const fastRefetch = eventsLive || cardMode === 'active';
  // stage-state 的轮询用「事件流是否还在增长」（isRunLive；归档截断时会多轮询，
  // 但 live 一律由基线裁决，见 lib/timeline.ts 的注释）。`live` 给文档头徽标用。
  const timeline = useTimelineStages(did, feed.events, {
    refetchMs: fastRefetch ? STAGE_STATE_LIVE_REFETCH_MS : 0,
    // job 驱动的那一段：新 run 的 stage-state 还没落盘时，只有 job 记录说“阶段在跑”。
    job: latestJob,
  });
  const documentQuery = useDocument(did, {
    refetchMs: DOCUMENT_LIVE_REFETCH_MS,
    // 时间线 live / 有活动 job → 一直按 2s 轮询；否则只在**编译未落定**时轮询
    // （草稿一改，详情里的 `compile.status`/`stale` 就是唯一真信号；ok/停手后自然停）。
    refetchWhen: (doc) =>
      timeline.live || cardMode === 'active' || compileUnsettled(doc?.compile),
  });
  const doc = documentQuery.data;
  const inspectorWidth = useUiStore((state) => state.inspectorWidth);

  if (documentQuery.isError) {
    const described = describeApiError(documentQuery.error);
    return (
      <ScrollArea className="h-full p-s7" data-od-id="workbench-error">
        <ErrorCard
          data-od-id="error-card"
          title={described.title}
          message={`${described.message}（did: ${did}）`}
          detail={described.detail}
        >
          <Button onClick={() => void documentQuery.refetch()} disabled={documentQuery.isFetching}>
            重试
          </Button>
          <LinkButton href="#/library">返回文件库</LinkButton>
        </ErrorCard>
      </ScrollArea>
    );
  }

  const layoutStyle = { '--inspw': `${inspectorWidth}px` } as CSSProperties;

  return (
    <div
      className="wb-grid h-full"
      style={layoutStyle}
      data-od-id="workbench"
      data-view={view}
      data-did={did}
    >
      <section
        aria-label="中栏内容"
        className="col-start-1 row-start-1 flex min-h-0 min-w-0 flex-col bg-canvas"
      >
        <div
          data-od-id="doc-header"
          className="flex h-10 flex-none items-center gap-s3 overflow-hidden border-b border-hair bg-parchment px-s4"
        >
          <span className="min-w-0 flex-1 truncate font-serif text-md text-ink-2">
            {doc === undefined ? did : (doc.title ?? doc.did)}
          </span>
          {doc === undefined ? null : (
            <>
              <DocumentStatusBadge
                stageSummary={doc.stage_summary}
                live={timeline.live}
                queued={queued}
              />
              <JobControls did={did} document={doc} jobs={jobsQuery.data} />
            </>
          )}
        </div>

        <div className="min-h-0 flex-1" data-od-id="stage">
          {view === 'archive' ? (
            // 归档视图：预览区换成版本列表（右侧面板给同一份数据的摘要）。
            <div className="h-full min-h-0" data-od-id="archive-panel">
              <ArchiveView did={did} compile={doc?.compile} />
            </div>
          ) : (
            <div className="h-full min-h-0">
              <PreviewArea did={did} />
            </div>
          )}
        </div>
      </section>

      <Gutter id="inspector" className="col-start-2 row-start-1" />
      <InspectorPanel did={did} view={view} feed={feed} compileEvents={persistent.events} />
    </div>
  );
}
