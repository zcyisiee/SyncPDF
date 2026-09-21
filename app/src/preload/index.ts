/**
 * preload 白名单（§13）：只暴露这些方法，渲染进程零 Node 访问。
 * 事件经 `engine:event`（webContents.send）推入，`onEvent` 用 ipcRenderer.on 订阅。
 */
import { contextBridge, ipcRenderer } from 'electron';
import type { EngineEvent } from '../shared/protocol';

const api = {
  /** 下发 configure（api_key 只经此通道，不落渲染进程日志）。 */
  configure: (request: {
    provider: string;
    base_url: string;
    model: string;
    api_key: string;
    concurrency: number;
    cache_dir: string;
  }) => ipcRenderer.invoke('engine:configure', request),

  startRun: (request: Record<string, unknown>) => ipcRenderer.invoke('engine:run', request),
  retranslate: (request: Record<string, unknown>) => ipcRenderer.invoke('engine:retranslate', request),
  applyEdit: (request: Record<string, unknown>) => ipcRenderer.invoke('engine:applyEdit', request),
  exportDocument: (request: Record<string, unknown>) => ipcRenderer.invoke('engine:export', request),
  cancel: () => ipcRenderer.invoke('engine:cancel'),

  /** 订阅引擎事件流；返回取消订阅函数。 */
  onEvent: (listener: (event: EngineEvent) => void): (() => void) => {
    const handler = (_event: Electron.IpcRendererEvent, payload: EngineEvent): void => {
      listener(payload);
    };
    ipcRenderer.on('engine:event', handler);
    return () => {
      ipcRenderer.removeListener('engine:event', handler);
    };
  },

  /** 系统文件选择框（PDF）。 */
  openFile: (): Promise<string | null> => ipcRenderer.invoke('app:openFile'),

  readCredentials: (): Promise<{
    provider: string;
    base_url: string;
    model: string;
    api_key: string;
  } | null> => ipcRenderer.invoke('credentials:read'),

  writeCredentials: (credentials: {
    provider: string;
    base_url: string;
    model: string;
    api_key: string;
  }) => ipcRenderer.invoke('credentials:write', credentials),
};

contextBridge.exposeInMainWorld('syncpdf', api);

export type SyncPdfApi = typeof api;
