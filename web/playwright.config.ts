import { defineConfig, devices } from '@playwright/test';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

// e2e 用独立端口（8793 serve / 5175 dev），避免和手工冒烟的 8787/5173 打架；
// 两个 webServer 都由 Playwright 托管：真 `bdt serve --root tmp` + Vite dev（/api 走同源代理）。
const webRoot = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(webRoot, '..');
const servePort = 8793;
const devPort = 5175;

export default defineConfig({
  testDir: './e2e',
  // 预览用例共享一棵 dev server，且都读同一份 tmp/ 真数据：串行跑，避免相互抢带宽
  fullyParallel: false,
  workers: 1,
  reporter: 'list',
  timeout: 90_000,
  expect: { timeout: 20_000 },
  use: {
    baseURL: `http://127.0.0.1:${devPort}`,
    trace: 'on-first-retry',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: [
    {
      command: `${repoRoot}/.venv/bin/bdt serve --root ${repoRoot}/tmp --port ${servePort}`,
      cwd: repoRoot,
      url: `http://127.0.0.1:${servePort}/api/v1/health`,
      reuseExistingServer: false,
      timeout: 60_000,
      stdout: 'pipe',
      stderr: 'pipe',
      // macOS 会把 editable 安装的 .pth 标成 hidden（见 web/README.md），此时 .venv/bin/bdt
      // 会 import 不到 babeldoc_tools；显式把仓库根放进 PYTHONPATH（等价于那个 .pth 的内容）。
      env: {
        PATH: `${repoRoot}/.venv/bin:${process.env.PATH ?? ''}`,
        PYTHONPATH: repoRoot,
      },
    },
    {
      command: `pnpm dev --host 127.0.0.1 --port ${devPort} --strictPort`,
      cwd: webRoot,
      url: `http://127.0.0.1:${devPort}/`,
      reuseExistingServer: false,
      timeout: 60_000,
      env: { BDT_SERVE_PORT: String(servePort) },
    },
  ],
});
