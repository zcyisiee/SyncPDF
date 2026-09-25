/**
 * SyncPDF 主进程入口：窗口、论文库、引擎会话 + 翻译队列、协议注册。
 */
import { app, BrowserWindow, protocol, shell } from 'electron';
import { homedir } from 'node:os';
import { join } from 'node:path';
import { SidecarManager } from './sidecar';
import { AllowedRoots, FILE_PROTOCOL, registerFileProtocol } from './protocol-handler';
import { pushDocChanged, pushEngineEvent, pushLog, registerIpc } from './ipc';
import { Library } from './library';
import { EngineQueue } from './engine';
import type { ConfigureRequest } from '../shared/protocol';

/** 开发模式判定（electron-vite 注入）。 */
const isDev = !app.isPackaged;

// 自定义协议必须在 app ready 前注册为 privileged（支持 fetch/stream/标准 URL 语义）
protocol.registerSchemesAsPrivileged([
  { scheme: FILE_PROTOCOL, privileges: { stream: true, standard: true, supportFetchAPI: true } },
]);

/** 全局单例。 */
let mainWindow: BrowserWindow | null = null;
let sidecar: SidecarManager | null = null;
let library: Library | null = null;

function createWindow(): void {
  mainWindow = new BrowserWindow({
    width: 1440,
    height: 900,
    minWidth: 960,
    minHeight: 600,
    show: false,
    // 自绘标题栏（§12.1）；红绿灯区域由系统交通灯占据
    titleBarStyle: 'hiddenInset',
    trafficLightPosition: { x: 16, y: 16 },
    backgroundColor: '#e5e0d4',
    webPreferences: {
      // 安全基线（§11）：渲染进程零 Node
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      // sandbox 下 preload 必须是 CJS（见 electron.vite.config.ts）
      preload: join(__dirname, '../preload/index.cjs'),
      spellcheck: false,
    },
  });

  mainWindow.on('ready-to-show', () => mainWindow?.show());

  // 外部链接交给系统浏览器，不在壳内导航
  mainWindow.webContents.setWindowOpenHandler(({ url }) => {
    void shell.openExternal(url);
    return { action: 'deny' };
  });

  if (isDev && process.env.ELECTRON_RENDERER_URL !== undefined) {
    void mainWindow.loadURL(process.env.ELECTRON_RENDERER_URL);
  } else {
    void mainWindow.loadFile(join(__dirname, '../renderer/index.html'));
  }
}

/** 数据根目录：`SYNCPDF_HOME` 覆盖，缺省 `~/.sp`。 */
function dataRoot(): string {
  const override = process.env.SYNCPDF_HOME;
  return override !== undefined && override !== '' ? override : join(homedir(), '.sp');
}

/**
 * 引擎命令：`SYNCPDF_ENGINE` 指向 `syncpdf-cli` 时跑长驻会话 `run --protocol 1`；
 * 未设置时回退 fake-sidecar（dev 与测试）。
 */
function sidecarCommand(): { cmd: string; args: string[] } {
  const override = process.env.SYNCPDF_ENGINE;
  if (override !== undefined && override !== '') {
    return { cmd: override, args: ['run', '--protocol', '1'] };
  }
  return { cmd: process.execPath, args: [join(app.getAppPath(), 'scripts', 'fake-sidecar.mjs')] };
}

/** 翻译配置（设置页落地前的默认：agy + gemini-3.8-flash-low）。 */
function configure(root: string): ConfigureRequest {
  return {
    type: 'configure',
    provider: 'agy',
    base_url: null,
    model: 'gemini-3.8-flash-low',
    api_key: null,
    concurrency: 1,
    cache_dir: join(root, 'cache'),
    translator: { kind: 'agy', program: 'agy', model: 'gemini-3.8-flash-low' },
  };
}

function startEngine(lib: Library): { manager: SidecarManager; queue: EngineQueue } {
  const command = sidecarCommand();
  // 队列与会话互相引用：会话事件进队列，队列经会话下发请求。
  let queue: EngineQueue | null = null;
  const manager = new SidecarManager({
    onEvent: (event) => queue?.handleEvent(event),
    onLog: (line) => pushLog(line),
    onStateChange: (state) => queue?.handleSessionState(state),
  });
  queue = new EngineQueue({
    library: lib,
    session: {
      send: (request) => manager.send(request),
      isRunning: () => manager.isRunning(),
      start: () => manager.start(command),
    },
    configure: () => configure(lib.root),
    onEvent: pushEngineEvent,
    onDocChanged: pushDocChanged,
  });
  return { manager, queue };
}

// 单实例锁：第二个实例聚焦既有窗口
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (mainWindow !== null) {
      if (mainWindow.isMinimized()) mainWindow.restore();
      mainWindow.focus();
    }
  });

  app.whenReady().then(() => {
    library = new Library(dataRoot());
    // 渲染进程只读论文库内的文件（原文 blob、译文）
    const roots = new AllowedRoots();
    roots.add(library.root);
    registerFileProtocol(roots);

    const engine = startEngine(library);
    sidecar = engine.manager;
    registerIpc({ library, queue: engine.queue, roots });
    // 上次退出时仍在排队 / 运行的论文重新排队
    for (const doc of [...library.list()].reverse()) {
      if (doc.status === 'queued' || doc.status === 'running') engine.queue.enqueue(doc.id);
    }

    createWindow();

    app.on('activate', () => {
      if (BrowserWindow.getAllWindows().length === 0) createWindow();
    });
  });

  app.on('window-all-closed', () => {
    if (process.platform !== 'darwin') app.quit();
  });

  app.on('before-quit', () => {
    void sidecar?.stop();
  });

  app.on('quit', () => {
    library?.close();
  });
}

export { mainWindow, sidecar };
