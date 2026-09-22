import { defineConfig } from 'electron-vite';
import react from '@vitejs/plugin-react';
import { cp, mkdir } from 'node:fs/promises';
import { createReadStream, existsSync } from 'node:fs';
import { join, resolve } from 'node:path';
import type { Plugin } from 'vite';

/** pdf.js 的运行时静态资源（cmaps / 标准字体 / wasm / icc）。 */
const PDFJS_ASSET_DIRS = ['cmaps', 'standard_fonts', 'wasm', 'iccs'] as const;

const PDFJS_ROOT = resolve(__dirname, 'node_modules/pdfjs-dist');

const MIME: Record<string, string> = {
  '.bcmap': 'application/octet-stream',
  '.pfb': 'application/octet-stream',
  '.ttf': 'font/ttf',
  '.wasm': 'application/wasm',
  '.icc': 'application/octet-stream',
};

/**
 * 把 pdfjs-dist 的运行时资源暴露在 `<renderer>/pdfjs/` 下：
 * - dev：中间件直接从 node_modules 读（不复制、改版本即时生效）；
 * - build：closeBundle 时复制到 `out/renderer/pdfjs/`。
 * 渲染进程用 `new URL('pdfjs/', document.baseURI)` 拼路径（见 src/renderer/src/pdf/pdfjs.ts）。
 */
function pdfjsAssets(): Plugin {
  return {
    name: 'syncpdf:pdfjs-assets',
    configureServer(server) {
      server.middlewares.use((request, response, next) => {
        const url = request.url ?? '';
        const match = /^\/pdfjs\/([^?#]+)/.exec(url);
        if (match === null) {
          next();
          return;
        }
        const relative = decodeURIComponent(match[1]);
        // 只允许白名单子目录，且不得含 `..`
        if (relative.includes('..') || !PDFJS_ASSET_DIRS.some((dir) => relative.startsWith(`${dir}/`))) {
          response.statusCode = 403;
          response.end('forbidden');
          return;
        }
        const file = join(PDFJS_ROOT, relative);
        if (!existsSync(file)) {
          response.statusCode = 404;
          response.end('not found');
          return;
        }
        const dot = file.lastIndexOf('.');
        response.setHeader(
          'content-type',
          MIME[dot >= 0 ? file.slice(dot) : ''] ?? 'application/octet-stream',
        );
        createReadStream(file).pipe(response);
      });
    },
    async closeBundle() {
      const target = resolve(__dirname, 'out/renderer/pdfjs');
      await mkdir(target, { recursive: true });
      for (const dir of PDFJS_ASSET_DIRS) {
        const from = join(PDFJS_ROOT, dir);
        if (!existsSync(from)) continue;
        await cp(from, join(target, dir), { recursive: true });
      }
    },
  };
}

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
    plugins: [react(), pdfjsAssets()],
    // pdf.js 的 worker 经 `?url` 引入，构建后是 out/renderer/assets 下的同源文件
    worker: { format: 'es' },
  },
});
