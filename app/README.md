# SyncPDF 桌面壳（app/）

Electron 应用骨架：electron-vite + React 18 + TypeScript 严格模式 + vitest。
视觉系统对齐 VSCode（Dark/Light Modern token、codicons、@vscode-elements）。

## 启动

```bash
cd app
npm install
npm run dev        # 开发（需要显示器）
npm test           # vitest 全部测试
npm run typecheck  # tsc --noEmit
npm run build      # 产物构建到 out/
npm run lint       # eslint
```

引擎二进制尚未构建时，主进程自动回退 `scripts/fake-sidecar.mjs`（读 stdin
JSONL、发事件流）。环境变量 `SYNCPDF_ENGINE` 可指定真实 `syncpdf-cli` 路径。

## 目录

```
app/
  electron.vite.config.ts    # 三进程构建（main / preload / renderer）
  vitest.config.ts           # 测试（node + jsdom 按目录切换）
  scripts/
    fake-sidecar.mjs         # 假 sidecar：configure 确认、run 事件流、cancel/EOF 退出
  src/
    shared/protocol.ts       # JSONL 协议类型 + 守卫（§9；后续由 Rust 导出 schema 校验）
    main/
      index.ts               # 窗口（hiddenInset）、安全基线、协议注册、sidecar 启动
      sidecar.ts             # SidecarManager：spawn / stdin JSONL / stdout 行解析 / 崩溃重启 / EOF+5s kill
      protocol-handler.ts    # syncpdf-file:// 白名单协议
      credentials.ts         # userData/credentials.json 0600 读写
      ipc.ts                 # ipcMain.handle 注册（engine:* / app:openFile / credentials:*）
    preload/index.ts         # contextBridge 白名单（configure/startRun/retranslate/applyEdit/exportDocument/cancel/onEvent/openFile/readCredentials/writeCredentials）
    renderer/
      index.html
      src/
        theme/               # --vscode-* token 两套主题 + codicons
        layout/              # Workbench：TitleBar/ActivityBar/SideBar/EditorArea/Panel/StatusBar（allotment 分栏，布局持久化）
        store/               # documentStore（事件 reducer）/ uiStore（布局、选中、面板）
        views/               # 段落树 / 问题面板 / 输出面板 / 段落编辑（占位深度）
  tests/                     # vitest：sidecar 集成 / protocol 守卫 / documentStore reducer / credentials 0600 / uiStore
```

## 安全基线

- `contextIsolation: true, nodeIntegration: false, sandbox: true`；渲染进程只能用 `window.syncpdf.*`。
- 本地文件只经 `syncpdf-file://`（白名单目录：用户打开的文件、userData）。
- API key 只经 stdin `configure` 传引擎；凭据文件 0o600。
