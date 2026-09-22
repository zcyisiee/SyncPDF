/**
 * PDFDocumentProxy 缓存（M2-06）。
 *
 * - 按 `path` 缓存已打开的文档，同一路径重复请求复用同一个 Promise；
 * - 按 `revision` 失效：译文 PDF 在 run 过程中被引擎就地重写，`page_ready`
 *   把 documentStore 的 revision +1，这里看到更高的 revision 就销毁旧 proxy 重新加载；
 * - 字节从主进程 `readFileBytes` 拿（白名单校验在主进程做），渲染进程零 Node。
 *
 * 依赖以构造参数注入（`readBytes` / `open`），便于在 vitest 里不带 pdf.js 单测缓存逻辑。
 */
import type { PDFDocumentProxy } from './pdfjs';

/** 打开一个 PDF 字节流。 */
export type OpenDocument = (data: ArrayBuffer) => Promise<PDFDocumentProxy>;

/** 读取白名单内文件的字节。 */
export type ReadBytes = (path: string) => Promise<ArrayBuffer>;

export interface PdfDocumentLoaderOptions {
  readBytes: ReadBytes;
  open: OpenDocument;
}

interface CacheEntry {
  revision: number;
  /** 未决 / 已完成的文档。加载失败时从缓存里剔除，允许重试。 */
  promise: Promise<PDFDocumentProxy>;
}

/** 默认实现：走 preload 的 `window.syncpdf.readFileBytes`。 */
export function preloadReadBytes(path: string): Promise<ArrayBuffer> {
  return window.syncpdf.readFileBytes(path);
}

export class PdfDocumentLoader {
  private readonly entries = new Map<string, CacheEntry>();
  private readonly options: PdfDocumentLoaderOptions;

  constructor(options: PdfDocumentLoaderOptions) {
    this.options = options;
  }

  /**
   * 取文档。`revision` 比缓存里的新则重新加载（旧 proxy 销毁）；
   * 相同或更旧则直接复用缓存。
   */
  load(path: string, revision = 0): Promise<PDFDocumentProxy> {
    const cached = this.entries.get(path);
    if (cached !== undefined && cached.revision >= revision) {
      return cached.promise;
    }
    const promise = this.options
      .readBytes(path)
      .then((bytes) => this.options.open(bytes))
      .catch((error: unknown) => {
        // 失败不留缓存，下一次（比如文件还没写完）可以重试
        if (this.entries.get(path)?.promise === promise) {
          this.entries.delete(path);
        }
        throw error;
      })
      .finally(() => {
        // 旧 proxy **等新的就位后**再销毁：重载期间组件还握着它渲染，
        // 提前 destroy 会让在途的 getPage/render 报错闪一下。
        if (cached !== undefined) void destroyQuietly(cached.promise);
      });
    const entry: CacheEntry = { revision, promise };
    this.entries.set(path, entry);
    return promise;
  }

  /** 已缓存的 revision（未缓存 → null）。 */
  revisionOf(path: string): number | null {
    return this.entries.get(path)?.revision ?? null;
  }

  /** 缓存条目数（测试 / 诊断用）。 */
  get size(): number {
    return this.entries.size;
  }

  /** 释放单个文档。 */
  release(path: string): void {
    const entry = this.entries.get(path);
    if (entry === undefined) return;
    this.entries.delete(path);
    void destroyQuietly(entry.promise);
  }

  /** 释放全部（换文档 / 卸载）。 */
  clear(): void {
    for (const path of [...this.entries.keys()]) {
      this.release(path);
    }
  }
}

/**
 * 销毁 proxy，吞掉加载本身就失败的情况。
 * pdf.js 6 的 `PDFDocumentProxy` 没有 `destroy()`，销毁入口在它的 `loadingTask` 上。
 */
async function destroyQuietly(promise: Promise<PDFDocumentProxy>): Promise<void> {
  try {
    const document = await promise;
    await document.loadingTask.destroy();
  } catch {
    // 加载失败 / 已销毁：无需处理
  }
}
