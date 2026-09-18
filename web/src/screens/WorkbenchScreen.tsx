import { describeApiError } from '../lib/api';
import { compileUnsettled } from '../lib/download';
import { isRunLive } from '../lib/events';
import { jobCardMode } from '../lib/jobs';
import { DOCUMENT_LIVE_REFETCH_MS, useDocument, useJobs } from '../lib/queries';
import type { WorkbenchView } from '../lib/routing';
import { Button, LinkButton } from '../components/ui/Button';
import { ErrorCard } from '../components/ui/ErrorCard';
import { ScrollArea } from '../components/ui/ScrollArea';
import { DocumentStatusBadge } from '../components/ui/StatusBadge';
import { Gutter } from '../components/shell/Gutter';
import { InspectorPanel } from '../components/shell/InspectorPanel';
import { ScreenFrame } from '../components/shell/ScreenFrame';
import { Timeline } from '../components/shell/Timeline';
import { ViewRail } from '../components/shell/ViewRail';
import { ActiveJobCard } from '../components/jobs/ActiveJobCard';
import { StartJobCard } from '../components/jobs/StartJobCard';
import { ArchiveView } from '../components/archive/ArchiveView';
import { PreviewArea } from '../components/preview/PreviewArea';
import { useEventWindow } from '../components/events/useEventWindow';
import { useJobUpdates } from '../components/events/useJobUpdates';
import { useTimelineStages } from '../components/events/useTimelineStages';
import { useUiStore } from '../stores/ui';
import type { CSSProperties } from 'react';

/** 预览区接真 PDF 的四个视图（bbox 默认模式不同，见 `bboxModeForView`）。 */
const PREVIEW_VIEWS: readonly WorkbenchView[] = ['progress', 'layout', 'translate', 'check'];

/** 事件流说 live 时 stage-state 的轮询间隔（brief：live 2s 否则不轮询）。 */
const STAGE_STATE_LIVE_REFETCH_MS = 2_000;

/**
 * `#/d/:did/*` 工作台壳（§3 栅格 + §8.1/8.2）：
 * 56px 图标栏 → 220px 视图栏 → gutter → 预览区 → gutter → 360px 右侧面板 → gutter → 96px 时间线。
 * 三条分隔条宽度由 `--vrw`/`--inspw`/`--tlh` 驱动（写在本容器行内 style 上）。
 */
export function WorkbenchScreen({ did, view }: { did: string; view: WorkbenchView }) {
  // job_update（W14）：收到就立刻失效对应查询，并把“刚推过”的时间戳交给 useJobs 做轮询节流。
  const jobUpdates = useJobUpdates(did);
  // jobs 轮询（W08/W14）：有 queued/running 时 5s（SSE 的 job_update 是快路径），否则 30s；
  // 刚收到推送的静默窗口内连这 5s 那一次都跳过（SSE 已经刷新过一轮）。
  const jobsQuery = useJobs(did, { quietSinceMs: jobUpdates.pushedAtMs });
  const cardMode = jobCardMode(jobsQuery.data);
  const latestJob = jobsQuery.data?.[0] ?? null;
  // 进度层：事件窗口（首拉 + SSE）→ 时间线段（stage-state 基线 + 事件流 live + job 驱动 live）。
  const feed = useEventWindow(did, {
    onJobUpdate: jobUpdates.onJobUpdate,
    // 活动 job 期间首拉按 5s 重拉：新 run 归档只能这样被发现（SSE 订阅的是首拉拿到的 run）。
    activeJob: cardMode === 'active',
  });
  // 两路并存、任一 live 就快轮询：事件流还在增长，或有活动 job（前者要 run 归档才活）。
  const eventsLive = isRunLive(feed.events);
  const fastRefetch = eventsLive || cardMode === 'active';
  // stage-state 的轮询用「事件流是否还在增长」（brief 冻结的 isRunLive；归档截断时会多轮询，
  // 但时间线/徽标的 live 一律由基线裁决，见 lib/timeline.ts 的注释）。
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
  const viewrailWidth = useUiStore((state) => state.viewrailWidth);
  const inspectorWidth = useUiStore((state) => state.inspectorWidth);
  const timelineHeight = useUiStore((state) => state.timelineHeight);
  const inspectorCollapsed = useUiStore((state) => state.inspectorCollapsed);

  const meta =
    doc === undefined ? null : (
      <>
        <span className="max-w-[34ch] truncate font-serif text-sm text-ink-2">
          {doc.title ?? doc.did}
        </span>
        <DocumentStatusBadge stageSummary={doc.stage_summary} live={timeline.live} />
      </>
    );

  if (documentQuery.isError) {
    const described = describeApiError(documentQuery.error);
    return (
      <ScreenFrame>
        <ScrollArea className="p-s7">
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
      </ScreenFrame>
    );
  }

  const layoutStyle = {
    '--vrw': `${viewrailWidth}px`,
    '--inspw': inspectorCollapsed ? '0px' : `${inspectorWidth}px`,
    '--tlh': `${timelineHeight}px`,
  } as CSSProperties;

  return (
    <ScreenFrame meta={meta}>
      <div
        className="wb-grid h-full"
        style={layoutStyle}
        data-od-id="workbench"
        data-view={view}
        data-did={did}
      >
        <ViewRail did={did} view={view} doc={doc} live={timeline.live} />
        <Gutter id="viewrail" className="col-start-2 row-start-1" />
        <section
          aria-label="预览区"
          data-od-id="stage"
          className="col-start-3 row-start-1 flex min-h-0 min-w-0 flex-col"
        >
          {view === 'progress' ? (
            <div
              className="flex-none border-b border-hair bg-ivory"
              data-od-id="job-panel"
            >
              {cardMode === 'start' ? (
                <StartJobCard did={did} document={doc} />
              ) : latestJob === null ? null : (
                <ActiveJobCard did={did} job={latestJob} document={doc} />
              )}
            </div>
          ) : null}
          {PREVIEW_VIEWS.includes(view) ? (
            <div className="min-h-0 flex-1">
              <PreviewArea did={did} view={view} />
            </div>
          ) : (
            // 归档视图（W12）：预览区换成版本列表（右侧面板给同一份数据的摘要）。
            <div className="min-h-0 flex-1" data-od-id="archive-panel">
              <ArchiveView did={did} compile={doc?.compile} />
            </div>
          )}
        </section>
        <Gutter id="inspector" className="col-start-4 row-start-1" />
        <InspectorPanel did={did} view={view} feed={feed} />
        <Gutter id="timeline" className="col-span-full row-start-2" />
        <Timeline did={did} segments={timeline.segments} unavailable={timeline.isError} />
      </div>
    </ScreenFrame>
  );
}
