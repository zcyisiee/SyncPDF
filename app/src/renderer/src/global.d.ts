import type { SyncPdfApi } from '../../preload/index';

/** preload 白名单 API（src/preload/index.ts）是渲染进程到达主进程的唯一途径。 */
declare global {
  interface Window {
    syncpdf: SyncPdfApi;
  }
}
