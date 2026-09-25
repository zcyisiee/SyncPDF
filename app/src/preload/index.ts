/**
 * preload 白名单：只暴露这些方法，渲染进程零 Node 访问。
 * 推送（engine:event / library:changed / library:removed / engine:log）用 `on*` 订阅，返回取消函数。
 */
import { contextBridge, ipcRenderer, webUtils } from 'electron';
import type { DocEngineEvent, DocSnapshot, LibraryDoc } from '../shared/library';

function subscribe<T>(channel: string, listener: (payload: T) => void): () => void {
  const handler = (_event: Electron.IpcRendererEvent, payload: T): void => listener(payload);
  ipcRenderer.on(channel, handler);
  return () => {
    ipcRenderer.removeListener(channel, handler);
  };
}

const api = {
  library: {
    list: (): Promise<LibraryDoc[]> => ipcRenderer.invoke('library:list'),
    snapshot: (id: string): Promise<DocSnapshot> => ipcRenderer.invoke('library:snapshot', id),
    queue: (): Promise<{ current: string | null; queued: string[] }> =>
      ipcRenderer.invoke('library:queue'),
    /** 系统文件框选 PDF 并加入论文库。 */
    pick: (): Promise<LibraryDoc[]> => ipcRenderer.invoke('library:pick'),
    add: (paths: string[]): Promise<LibraryDoc[]> => ipcRenderer.invoke('library:add', paths),
    remove: (id: string): Promise<boolean> => ipcRenderer.invoke('library:remove', id),
    reveal: (id: string): Promise<void> => ipcRenderer.invoke('library:reveal', id),
    markOpened: (id: string): Promise<void> => ipcRenderer.invoke('library:markOpened', id),
    updateMeta: (
      id: string,
      meta: { title?: string | null; authors?: string | null },
      source: 'pdf_info' | 'user',
    ): Promise<void> => ipcRenderer.invoke('library:updateMeta', id, meta, source),
    contextMenu: (id: string): Promise<void> => ipcRenderer.invoke('library:contextMenu', id),
    onChanged: (listener: (doc: LibraryDoc) => void) => subscribe('library:changed', listener),
    onRemoved: (listener: (id: string) => void) => subscribe('library:removed', listener),
  },
  engine: {
    enqueue: (id: string): Promise<void> => ipcRenderer.invoke('engine:enqueue', id),
    cancel: (id: string): Promise<void> => ipcRenderer.invoke('engine:cancel', id),
    onEvent: (listener: (event: DocEngineEvent) => void) => subscribe('engine:event', listener),
    onLog: (listener: (line: string) => void) => subscribe('engine:log', listener),
  },
  /** 拖放进来的 File → 本地路径（Electron 32+ 移除了 File.path）。 */
  pathForFile: (file: File): string => webUtils.getPathForFile(file),
  /**
   * 读白名单内文件的字节（pdf.js 用）。白名单外的路径主进程直接拒绝。
   * 主进程回 Uint8Array（结构化克隆），这里统一成 ArrayBuffer 交给 pdf.js。
   */
  readFileBytes: async (path: string): Promise<ArrayBuffer> => {
    const bytes: Uint8Array = await ipcRenderer.invoke('app:readFileBytes', path);
    return bytes.buffer.slice(bytes.byteOffset, bytes.byteOffset + bytes.byteLength) as ArrayBuffer;
  },
};

contextBridge.exposeInMainWorld('syncpdf', api);

export type SyncPdfApi = typeof api;
