/**
 * 单页画布（M2-06）。
 *
 * - devicePixelRatio：画布位图尺寸 = CSS 尺寸 × DPR（上限见 `canvasPixelSize`），
 *   再把 DPR 作为 `transform` 传给 pdf.js，保证高倍屏不糊；
 * - 懒渲染：IntersectionObserver（rootMargin 让上下各预渲染一屏）；
 * - `revision` 变化 → 重新渲染本页（译文栏 `page_ready` 增量刷新走这条路）；
 * - 卸载 / 参数变更时 `RenderTask.cancel()`，避免并发写同一画布。
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import { canvasPixelSize } from './geometry';
import type { PDFDocumentProxy, PDFPageProxy, PageViewport, RenderTask } from './pdfjs';

export interface PageCanvasProps {
  doc: PDFDocumentProxy;
  /** 1 基页号（pdf.js 约定）。 */
  pageNumber: number;
  /** 缩放：数字 = 固定倍率；'fit-width' = 按 `containerWidth` 自适应。 */
  zoom: number | 'fit-width';
  /** 可用宽度（CSS px），fit-width 时用。 */
  containerWidth: number;
  /** 内容修订号：变化即重渲染本页。 */
  revision?: number;
  /** 视口就绪回调（叠加层要用同一个 viewport 做换算）。 */
  onViewport?: (pageNumber: number, viewport: PageViewport | null) => void;
  /** 画布之上的叠加层（段落框）。 */
  renderOverlay?: (viewport: PageViewport) => React.ReactNode;
  /** 页边距（fit-width 计算时扣除）。 */
  gutter?: number;
}

/** 上下各预渲染约一屏。 */
const LAZY_ROOT_MARGIN = '600px 0px';

export function PageCanvas({
  doc,
  pageNumber,
  zoom,
  containerWidth,
  revision = 0,
  onViewport,
  renderOverlay,
  gutter = 24,
}: PageCanvasProps): JSX.Element {
  const wrapperRef = useRef<HTMLDivElement | null>(null);
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const pageRef = useRef<PDFPageProxy | null>(null);
  const taskRef = useRef<RenderTask | null>(null);
  const [page, setPage] = useState<PDFPageProxy | null>(null);
  // 无 IntersectionObserver（jsdom / 老环境）时退化为"全部立即渲染"
  const [visible, setVisible] = useState(() => typeof IntersectionObserver === 'undefined');
  const [rendered, setRendered] = useState(-1);
  const [failure, setFailure] = useState<string | null>(null);

  // 取页对象（一次），卸载时释放
  useEffect(() => {
    let cancelled = false;
    doc
      .getPage(pageNumber)
      .then((value) => {
        if (cancelled) {
          value.cleanup();
          return;
        }
        pageRef.current = value;
        setPage(value);
      })
      .catch((error: unknown) => {
        if (!cancelled) setFailure(describe(error));
      });
    return () => {
      cancelled = true;
      pageRef.current = null;
      setPage(null);
    };
  }, [doc, pageNumber]);

  /** 基准视口（scale=1）决定 fit-width 的倍率。 */
  const scale = useMemo(() => {
    if (page === null) return 1;
    if (zoom !== 'fit-width') return zoom;
    const base = page.getViewport({ scale: 1 });
    const available = Math.max(80, containerWidth - gutter);
    return base.width > 0 ? available / base.width : 1;
  }, [page, zoom, containerWidth, gutter]);

  const viewport = useMemo(
    () => (page === null ? null : page.getViewport({ scale })),
    [page, scale],
  );

  useEffect(() => {
    onViewport?.(pageNumber, viewport);
  }, [onViewport, pageNumber, viewport]);

  // 可视性（懒渲染）
  useEffect(() => {
    const node = wrapperRef.current;
    if (node === null || typeof IntersectionObserver === 'undefined') return;
    const observer = new IntersectionObserver(
      (entries) => {
        for (const entry of entries) {
          if (entry.isIntersecting) setVisible(true);
        }
      },
      { rootMargin: LAZY_ROOT_MARGIN },
    );
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  // 渲染：可见 + 视口就绪 + （首次 / revision 或 scale 变了）
  const renderKey = `${revision}:${viewport?.width.toFixed(2) ?? ''}x${viewport?.height.toFixed(2) ?? ''}`;
  const renderKeyRef = useRef<string>('');

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!visible || page === null || viewport === null || canvas === null) return;
    if (renderKeyRef.current === renderKey) return;
    renderKeyRef.current = renderKey;

    taskRef.current?.cancel();
    const { width, height, ratio } = canvasPixelSize(
      viewport.width,
      viewport.height,
      typeof window === 'undefined' ? 1 : window.devicePixelRatio,
    );
    canvas.width = width;
    canvas.height = height;
    canvas.style.width = `${viewport.width}px`;
    canvas.style.height = `${viewport.height}px`;
    const context = canvas.getContext('2d');
    if (context === null) {
      setFailure('无法获取 2d 上下文');
      return;
    }
    const task = page.render({
      canvas,
      canvasContext: context,
      viewport,
      // DPR 缩放：viewport 用 CSS 尺寸，位图放大 ratio 倍
      transform: ratio === 1 ? undefined : [ratio, 0, 0, ratio, 0, 0],
    });
    taskRef.current = task;
    task.promise
      .then(() => {
        setFailure(null);
        setRendered(revision);
      })
      .catch((error: unknown) => {
        // 取消是正常路径（快速滚动 / 参数变更）
        if (isCancellation(error)) {
          renderKeyRef.current = '';
          return;
        }
        renderKeyRef.current = '';
        setFailure(describe(error));
      });
    return () => {
      task.cancel();
    };
  }, [visible, page, viewport, renderKey, revision]);

  const cssWidth = viewport?.width ?? Math.max(80, containerWidth - gutter);
  const cssHeight = viewport?.height ?? cssWidth * 1.414;

  return (
    <div
      ref={wrapperRef}
      data-page={pageNumber}
      data-rendered={rendered >= 0 ? 'true' : 'false'}
      style={{
        position: 'relative',
        width: cssWidth,
        height: cssHeight,
        margin: '0 auto 12px',
        background: '#ffffff',
        boxShadow: '0 1px 4px rgba(0,0,0,0.35)',
        flex: '0 0 auto',
      }}
    >
      <canvas ref={canvasRef} style={{ display: 'block', width: cssWidth, height: cssHeight }} />
      {viewport !== null && renderOverlay !== undefined ? renderOverlay(viewport) : null}
      {failure !== null && (
        <div
          style={{
            position: 'absolute',
            inset: 0,
            display: 'grid',
            placeItems: 'center',
            color: 'var(--vscode-errorForeground)',
            fontSize: 12,
            background: 'var(--vscode-editor-background)',
          }}
        >
          第 {pageNumber} 页渲染失败：{failure}
        </div>
      )}
    </div>
  );
}

function isCancellation(error: unknown): boolean {
  const name = (error as { name?: unknown } | null)?.name;
  return name === 'RenderingCancelledException' || name === 'AbortException';
}

function describe(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
