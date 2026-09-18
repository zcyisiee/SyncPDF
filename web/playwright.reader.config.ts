import { defineConfig, devices } from '@playwright/test';

// Isolated reader acceptance uses only route stubs and generated PDFs. No backend
// process, credentials, or existing tmp document is opened.
export default defineConfig({
  testDir: './e2e',
  testMatch: 'reader.spec.ts',
  workers: 1,
  reporter: 'list',
  outputDir: process.env.READER_TEST_OUTPUT ?? `../tmp/reader-${Date.now()}`,
  use: { baseURL: 'http://127.0.0.1:5178', ...devices['Desktop Chrome'], trace: 'retain-on-failure' },
  webServer: {
    command: 'pnpm dev --host 127.0.0.1 --port 5178 --strictPort',
    url: 'http://127.0.0.1:5178',
    reuseExistingServer: false,
  },
});
