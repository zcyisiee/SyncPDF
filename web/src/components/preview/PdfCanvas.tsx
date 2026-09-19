/**
 * 单页 PDF 渲染（pdf.js API，不使用 viewer.html）。
 *
 * 职责：加载文档（`getDocument({url})`，走服务端 artifact 的 Range 请求）→ 取页 →
 * 按父级算好的 `scale` 渲染到 canvas（devicePixelRatio 放大保证清晰）→ 把该页 scale=1 的
 * viewport 交回父级（适宽计算与 bbox 换算共用同一个 viewport）。页切换时 `RenderTask.cancel()`
 * 取消上一个渲染任务，避免旧页后到把新页覆盖成花屏。
 *
 * 状态重置走 React 的 `key` 重挂载（url / 重试计数器），不在 effect 里同步 setState。
 */
import { useEffect, useRef, useState } from 'react';
import type {
  PDFDocumentLoadingTask,
  PDFDocumentProxy,
  PDFPageProxy,
  PageViewport,
} from 'pdfjs-dist';
import type { MutableRefObject } from 'react';

import { ErrorCard } from '../ui/ErrorCard';
import { Button } from '../ui/Button';

export interface PdfPageInfo {
  /** 该页 scale=1 的 pdf.js viewport（保留 PDF user space → 屏幕的完整变换）。 */
  viewport: PageViewport;
  /** 文档总页数（pdf.js 读取的真实值）。 */
  numPages: number;
}

export interface PdfCanvasProps {
  /** artifact 预览 URL（支持 Range；不要先 fetch 成 blob）。 */
  url: string;
  /** 1 基页码。 */
  pageNumber: number;
  /** CSS px / PDF 点；由父级按容器宽算好（适宽）。 */
  scale: number;
  /** 每页加载完成后回调（父级据此算适宽与 bbox 换算）。 */
  onPage?: (info: PdfPageInfo) => void;
  onRenderComplete?: () => void;
  onError?: (cause: unknown) => void;
  className?: string;
}

/** 取消类异常（页切换/缩放打断上一次渲染）不是错误，不显示错误卡。 */
function isCancellation(cause: unknown): boolean {
  return (
    typeof cause === 'object' &&
    cause !== null &&
    'name' in cause &&
    (cause as { name: unknown }).name === 'RenderingCancelledException'
  );
}

function useLatest<T>(value: T): MutableRefObject<T> {
  const ref = useRef(value);
  useEffect(() => {
    ref.current = value;
  });
  return ref;
}

// Nearby page canvases share one loading task/worker per artifact. The last
// consumer releases it, so navigating away also cancels pending Range requests.
const documents = new Map<string, { users: number; promise: Promise<PDFDocumentProxy>; release: () => void }>();
export function acquireDocument(url: string) {
  let entry = documents.get(url);
  if (!entry) {
    let task: PDFDocumentLoadingTask | null = null;
    let released = false;
    const promise = import('../../lib/pdf').then(({ loadPdfDocument }) => {
      if (released) throw new Error('PDF reader closed');
      task = loadPdfDocument(url);
      return task.promise;
    });
    entry = { users: 0, promise, release: () => { released = true; if (task) void task.destroy(); } };
    documents.set(url, entry);
    const pending = entry;
    void promise.catch(() => { if (documents.get(url) === pending) documents.delete(url); });
  }
  entry.users += 1;
  const acquired = entry;
  return { promise: entry.promise, release: () => {
    acquired.users -= 1;
    if (acquired.users === 0) {
      if (documents.get(url) === acquired) documents.delete(url);
      acquired.release();
    }
  } };
}

interface PdfCanvasPageProps extends PdfCanvasProps {
  onRetry: () => void;
}

