/**
 * SyncPDF 主进程入口：窗口、sidecar 生命周期、协议注册、凭据。
 */
import { app, BrowserWindow, protocol, shell } from 'electron';
import { join } from 'node:path';
import { SidecarManager } from './sidecar';
import { AllowedRoots, FILE_PROTOCOL, registerFileProtocol } from './protocol-handler';
import { registerIpc, broadcastEvent, type IpcDeps } from './ipc';
import type { EngineEvent } from '../shared/protocol';

/** 开发模式判定（electron-vite 注入）。 */
const isDev = !app.isPackaged;

// 自定义协议必须在 app ready 前注册为 privileged（支持 fetch/stream/标准 URL 语义）
protocol.registerSchemesAsPrivileged([
  { scheme: FILE_PROTOCOL, privileges: { stream: true, standard: true, supportFetchAPI: true } },
]);

/** 全局单例。 */
let mainWindow: BrowserWindow | null = null;
let sidecar: SidecarManager | null = null;

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
    backgroundColor: '#1f1f1f',
    webPreferences: {
      // 安全基线（§11）：渲染进程零 Node
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      // electron-vite 产物为 ESM（package type:module → out/preload/index.mjs）
      preload: join(__dirname, '../preload/index.mjs'),
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

/**
 * sidecar 启动配置：
 * - 引擎二进制存在时跑 `syncpdf-cli run --protocol 1`；
 * - 引擎尚未构建（M1 未完成）时回退 fake-sidecar（dev 与测试）。
 * 环境变量 SYNCPDF_ENGINE 覆盖二进制路径。
 */
function sidecarCommand(): { cmd: string; args: string[] } {
  const override = process.env.SYNCPDF_ENGINE;
  if (override !== undefined && override !== '') {
    return { cmd: override, args: ['run', '--protocol', '1'] };
  }
  // fake-sidecar 用 node 运行（仓库 app/scripts/fake-sidecar.mjs）
  return { cmd: process.execPath, args: [join(app.getAppPath(), 'scripts', 'fake-sidecar.mjs')] };
}

function startSidecar(): SidecarManager {
  const manager = new SidecarManager({
    onEvent: (event: EngineEvent) => broadcastEvent(event),
    onLog: (line) => {
      // stderr 为引擎 tracing 日志（人读）；API key 不会出现在其中（§11）
      console.log(`[sidecar] ${line}`);
    },
    onStateChange: (state) => {
      console.log(`[sidecar] state=${state}`);
    },
  });
  const { cmd, args } = sidecarCommand();
  void manager.start({ cmd, args });
  return manager;
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
    const roots = new AllowedRoots();
    // 应用数据目录始终在白名单（预览产物、缓存）
    roots.add(app.getPath('userData'));
    roots.add(app.getPath('downloads'));

    registerFileProtocol(roots);

    sidecar = startSidecar();
    const deps: IpcDeps = { sidecar, roots };
    registerIpc(deps);

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
}

export { mainWindow, sidecar };
