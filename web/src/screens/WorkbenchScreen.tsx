import { describeApiError } from '../lib/api';
import { useDocument } from '../lib/queries';
import { WORKBENCH_VIEWS, type WorkbenchView } from '../lib/routing';
import { Button, LinkButton } from '../components/ui/Button';
import { ErrorCard } from '../components/ui/ErrorCard';
import { ScrollArea } from '../components/ui/ScrollArea';
import { DocumentStatusBadge } from '../components/ui/StatusBadge';
import { Gutter } from '../components/shell/Gutter';
import { InspectorPanel } from '../components/shell/InspectorPanel';
import { ScreenFrame } from '../components/shell/ScreenFrame';
import { Timeline } from '../components/shell/Timeline';
import { ViewRail } from '../components/shell/ViewRail';
import { PreviewArea } from '../components/preview/PreviewArea';
import { useUiStore } from '../stores/ui';
import type { CSSProperties } from 'react';

/** 预览区接真 PDF 的四个视图（bbox 默认模式不同，见 `bboxModeForView`）。 */
const PREVIEW_VIEWS: readonly WorkbenchView[] = ['progress', 'layout', 'translate', 'check'];

/**
 * `#/d/:did/*` 工作台壳（§3 栅格 + §8.1/8.2）：
 * 56px 图标栏 → 220px 视图栏 → gutter → 预览区 → gutter → 360px 右侧面板 → gutter → 96px 时间线。
 * 三条分隔条宽度由 `--vrw`/`--inspw`/`--tlh` 驱动（写在本容器行内 style 上）。
 */
export function WorkbenchScreen({ did, view }: { did: string; view: WorkbenchView }) {
  const documentQuery = useDocument(did);
  const doc = documentQuery.data;
  const activeView = WORKBENCH_VIEWS.find((candidate) => candidate.id === view) ?? WORKBENCH_VIEWS[0];
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
        <DocumentStatusBadge stageSummary={doc.stage_summary} />
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
        <ViewRail did={did} view={view} doc={doc} />
        <Gutter id="viewrail" className="col-start-2 row-start-1" />
        <section
          aria-label="预览区"
          data-od-id="stage"
          className="col-start-3 row-start-1 flex min-h-0 min-w-0 flex-col"
        >
          {PREVIEW_VIEWS.includes(view) ? (
            <PreviewArea did={did} view={view} />
          ) : (
            <div
              data-od-id="view-placeholder"
              className="grid min-h-0 flex-1 place-items-center overflow-auto p-s6"
            >
              <div className="max-w-[42ch] text-center">
                <p className="font-serif text-md font-medium leading-[1.4] text-ink-2">
                  {activeView.label}视图待 W12 接入
                </p>
                <p className="mt-s3 font-mono text-tiny text-ink-4">
                  #/d/{did}/{view}
                </p>
              </div>
            </div>
          )}
        </section>
        <Gutter id="inspector" className="col-start-4 row-start-1" />
        <InspectorPanel />
        <Gutter id="timeline" className="col-span-full row-start-2" />
        <Timeline did={did} />
      </div>
    </ScreenFrame>
  );
}