function PdfCanvasPage({
  url,
  pageNumber,
  scale,
  onPage,
  onRenderComplete,
  onError,
  className,
  onRetry,
}: PdfCanvasPageProps) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const [doc, setDoc] = useState<PDFDocumentProxy | null>(null);
  const [page, setPage] = useState<PDFPageProxy | null>(null);
  const [error, setError] = useState<unknown>(null);
  const onPageRef = useLatest(onPage);
  const onRenderCompleteRef = useLatest(onRenderComplete);
  const onErrorRef = useLatest(onError);
  useEffect(() => { if (error !== null) onErrorRef.current?.(error); }, [error, onErrorRef]);

  // 1) 加载文档：卸载/切换时销毁 loading task（中断进行中的 Range 请求）。
  useEffect(() => {
    let cancelled = false;
    const handle = acquireDocument(url);
    void handle.promise.then((loaded) => {
      if (!cancelled) setDoc(loaded);
    }).catch((cause: unknown) => {
      if (!cancelled) setError(cause);
    });
    return () => {
      cancelled = true;
      handle.release();
    };
  }, [url]);

  // 2) 取页：并把 scale=1 的 viewport 交回父级。
  useEffect(() => {
    if (doc === null) return;
    let cancelled = false;
    void (async () => {
      try {
        const loadedPage = await doc.getPage(pageNumber);
        if (cancelled) return;
        setPage(loadedPage);
        onPageRef.current?.({
          viewport: loadedPage.getViewport({ scale: 1 }),
          numPages: doc.numPages,
        });
      } catch (cause) {
        if (!cancelled) setError(cause);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [doc, pageNumber, onPageRef]);

  // 3) 渲染：canvas 后备缓存按 devicePixelRatio 放大，CSS 尺寸仍是 CSS px（bbox 换算用同一坐标系）。
  useEffect(() => {
    if (page === null) return;
    const canvas = canvasRef.current;
    const context = canvas?.getContext('2d') ?? null;
    if (canvas === null || context === null) return;
    let cancelled = false;
    const ratio = window.devicePixelRatio > 0 ? window.devicePixelRatio : 1;
    const viewport = page.getViewport({ scale });
    canvas.width = Math.max(1, Math.round(viewport.width * ratio));
    canvas.height = Math.max(1, Math.round(viewport.height * ratio));
    canvas.style.width = `${viewport.width}px`;
    canvas.style.height = `${viewport.height}px`;
    const task = page.render({
      canvasContext: context,
      viewport: page.getViewport({ scale: scale * ratio }),
    });
    task.promise
      .then(() => {
        if (!cancelled) {
          setError(null);
          onRenderCompleteRef.current?.();
        }
      })
      .catch((cause: unknown) => {
        if (!cancelled && !isCancellation(cause)) setError(cause);
      });
    return () => {
      cancelled = true;
      task.cancel();
    };
  }, [page, scale, onRenderCompleteRef]);

  // Release decoded page resources when a virtualized page leaves the window.
  useEffect(() => () => { page?.cleanup(); }, [page]);

  if (error !== null) {
    return (
      <ErrorCard
        data-od-id="preview-error"
        error={error}
        title="PDF 预览失败"
        message="无法加载或渲染该产物 PDF（Range 请求 / 解析错误）。"
      >
        <Button onClick={onRetry}>重试</Button>
      </ErrorCard>
    );
  }

  if (page === null) {
    return (
      <div
        data-od-id="preview-loading"
        className="grid h-full w-full place-items-center border border-hair bg-ivory text-tiny text-ink-4"
      >
        正在加载 PDF…
      </div>
    );
  }

  return <canvas ref={canvasRef} data-od-id="pdf-canvas" className={className ?? 'block'} />;
}

/** `url` 或重试计数器变化 → 内层重挂载（pdf.js 文档与渲染状态一起重置）。 */
export function PdfCanvas({ url, pageNumber, scale, onPage, onRenderComplete, onError, className }: PdfCanvasProps) {
  const [attempt, setAttempt] = useState(0);
  return (
    <PdfCanvasPage
      key={`${url}#${pageNumber}#${attempt}`}
      url={url}
      pageNumber={pageNumber}
      scale={scale}
      onPage={onPage}
      onRenderComplete={onRenderComplete}
      onError={onError}
      className={className}
      onRetry={() => setAttempt((value) => value + 1)}
    />
  );
}
