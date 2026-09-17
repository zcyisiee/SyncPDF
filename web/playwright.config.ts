import { defineConfig, devices } from '@playwright/test';

// W04 只建骨架：命名用例从 W05（PDF 预览）起随对应 brief 补进 e2e/。
export default defineConfig({
  testDir: './e2e',
  fullyParallel: true,
  reporter: 'list',
  use: {
    baseURL: 'http://localhost:5173',
    trace: 'on-first-retry',
  },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
});
