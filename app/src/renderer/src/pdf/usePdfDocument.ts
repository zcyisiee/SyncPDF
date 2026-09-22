/**
 * 渲染进程共享的 PDF 文档缓存 + React 绑定。
 * 源栏与译文栏共用一个 loader（同一路径只解析一次）。
 */
import { useEffect, useState } from 'react';
import { PdfDocumentLoader, preloadReadBytes, type OpenDocument } from './PdfDocumentLoader';
import { documentInitParameters, getDocument, type PDFDocumentProxy } from './pdfjs';

/** 真实 pdf.js 打开实现。 */
const openWithPdfJs: OpenDocument = async (data) =>
  getDocument(documentInitParameters(data)).promise;

/** 全局单例（换文档时由 App 调 `clear()`）。 */
export const sharedPdfLoader = new PdfDocumentLoader({
  readBytes: preloadReadBytes,
  open: openWithPdfJs,
});

export interface PdfDocumentHandle {
  doc: PDFDocumentProxy | null;
  loading: boolean;
  /** 加载失败信息（文件还没写完 / 非 PDF / 白名单外）。 */
  error: string | null;
}

const EMPTY: PdfDocumentHandle = { doc: null, loading: false, error: null };

interface LoadState {
  /** `path\0revision`，空串 = 还没加载过任何东西。 */
  key: string;
  path: string | null;
  doc: PDFDocumentProxy | null;
  error: string | null;
}

/** `path` + `revision` → 缓存键。 */
export function loadKey(path: string | null, revision: number): string {
  return path === null || path === '' ? '' : `${path}\u0000${revision}`;
}

/**
 * 加载并订阅一个 PDF 文档。`revision` 变化触发重载（译文 PDF 被引擎重写）。
 *
 * 重载期间**保留同一路径的旧 doc**：译文栏在 `page_ready` 洪水下不会整栏闪成
 * "加载中"，只有真正换文件（path 变了）才清空。
 */
export function usePdfDocument(path: string | null, revision = 0): PdfDocumentHandle {
  const key = loadKey(path, revision);
  const [state, setState] = useState<LoadState>({ key: '', path: null, doc: null, error: null });

  useEffect(() => {
    if (key === '' || path === null) return;
    let cancelled = false;
    sharedPdfLoader
      .load(path, revision)
      .then((doc) => {
        if (!cancelled) setState({ key, path, doc, error: null });
      })
      .catch((error: unknown) => {
        if (!cancelled) setState({ key, path, doc: null, error: describeError(error) });
      });
    return () => {
      cancelled = true;
    };
  }, [key, path, revision]);

  if (key === '') return EMPTY;
  const samePath = state.path === path;
  return {
    doc: samePath ? state.doc : null,
    loading: state.key !== key,
    error: state.key === key ? state.error : null,
  };
}

function describeError(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error);
}
