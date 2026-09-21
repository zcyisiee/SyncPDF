import { defineConfig } from 'electron-vite';
import react from '@vitejs/plugin-react';
import { resolve } from 'node:path';

/**
 * electron-vite 三进程构建：
 * - main / preload：CJS 产物到 out/main、out/preload（Electron 直接加载）；
 * - renderer：Vite + React，`@` 别名指向 src/renderer/src。
 * 测试（vitest）不走本配置——见 vitest.config.ts。
 */
export default defineConfig({
  main: {
    build: {
      outDir: 'out/main',
      lib: {
        entry: resolve(__dirname, 'src/main/index.ts'),
      },
    },
    resolve: {
      alias: {
        '@shared': resolve(__dirname, 'src/shared'),
      },
    },
  },
  preload: {
    build: {
      outDir: 'out/preload',
      lib: {
        entry: resolve(__dirname, 'src/preload/index.ts'),
      },
    },
    resolve: {
      alias: {
        '@shared': resolve(__dirname, 'src/shared'),
      },
    },
  },
  renderer: {
    root: resolve(__dirname, 'src/renderer'),
    build: {
      outDir: 'out/renderer',
      rollupOptions: {
        input: resolve(__dirname, 'src/renderer/index.html'),
      },
    },
    resolve: {
      alias: {
        '@': resolve(__dirname, 'src/renderer/src'),
        '@shared': resolve(__dirname, 'src/shared'),
      },
    },
    plugins: [react()],
  },
});
