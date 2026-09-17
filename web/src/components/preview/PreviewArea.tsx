/**
 * 预览区（§3 第 3 列）：工具条 + 适宽纸页画布 + bbox 叠加层。
 *
 * 数据来源全部是真产物：`GET /artifacts`（mono 首选 → dual → 无产物占位）、
 * `GET /documents/{did}`（页数）、`GET /geometry?kind=parse|layout&page=N`（bbox）。
 * 渲染的是**产物 PDF**，bbox 来自解析快照——对齐基准是页面渲染像素，所以适宽 scale 与
 * bbox 换算共用同一个 pdf.js viewport（`PdfCanvas` 交回的 scale=1 viewport 上 `clone({scale})`）。
 *
 * 三种模式：译文=单页产物（mono/dual）；原文=单页 source.pdf（无 source.pdf 时按钮禁用，
 * 误选也给出明确出口）；对照=左右双页（左源右译，bbox 层只叠在译侧）。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';

import { ApiError, describeApiError } from '../../lib/api';
import {
  artifactUrl,
  bboxModeForView,
  clampPage,
  geometryBboxes,
  pickPreviewArtifacts,
  type BboxMode,
  type Box,
  type GeometryBboxes,
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
  useDocument,
  useDraft,
  useGeometry,
  useJobs,
  usePatchDraftMutation,
} from '../../lib/queries';
import type { WorkbenchView } from '../../lib/routing';
import { readStoredBboxMode, useUiStore } from '../../stores/ui';
import { Button } from '../ui/Button';
import { ErrorCard } from '../ui/ErrorCard';
import { BboxEditor } from '../edit/BboxEditor';
import { BboxLayer, pdfToScreen, type PdfPointViewport, type ScreenViewport } from './BboxLayer';
import { CompileBar } from './CompileBar';
import { DownloadButton } from './DownloadButton';
import { PdfCanvas } from './PdfCanvas';
import type { PdfPageInfo } from './PdfCanvas';
import { PreviewToolbar } from './PreviewToolbar';

/** 适宽 scale 的钳制区间（brief：0.5–3）。 */
const FIT_SCALE_MIN = 0.5;
const FIT_SCALE_MAX = 3;

interface BboxPaneData {
  mode: 'parse' | 'layout';
  data: GeometryBboxes;
  selectedId: string | null;
  onSelect: (id: string) => void;
}

/**
 * 单页容器：自己测容器宽算适宽 scale（对照模式两页各有各的宽），把 scale 报给工具条，
 * 并把渲染 viewport 交给 bbox 层——两者共用同一个 viewport，所以叠加层与 canvas 像素对齐。
 */
