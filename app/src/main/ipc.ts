/**
 * ipcMain 注册。渲染进程只能经 preload 暴露的 `window.syncpdf.*` 白名单到达这里；
 * 推送通道：`engine:event`（DocEngineEvent）、`library:changed`（LibraryDoc）、
 * `library:removed`（id）、`engine:log`（stderr 行）。
 */
import { BrowserWindow, Menu, dialog, ipcMain, shell } from 'electron';
import type { DocEngineEvent, LibraryDoc, MetaSource } from '../shared/library';
import type { AllowedRoots } from './protocol-handler';
import { requireEdits, type EngineQueue } from './engine';
import type { Library } from './library';
import { readAllowedFileBytes } from './file-bytes';

export interface IpcDeps {
  library: Library;
  queue: EngineQueue;
  roots: AllowedRoots;
}

/** 推送到所有窗口。 */
export function broadcast(channel: string, payload: unknown): void {
  for (const window of BrowserWindow.getAllWindows()) {
    if (!window.isDestroyed()) window.webContents.send(channel, payload);
  }
}

export const pushEngineEvent = (event: DocEngineEvent): void => broadcast('engine:event', event);
export const pushDocChanged = (doc: LibraryDoc): void => broadcast('library:changed', doc);
export const pushLog = (line: string): void => broadcast('engine:log', line);

const isId = (v: unknown): v is string => typeof v === 'string' && /^[0-9a-f]{16}$/.test(v);

function requireId(v: unknown): string {
  if (!isId(v)) throw new Error('论文 id 非法');
  return v;
}

export function registerIpc({ library, queue, roots }: IpcDeps): void {
  /** 加入若干本地 PDF 并排队翻译；返回加入 / 已存在的条目。 */
  const addPaths = (paths: string[]): LibraryDoc[] => {
    const docs: LibraryDoc[] = [];
    for (const path of paths) {
      const { doc, added } = library.addFile(path);
      docs.push(doc);
      if (added) {
        pushDocChanged(doc);
        queue.enqueue(doc.id);
      }
    }
    return docs;
  };

  const remove = async (id: string): Promise<boolean> => {
    const doc = library.get(id);
    if (doc === null) return false;
    const window = BrowserWindow.getFocusedWindow();
    const options = {
      type: 'warning' as const,
      buttons: ['删除', '取消'],
      defaultId: 1,
      cancelId: 1,
      message: `删除「${doc.title}」？`,
      detail: '原文、译文与缓存都会从论文库中删除。',
    };
    const { response } =
      window === null ? await dialog.showMessageBox(options) : await dialog.showMessageBox(window, options);
    if (response !== 0) return false;
    queue.forget(id);
    library.remove(id);
    broadcast('library:removed', id);
    return true;
  };

  const reveal = (id: string): void => {
    const doc = library.get(id);
    if (doc !== null) shell.showItemInFolder(doc.translatedPath ?? doc.sourcePath);
  };

  ipcMain.handle('library:list', () => library.list());
  ipcMain.handle('library:snapshot', (_e, id: unknown) => library.snapshot(requireId(id)));
  ipcMain.handle('library:queue', () => ({ current: queue.currentDoc, queued: [...queue.queued()] }));

  ipcMain.handle('library:pick', async () => {
    const result = await dialog.showOpenDialog({
      properties: ['openFile', 'multiSelections'],
      filters: [{ name: 'PDF', extensions: ['pdf'] }],
    });
    return result.canceled ? [] : addPaths(result.filePaths);
  });

  ipcMain.handle('library:add', (_e, paths: unknown) => {
    if (!Array.isArray(paths) || !paths.every((p) => typeof p === 'string' && p !== '')) {
      throw new Error('路径列表非法');
    }
    return addPaths(paths as string[]);
  });

  ipcMain.handle('library:remove', (_e, id: unknown) => remove(requireId(id)));
  ipcMain.handle('library:reveal', (_e, id: unknown) => reveal(requireId(id)));
  ipcMain.handle('library:markOpened', (_e, id: unknown) => library.markOpened(requireId(id)));

  ipcMain.handle('library:updateMeta', (_e, id: unknown, meta: unknown, source: unknown) => {
    const docId = requireId(id);
    if (source !== 'pdf_info' && source !== 'user') throw new Error('来源非法');
    if (typeof meta !== 'object' || meta === null) throw new Error('元数据非法');
    const { title, authors } = meta as { title?: unknown; authors?: unknown };
    const str = (v: unknown): string | null => (typeof v === 'string' ? v : null);
    if (library.updateMeta(docId, { title: str(title), authors: str(authors) }, source as MetaSource)) {
      pushDocChanged(library.get(docId) as LibraryDoc);
    }
  });

  /** 卡片右键菜单（原生）。 */
  ipcMain.handle('library:contextMenu', (event, id: unknown) => {
    const docId = requireId(id);
    const doc = library.get(docId);
    if (doc === null) return;
    const busy = doc.status === 'queued' || doc.status === 'running';
    const menu = Menu.buildFromTemplate([
      busy
        ? { label: '取消翻译', click: () => queue.cancel(docId) }
        : { label: doc.status === 'done' ? '重新翻译' : '翻译', click: () => queue.enqueue(docId) },
      { type: 'separator' },
      { label: '在 Finder 中显示', click: () => reveal(docId) },
      { label: '删除', click: () => void remove(docId) },
    ]);
    const window = BrowserWindow.fromWebContents(event.sender);
    menu.popup(window === null ? undefined : { window });
  });

  ipcMain.handle('engine:enqueue', (_e, id: unknown) => queue.enqueue(requireId(id)));
  ipcMain.handle('engine:edit', (_e, id: unknown, requests: unknown) =>
    queue.edit(requireId(id), requireEdits(requests)),
  );
  ipcMain.handle('engine:cancel', (_e, id: unknown) => queue.cancel(requireId(id)));

  // 渲染进程读 PDF 字节（pdf.js `getDocument({ data })`）。白名单校验在 file-bytes.ts。
  ipcMain.handle('app:readFileBytes', (_e, path: unknown) => readAllowedFileBytes(path, roots));
}
