import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

// 开发时 /api 代理到本机 `bdt cloud serve`（默认 8790）；生产由 nginx 托管 dist 并反代 /api。
const port = process.env.BDT_CLOUD_PORT ?? '8790';

export default defineConfig({
  plugins: [react()],
  server: { proxy: { '/api': `http://127.0.0.1:${port}` } },
  preview: { proxy: { '/api': `http://127.0.0.1:${port}` } },
});