function PreviewPane({
  url,
  pageNumber,
  bbox,
  overlay,
  odId,
  onPageInfo,
  onScale,
  emptyState,
}: {
  url: string | null;
  pageNumber: number;
  bbox: BboxPaneData | null;
  /**
   * 叠加在 bbox 层之上的编辑层：**渲染函数**而不是元素——编辑层要用与 canvas 同一个
   * viewport 做 `pdfToScreen` 换算，而那个 viewport 只在这个 pane 里算出来。
   */
  overlay?: (viewport: ScreenViewport & PdfPointViewport) => ReactNode;
  odId: string;
  onPageInfo?: (info: PdfPageInfo) => void;
  onScale?: (scale: number) => void;
  emptyState: ReactNode;
}) {
  const containerRef = useRef<HTMLDivElement | null>(null);
  const [containerWidth, setContainerWidth] = useState<number | null>(null);
  const [info, setInfo] = useState<PdfPageInfo | null>(null);

  useEffect(() => {
    const element = containerRef.current;
    if (element === null) return;
    const update = () => setContainerWidth(element.clientWidth);
    update();
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(update);
    observer.observe(element);
    return () => observer.disconnect();
  }, [url]);

  const scale =
    info !== null && containerWidth !== null && info.viewport.width > 0
      ? Math.min(
          Math.max(containerWidth / info.viewport.width, FIT_SCALE_MIN),
          FIT_SCALE_MAX,
        )
      : 1;

  useEffect(() => {
    onScale?.(scale);
  }, [onScale, scale]);

  const viewport = info === null ? null : info.viewport.clone({ scale });

  return (
    <div ref={containerRef} className="min-h-0 min-w-0 flex-1 overflow-auto">
      {url === null ? (
        <div className="grid h-full place-items-center p-s6">{emptyState}</div>
      ) : (
        <>
          {info !== null && info.viewport.rotation !== 0 ? (
            <p
              data-od-id="preview-rotation-warning"
              className="mx-auto mt-s4 w-fit rounded border border-hair bg-run-soft px-s3 py-[3px] text-tiny text-run-ink"
            >
              该页 rotation={info.viewport.rotation}°：bbox 叠加按 pdf.js viewport 变换处理
            </p>
          ) : null}
          <div
            data-od-id={odId}
            className="relative mx-auto my-s5"
            style={
              viewport === null
                ? undefined
                : { width: `${viewport.width}px`, height: `${viewport.height}px` }
            }
          >
            <PdfCanvas
              url={url}
              pageNumber={pageNumber}
              scale={scale}
              onPage={(next) => {
                setInfo(next);
                onPageInfo?.(next);
              }}
            />
            {viewport !== null && bbox !== null ? (
              <BboxLayer
                boxes={bbox.data.boxes}
                viewport={viewport}
                mode={bbox.mode}
                cropbox={bbox.data.cropbox}
                selectedId={bbox.selectedId}
                onSelect={bbox.onSelect}
              />
            ) : null}
            {viewport === null ? null : overlay?.(viewport)}
          </div>
        </>
      )}
    </div>
  );
}

