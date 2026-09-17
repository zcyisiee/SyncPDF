import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';

// 开发代理：/api 转发到本机 bdt serve（默认 8787，可用 BDT_SERVE_PORT 覆盖）。
// serve 默认只监听 127.0.0.1，且不注册 CORS，所以前端必须经由同源代理访问（docs/frontend/api.md §1）。
const servePort = process.env.BDT_SERVE_PORT ?? '8787';

export default defineConfig({
  plugins: [react()],
  server: {
    proxy: {
      '/api': `http://127.0.0.1:${servePort}`,
    },
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
  test: {
    environment: 'jsdom',
    globals: true,
    setupFiles: ['./vitest.setup.ts'],
    include: ['tests/**/*.test.{ts,tsx}'],
    restoreMocks: true,
    clearMocks: true,
    unstubGlobals: true,
  },
});
