/**
 * ipcMain.handle 注册（§13 IPC 设计）。
 *
 * 渲染进程只能通过 preload 暴露的 `window.syncpdf.*` 白名单到达这里；
 * 引擎事件经 `webContents.send('engine:event')` 回推（低频通道；高频事件后续走 MessagePort）。
 */
import { BrowserWindow, dialog, ipcMain } from 'electron';
import type { EngineEvent, Request } from '../shared/protocol';
import type { SidecarManager } from './sidecar';
import type { AllowedRoots } from './protocol-handler';
import { readCredentials, writeCredentials, type Credentials } from './credentials';

export const ENGINE_EVENT_CHANNEL = 'engine:event';

export interface IpcDeps {
  sidecar: SidecarManager;
  roots: AllowedRoots;
  /**
   * 发送引擎事件到渲染进程的回调。缺省由 index.ts 在 SidecarManager 构造时
   * 直接广播（见 main/index.ts 的 onEvent）；保留此参数供未来 MessagePort
   * 高频通道替换（§13）。
   */
  sendEvent?: (event: EngineEvent) => void;
}

/** 事件广播到所有窗口（引擎事件低频通道）。 */
export function broadcastEvent(event: EngineEvent): void {
  for (const window of BrowserWindow.getAllWindows()) {
    if (!window.isDestroyed()) {
      window.webContents.send(ENGINE_EVENT_CHANNEL, event);
    }
  }
}

/** 渲染进程请求 payload（preload 已剔除多余字段后转发）。 */
export type RendererRequest = Request;

export function registerIpc(deps: IpcDeps): void {
  const { sidecar, roots } = deps;
  // SidecarManager 的事件在构造时注入（见 index.ts），这里只负责 IPC 面；
  // deps.sendEvent 预留给未来 MessagePort 高频通道。

  ipcMain.handle('engine:configure', (_event, request: unknown) => {
    assertRequest(request);
    sidecar.send(request);
    return { ok: true };
  });

  ipcMain.handle('engine:run', (_event, request: unknown) => {
    assertRequest(request);
    // 用户打开的文件目录加入协议白名单（§11：用户打开的文件）
    const paths = request as unknown as Record<string, unknown>;
    if (typeof paths.input === 'string' && paths.input !== '') {
      roots.add(dirnameOf(paths.input));
    }
    if (typeof paths.output === 'string' && paths.output !== '') {
      roots.add(dirnameOf(paths.output));
    }
    sidecar.send(request);
    return { ok: true };
  });

  ipcMain.handle('engine:retranslate', (_event, request: unknown) => {
    assertRequest(request);
    sidecar.send(request);
    return { ok: true };
  });

  ipcMain.handle('engine:applyEdit', (_event, request: unknown) => {
    assertRequest(request);
    sidecar.send(request);
    return { ok: true };
  });

  ipcMain.handle('engine:export', (_event, request: unknown) => {
    assertRequest(request);
    sidecar.send(request);
    return { ok: true };
  });

  ipcMain.handle('engine:cancel', async () => {
    // §9.1：cancel 请求走 stdin（引擎优雅收尾）；随后 stop() 兜底 EOF + 5s kill
    try {
      sidecar.send({ type: 'cancel' });
    } catch {
      // sidecar 已退出（stdin 不可写）：直接走 stop 清理
    }
    await sidecar.stop();
    return { ok: true };
  });

  ipcMain.handle('app:openFile', async () => {
    const result = await dialog.showOpenDialog({
      properties: ['openFile'],
      filters: [{ name: 'PDF', extensions: ['pdf'] }],
    });
    if (result.canceled || result.filePaths.length === 0) return null;
    const path = result.filePaths[0];
    roots.add(dirnameOf(path));
    return path;
  });

  ipcMain.handle('credentials:read', () => readCredentials());

  ipcMain.handle('credentials:write', (_event, credentials: unknown) => {
    if (
      typeof credentials !== 'object' ||
      credentials === null ||
      typeof (credentials as Record<string, unknown>).api_key !== 'string'
    ) {
      throw new Error('凭据结构非法');
    }
    return writeCredentials(credentials as Credentials);
  });
}

function assertRequest(request: unknown): asserts request is RendererRequest {
  // 基本形状校验（完整校验在 SidecarManager.send 内 isRequest）
  if (
    typeof request !== 'object' ||
    request === null ||
    typeof (request as Record<string, unknown>).type !== 'string'
  ) {
    throw new Error('请求形状非法');
  }
}

function dirnameOf(path: string): string {
  const separator = path.includes('/') ? '/' : '\\';
  const index = path.lastIndexOf(separator);
  return index > 0 ? path.slice(0, index) : path;
}