export function PreviewArea({ did, view }: { did: string; view: WorkbenchView }) {
  const detailQuery = useDocument(did);
  // 文档本身不存在时不发产物请求：避免在「文档不存在」错误卡旁边再冒一个产物清单错误卡
  const artifactsQuery = useArtifacts(detailQuery.isSuccess ? did : null);
  const artifacts = artifactsQuery.data;
  // W10：草稿（选中段的 box 覆盖 + 编译状态条的草稿修订号）+ 活动 job（编辑只读判据）
  const draftQuery = useDraft(detailQuery.isSuccess ? did : null);
  const jobsQuery = useJobs(detailQuery.isSuccess ? did : null);
  const patchDraft = usePatchDraftMutation(did);
  const draft = draftQuery.data;
  const compile = detailQuery.data?.compile ?? null;
  const busyJob = activeJob(jobsQuery.data);
  // 契约也会挡（409 document_busy），这里提前置只读：编译中就不要让人白改
  const editingLocked = compile?.status === 'running' || busyJob !== null;
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
  const selectedParagraphId = useUiStore((state) => state.selectedParagraphId);
  const setSelectedParagraph = useUiStore((state) => state.setSelectedParagraph);

  const [pdfPageInfo, setPdfPageInfo] = useState<{ url: string; numPages: number } | null>(null);
  const [paneScale, setPaneScale] = useState(1);
  // 用户显式选过 bbox 图层（持久化 ieet.bboxMode）之后，视图默认值不再覆盖它
  const bboxPickedByUser = useRef(readStoredBboxMode() !== null);

  const { target, source } = useMemo(
    () => pickPreviewArtifacts(artifacts ?? []),
    [artifacts],
  );

  // did 变化：页码回 1、清空选中（段落 id 属于某个文档）
  useEffect(() => {
    resetPreviewForDocument(did);
  }, [did, resetPreviewForDocument]);

  useEffect(() => {
    if (!bboxPickedByUser.current) setBboxMode(bboxModeForView(view));
  }, [view, setBboxMode]);

  const targetUrl =
    target === null
      ? null
      : withArtifactRevision(
          artifactUrl(did, target.name),
          // 预览的就是这次编译发布的那个产物时才带修订号：编译一结束 URL 变 →
          // `PdfCanvas` 按 url 重挂载 → 真的重新取字节（否则 pdf.js 还拿着旧 PDF）
          target.name === compileArtifactKey(compile) ? compileArtifactRevision(compile) : null,
        );
  const sourceUrl = source === null ? null : artifactUrl(did, source.name);
  const primaryUrl = previewMode === 'source' ? sourceUrl : targetUrl;

  // 页数优先用文档详情（契约字段）；pdf.js 自己报的页数作为兜底，但要属当前主 PDF
  const numPages = pdfPageInfo !== null && pdfPageInfo.url === primaryUrl ? pdfPageInfo.numPages : null;
  const pageCount = detailQuery.data?.pages ?? numPages ?? 1;
  const page = clampPage(previewPage, pageCount);

  // 原文模式只叠识别框（源侧没有译文概念）；其余模式按 bbox 图层开关取值。
  // 没有可渲染 PDF 时不发 geometry 请求（没有画布可叠）。
  const geometryKind: Exclude<BboxMode, 'off'> | null =
    primaryUrl === null || bboxMode === 'off' ? null : previewMode === 'source' ? 'parse' : bboxMode;
  const geometryQuery = useGeometry(did, geometryKind, page);
  const coords = geometryQuery.data ?? null;
  const bboxData = useMemo(() => (coords === null ? null : geometryBboxes(coords)), [coords]);
  const bboxUnavailable = geometryKind !== null && geometryQuery.isSuccess && coords === null;

  const handleSelect = useCallback(
    (id: string) => setSelectedParagraph(id),
    [setSelectedParagraph],
  );
  const handlePageInfo = useCallback(
    (info: PdfPageInfo) => {
      if (primaryUrl !== null) setPdfPageInfo({ url: primaryUrl, numPages: info.numPages });
    },
    [primaryUrl],
  );
  const chooseBboxMode = useCallback(
    (mode: BboxMode) => {
      bboxPickedByUser.current = true;
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
            onSelect: handleSelect,
          }
        : null,
    [bboxData, layerMode, handleSelect, selectedParagraphId],
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
   * 选中段的可拖拽编辑层：只在**版面框（`pdf_native`）+ 译文侧**开（源侧没有可写的 box）。
   * box 优先用草稿覆盖（拖动后的值），否则用该页几何基线；屏幕矩形交给 `pdfToScreen`。
   */
  const buildOverlay = useCallback(
    (withLayer: boolean) =>
      (viewport: ScreenViewport & PdfPointViewport): ReactNode => {
        if (!withLayer || selectedParagraphId === null || layerMode !== 'layout') return null;
        const item = bboxData?.boxes.find((row) => row.id === selectedParagraphId);
        if (item === undefined) return null;
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
      page={page}
      pageCount={pageCount}
      scale={paneScale}
      sourceAvailable={source !== null}
      paged={primaryUrl !== null}
      onPageChange={setPreviewPage}
      onBboxModeChange={chooseBboxMode}
      download={
        <DownloadButton did={did} compile={compile} quality={detailQuery.data?.quality ?? null} />
      }
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
                : `保存段落框失败：${describeApiError(patchError).message}`}
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
  if (primaryUrl === null) {
    const noProduct = target === null;
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
          <PreviewPane
            key="source"
            url={sourceUrl}
            pageNumber={page}
            bbox={null}
            odId="preview-canvas-source"
            emptyState={
              <p className="max-w-[32ch] text-center text-tiny text-ink-4">
                该文档没有 source.pdf（原文 PDF 在上传时写入），对照模式的左侧不可用。
              </p>
            }
          />
        ) : null}
        <PreviewPane
          key="primary"
          url={primaryUrl}
          pageNumber={page}
          bbox={buildBbox(true)}
          overlay={buildOverlay(previewMode !== 'source')}
          odId="preview-canvas"
          onPageInfo={handlePageInfo}
          onScale={setPaneScale}
          emptyState={null}
        />
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 min-w-0 flex-col" data-od-id="preview-area">
      {toolbar}
      {compileBar}
      {patchNotice}
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
