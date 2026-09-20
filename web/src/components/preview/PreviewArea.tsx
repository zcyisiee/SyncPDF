/** Continuous PDF reader with per-page viewport-aligned geometry and draft editing. */
import { useCallback, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';

import { ApiError, describeApiError } from '../../lib/api';
import {
  artifactUrl,
  assetUrl,
  clampPage,
  geometryBboxes,
  pickPreviewArtifacts,
  type BboxMode,
  type Box,
} from '../../lib/preview';
import {
  draftParagraphOf,
  layoutBox,
  layoutInputsOf,
  layoutPatch,
  layoutValuesOf,
} from '../../lib/draft';
import { activeJob } from '../../lib/jobs';
import {
  compileArtifactKey,
  compileArtifactRevision,
  withArtifactRevision,
} from '../../lib/download';
import {
  useArtifacts,
  usePreviewPages,
  useDocument,
  useDraft,
  useGeometry,
  useJobs,
  usePatchDraftMutation,
} from '../../lib/queries';
import { categoryVisible } from '../../lib/bbox';
import { readVisibility, useBboxStore } from '../../stores/bbox';
import { BboxLegend } from './BboxLegend';
import { useUiStore } from '../../stores/ui';
import { Button } from '../ui/Button';
import { ErrorCard } from '../ui/ErrorCard';
import { DocumentStatusBadge } from '../ui/StatusBadge';
import { BboxEditor } from '../edit/BboxEditor';
import { pdfToScreen, type PdfPointViewport, type ScreenViewport } from './BboxLayer';
import { CompileBar } from './CompileBar';
import { ExportButton } from './ExportButton';
import { DownloadButton } from './DownloadButton';
import { ContinuousPdfPane, type BboxPaneData, type ReaderPosition } from './ContinuousPdfPane';
import type { PdfPageInfo } from './PdfCanvas';
import { PreviewToolbar } from './PreviewToolbar';

type PreviewProps = { did: string };

export function PreviewArea({ did }: PreviewProps) {
  return <DocumentPreview key={did} did={did} />;
}

function DocumentPreview({ did }: PreviewProps) {
  const bboxPreferences = useBboxStore();
  const visibility = useMemo(() => bboxPreferences.documents[did] ?? readVisibility(did), [bboxPreferences.documents, did]);
  const detailQuery = useDocument(did);
  // 文档本身不存在时不发产物请求：避免在「文档不存在」错误卡旁边再冒一个产物清单错误卡
  const artifactsQuery = useArtifacts(detailQuery.isSuccess ? did : null);
  const previewPagesQuery = usePreviewPages(detailQuery.isSuccess ? did : null);
  const artifacts = artifactsQuery.data;
  // W10：草稿（选中段的 box 覆盖 + 编译状态条的草稿修订号）+ 活动 job（编辑只读判据）
  const draftQuery = useDraft(detailQuery.isSuccess ? did : null);
  const jobsQuery = useJobs(detailQuery.isSuccess ? did : null);
  const patchDraft = usePatchDraftMutation(did);
  const draft = draftQuery.data;
  const compile = detailQuery.data?.compile ?? null;
  const busyJob = activeJob(jobsQuery.data);
  // 契约也会挡（409 document_busy），这里提前置只读：编译中就不要让人白改
  const editingLocked = compile?.status === 'running' || (busyJob !== null && busyJob.effective_scope !== 'block');
  const editingLockedReason =
    compile?.status === 'running'
      ? '编译中…：编译结束后可继续编辑'
      : busyJob === null
        ? null
        : `该文档有活动任务 ${busyJob.job_id}（${busyJob.action}）：任务期间草稿只读`;

  const previewMode = useUiStore((state) => state.previewMode);
  const setPreviewMode = useUiStore((state) => state.setPreviewMode);
  const bboxMode = useUiStore((state) => state.bboxMode);
  const setBboxMode = useUiStore((state) => state.setBboxMode);
  const previewPage = useUiStore((state) => state.previewPage);
  const setPreviewPage = useUiStore((state) => state.setPreviewPage);
  const resetPreviewForDocument = useUiStore((state) => state.resetPreviewForDocument);
  const selectedParagraphIds = useUiStore((state) => state.selectedParagraphIds);
  const selectedParagraphId = useUiStore((state) => state.selectedParagraphId);
  const selectParagraph = useUiStore((state) => state.selectParagraph);

  const [pdfPageInfo, setPdfPageInfo] = useState<{ url: string; numPages: number } | null>(null);
  const [sourcePageInfo, setSourcePageInfo] = useState<{ url: string; numPages: number } | null>(null);
  const [panePositions, setPanePositions] = useState<Record<string, ReaderPosition>>({});
  const linked = useUiStore((state) => state.compareLinked);
  const [position, setPosition] = useState<ReaderPosition | null>(null);
  const [navigation, setNavigation] = useState<{ page: number; revision: number; pane?: string }>({ page: 1, revision: 0 });
  const navigate = (next: number) => {
    setPosition({ pane: position?.pane ?? 'primary', page: next, fraction: 0 });
    setPreviewPage(next);
    setNavigation((previous) => ({ page: next, revision: previous.revision + 1,
      pane: !linked && previewMode === 'compare' ? position?.pane ?? 'primary' : undefined }));
  };
  const onPosition = useCallback((next: ReaderPosition, programmatic = false) => {
    setPanePositions((previous) => {
      const saved = previous[next.pane];
      return saved?.page === next.page && saved.fraction === next.fraction ? previous : { ...previous, [next.pane]: next };
    });
    setPosition((previous) => {
      if (programmatic && previous && previous.pane !== next.pane) return previous;
      if (previous?.pane === next.pane && previous.page === next.page && previous.fraction === next.fraction) return previous;
      return next;
    });
    if (!programmatic) setPreviewPage(next.page);
  }, [setPreviewPage]);

  useEffect(() => { if (position) setPreviewPage(position.page); }, [position, setPreviewPage]);

  const { target, source } = useMemo(
    () => pickPreviewArtifacts(artifacts ?? []),
    [artifacts],
  );

  // did 变化：页码回 1、清空选中（段落 id 属于某个文档）
  useEffect(() => {
    resetPreviewForDocument(did);
  }, [did, resetPreviewForDocument]);

  const localPreview = detailQuery.data?.preview_asset;
  const targetUrl = localPreview ? `/api/v1/documents/${encodeURIComponent(did)}/assets/${localPreview}` :
    target === null
      ? null
      : withArtifactRevision(
          artifactUrl(did, target.name),
          // 预览的就是这次编译发布的那个产物时才带修订号：编译一结束 URL 变 →
          // `PdfCanvas` 按 url 重挂载 → 真的重新取字节（否则 pdf.js 还拿着旧 PDF）
          target.name === compileArtifactKey(compile) ? compileArtifactRevision(compile) : null,
        );
  const sourceUrl = source === null ? null : artifactUrl(did, source.name);
  const pageSources = useMemo(() => Object.fromEntries(
    (previewPagesQuery.data?.pages ?? []).map((item) => [item.page, assetUrl(did, item.asset)]),
  ) as Record<number, string>, [did, previewPagesQuery.data]);
  const primaryUrl = previewMode === 'source' ? sourceUrl : targetUrl ?? sourceUrl;

  // PDF 的实际页数优先（源/译页数可能不同）；加载前以文档详情估计。
  const numPages = pdfPageInfo !== null && pdfPageInfo.url === primaryUrl ? pdfPageInfo.numPages : null;
  const primaryPageCount = numPages ?? detailQuery.data?.pages ?? 1;
  const sourcePageCount = sourcePageInfo?.url === sourceUrl ? sourcePageInfo.numPages : detailQuery.data?.pages ?? 1;
  const pageCount = previewMode === 'compare' && !linked && position?.pane === 'source' ? sourcePageCount : primaryPageCount;
  const page = clampPage(previewPage, pageCount);

  // 原文模式只叠识别框（源侧没有译文概念）；其余模式按 bbox 图层开关取值。
  // 没有可渲染 PDF 时不发 geometry 请求（没有画布可叠）。
  const geometryKind: Exclude<BboxMode, 'off'> | null =
    primaryUrl === null || bboxMode === 'off' ? null : previewMode === 'source' ? 'parse' : bboxMode;
  const geometryQuery = useGeometry(did, geometryKind, page);
  const coords = geometryQuery.data ?? null;
  const bboxData = useMemo(() => (coords === null ? null : geometryBboxes(coords, previewMode === 'source')), [coords, previewMode]);
  const sourceGeometry = useGeometry(did, previewMode === 'compare' && bboxMode === 'parse' && sourceUrl !== null ? 'parse' : null, page);
  const legendCoords = previewMode === 'compare' && bboxMode === 'parse' ? sourceGeometry.data : coords;
  const legendLabels = previewMode === 'target' && geometryKind === 'parse' ? coords?.paragraph_labels : legendCoords?.labels;
  const bboxUnavailable = geometryKind !== null && geometryQuery.isSuccess && coords === null;

  // 普通点击单选、shift 点击进出多选集合（store 里维护集合与主选中段）
  const handleSelect = useCallback(
    (id: string, opts?: { shift?: boolean }) => selectParagraph(id, { extend: opts?.shift }),
    [selectParagraph],
  );
  const handlePageInfo = useCallback(
    (info: PdfPageInfo) => {
      if (primaryUrl !== null) setPdfPageInfo({ url: primaryUrl, numPages: info.numPages });
    },
    [primaryUrl],
  );
  const handleSourcePageInfo = useCallback((info: PdfPageInfo) => {
    if (sourceUrl !== null) setSourcePageInfo({ url: sourceUrl, numPages: info.numPages });
  }, [sourceUrl]);
  const chooseBboxMode = useCallback(
    (mode: BboxMode) => {
      setBboxMode(mode);
    },
    [setBboxMode],
  );

  // 转换用的坐标系取自服务端标注（`coord_system`），不由前端猜
  const layerMode: 'parse' | 'layout' = bboxData?.coordSystem === 'pdf_native' ? 'layout' : 'parse';

  const buildBbox = useCallback(
    (withLayer: boolean): BboxPaneData | null =>
      withLayer && bboxData !== null
        ? {
            mode: layerMode,
            data: bboxData,
            selectedId: selectedParagraphId,
            selectedIds: selectedParagraphIds,
            onSelect: handleSelect,
          }
        : null,
    [bboxData, layerMode, handleSelect, selectedParagraphId, selectedParagraphIds],
  );

  /**
   * 拖拽松手（`BboxEditor` 已做过 viewport 逆变换）→ PATCH 草稿的 `layout.box`。
   * 只带这一段的 `layout`（整对象替换语义）：数值覆盖照**当前草稿**回填（不拿面板里正在改的
   * 输入，避免把未保存的编辑状态写进去），也不碰别的段落。
   */
  const handleBoxCommit = useCallback(
    (id: string, box: Box) => {
      const revision = draft?.revision;
      if (revision === undefined) return;
      const current = draftParagraphOf(draft, id)?.layout ?? null;
      const layout = layoutPatch(layoutValuesOf(layoutInputsOf(current)), box, current);
      patchDraft.mutate({ baseRevision: revision, paragraphs: { [id]: { layout } } });
    },
    [draft, patchDraft],
  );

  /**
   * 选中段的可拖拽编辑层：只在**译文框（`pdf_native`）+ 译文侧**开（源侧没有可写的 box）。
   * box 优先用草稿覆盖（拖动后的值），否则用该页几何基线；屏幕矩形交给 `pdfToScreen`。
   */
  const buildOverlay = useCallback(
    (withLayer: boolean) =>
      (viewport: ScreenViewport & PdfPointViewport): ReactNode => {
        if (geometryKind === null || !withLayer || selectedParagraphId === null || layerMode !== 'layout') return null;
        const item = bboxData?.boxes.find((row) => row.id === selectedParagraphId);
        if (item === undefined || !categoryVisible(visibility, item.label)) return null;
        const cropbox = bboxData?.cropbox ?? null;
        const coordSystem = bboxData?.coordSystem ?? 'pdf_native';
        const draftBox = layoutBox(draftParagraphOf(draft, selectedParagraphId)?.layout);
        return (
          <BboxEditor
            id={selectedParagraphId}
            rect={pdfToScreen(draftBox ?? item.box, viewport, coordSystem, cropbox)}
            viewport={viewport}
            cropbox={cropbox}
            disabled={editingLocked}
            disabledReason={editingLockedReason ?? undefined}
            onCommit={(box) => handleBoxCommit(selectedParagraphId, box)}
          />
        );
      },
    [
      bboxData,
      visibility,
      geometryKind,
      draft,
      editingLocked,
      editingLockedReason,
      handleBoxCommit,
      layerMode,
      selectedParagraphId,
    ],
  );

  const toolbar = (
    <PreviewToolbar
      page={clampPage(previewPage, pageCount)}
      pageCount={pageCount}
      sourceAvailable={source !== null}
      paged={primaryUrl !== null}
      onPageChange={navigate}
      onBboxModeChange={chooseBboxMode}
      // 文档名 + 阶段状态徽标住工具条最左（中栏的临时文档头行已删除）。这里只给
      // `stage_summary` 的终态判断：live/queued 由任务控制（actionbar 的 JobControls）
      // 自身徽标承担，两处不抢同一句话。
      title={detailQuery.data?.title ?? did}
      status={<DocumentStatusBadge stageSummary={detailQuery.data?.stage_summary} />}
      download={<>
        <ExportButton did={did} />
        <DownloadButton did={did} compile={compile} quality={detailQuery.data?.quality ?? null} />
      </>}
    />
  );

  const compileBar = (
    <CompileBar
      did={did}
      compile={compile}
      draftRevision={draft?.revision ?? null}
      busyJobId={busyJob?.job_id ?? null}
    />
  );

  // bbox 拖拽的 PATCH 失败（409/422）不静默：拖完的框是「预览态」，得说清为何存不上
  const patchError = patchDraft.error ?? null;
  const patchNotice =
    patchError === null
      ? null
      : (
          <p
            data-od-id="bbox-patch-error"
            data-error-code={patchError instanceof ApiError ? patchError.code : 'unknown'}
            className="flex-none border-b border-hair bg-err-soft px-s5 py-[5px] text-tiny text-err-ink"
          >
            {patchError instanceof ApiError && patchError.code === 'document_busy'
              ? '编译中，稍后再试：活动任务期间草稿只读（409 document_busy）'
              : patchError instanceof ApiError && patchError.code === 'revision_conflict'
                ? '草稿已被其它会话改动：刷新后重新拖拽（409 revision_conflict）'
                : `保存译文框失败：${describeApiError(patchError).message}`}
          </p>
        );

  if (artifactsQuery.isError) {
    return (
      <div className="flex h-full min-h-0 flex-col" data-od-id="preview-area">
        {toolbar}
        {compileBar}
        <div className="min-h-0 flex-1 overflow-auto p-s6">
          <ErrorCard
            data-od-id="preview-error"
            error={artifactsQuery.error}
            title="读取产物清单失败"
          >
            <Button
              onClick={() => void artifactsQuery.refetch()}
              disabled={artifactsQuery.isFetching}
            >
              重试
            </Button>
          </ErrorCard>
        </div>
      </div>
    );
  }

  let content: ReactNode;
  if (artifactsQuery.isPending) {
    content = <div className="p-s6 text-tiny text-ink-4">正在读取 PDF 清单…</div>;
  } else if (primaryUrl === null) {
    const noProduct = previewMode !== 'source' && target === null;
    content = (
      <div className="grid h-full place-items-center p-s6">
        <div
          data-od-id="preview-no-pdf"
          className="max-w-[42ch] text-center"
        >
          <p className="font-serif text-md font-medium leading-[1.4] text-ink-2">
            {noProduct ? '无产物 PDF' : '该文档没有 source.pdf'}
          </p>
          <p className="mt-s3 text-tiny text-ink-4">
            {noProduct
              ? '清单里没有 output/*.pdf 编译产物（mono / dual），编译完成后可在此预览。'
              : '原文 PDF 在上传时写入 source.pdf；该文档没有它，只能看译文预览。'}
          </p>
          {previewMode === 'source' && target !== null ? (
            <Button
              className="mt-s4"
              onClick={() => setPreviewMode('target')}
            >
              切换到译文
            </Button>
          ) : null}
        </div>
      </div>
    );
  } else {
    // 模式切换不重建 DOM：外层 flex 容器与 pane 的 key 固定，
    // 切到对照只是多挂一个源侧 pane，译侧不会重新加载 PDF。
    content = (
      <div className="flex min-h-0 min-w-0 flex-1 gap-s5 px-s5">
        {previewMode === 'compare' ? (
          <ContinuousPdfPane
            key={`source-${did}-${sourceUrl}`}
            url={sourceUrl}
            pageNumber={page}
            did={did}
            pageCount={sourcePageCount}
            initialPosition={panePositions.source ?? null}
            onPageInfo={handleSourcePageInfo}
            navigation={navigation}
            position={linked ? position : null}
            paneId="source"
            onPosition={onPosition}
            geometryKind={bboxMode === 'parse' ? 'parse' : null}
            recognition
            bbox={null}
            odId="preview-canvas-source"
            emptyState={
              <p className="max-w-[32ch] text-center text-tiny text-ink-4">
                该文档没有 source.pdf（原文 PDF 在上传时写入），对照模式的左侧不可用。
              </p>
            }
          />
        ) : null}
        <ContinuousPdfPane
          key={`primary-${did}`}
          url={primaryUrl}
          pageSources={previewMode !== 'source' ? pageSources : undefined}
          pageNumber={page}
          did={did}
          pageCount={primaryPageCount}
          initialPosition={panePositions.primary ?? null}
          navigation={navigation}
          position={linked ? position : null}
          paneId="primary"
          onPosition={onPosition}
          geometryKind={geometryKind}
          recognition={previewMode === 'source'}
          bbox={buildBbox(true)}
          overlay={buildOverlay(previewMode !== 'source')}
          odId="preview-canvas"
          onPageInfo={handlePageInfo}
          emptyState={null}
        />
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 min-w-0 flex-col" data-od-id="preview-area">
      {toolbar}
      {compileBar}
      {busyJob !== null && (previewPagesQuery.data?.pages.length ?? 0) > 0 ? <p data-od-id="stream-preview-note" className="px-s5 py-1 text-tiny text-run-ink">实时翻译预览 · 未完成段落保留原文，最终结果仍在生成</p> : null}
      {patchNotice}
      {geometryKind !== null ? <BboxLegend did={did} boxes={bboxData?.boxes ?? []} labels={legendLabels?.length ? legendLabels : undefined} /> : null}
      {bboxUnavailable ? (
        <p
          data-od-id="preview-bbox-unavailable"
          className="flex-none border-b border-hair bg-sand px-s5 py-[5px] text-tiny text-ink-3"
        >
          该页无{geometryKind === 'layout' ? '版面' : '解析'}数据（产物缺失），预览本身仍可用。
        </p>
      ) : null}
      <div className="flex min-h-0 min-w-0 flex-1">{content}</div>
    </div>
  );
}
