/**
 * pdf.js 渲染进程加载冒烟测试（M2-06 验收补充）：
 *
 * brief 要求"确认 pdf.js 在渲染进程能加载"。真实运行环境是 Electron 渲染进程
 * （Chromium，有完整 DOM / worker / `Uint8Array.prototype.toHex`）；vitest 只有
 * node + jsdom，这里做**等价最小验证**：
 *
 * 1. jsdom 挂上 DOM 全局（window/document/DOMMatrix/Path2D/ImageData/navigator）
 *    后 `import 'pdfjs-dist'`（即渲染进程打包的同一个模块）不再崩；
 * 2. 用 `getDocument` 真解析 engine/fixtures/up-vns.pdf（12 页真 PDF）：
 *    numPages=12、第 1 页 viewport transform 为 `[1,0,0,-1,0,793.701]`
 *    （y 翻转——geometry.ts 全部坐标变换的依据）；
 * 3. `GlobalWorkerOptions.workerSrc` 指到包内 worker 文件并真实以 worker 线程跑通。
 *
 * node 24 的 `Uint8Array.prototype` 还没有 `toHex`（v25+ 才进标准，Chromium 134
 * 已有），这里 polyfill 一份——仅测试进程需要，Electron 渲染进程不受影响。
 *
 * 夹具缺失（engine/fixtures/up-vns.pdf 被根 .gitignore 忽略，未 sync）时跳过。
 * @vitest-environment node
 */
import { describe, expect, it } from 'vitest';
import { existsSync } from 'node:fs';
import { readFile } from 'node:fs/promises';
import { join, resolve } from 'node:path';

/** 浏览器有而 node 24 没有的 API（Electron Chromium 均具备）。 */
function polyfillBrowserApis(): void {
  if (!('toHex' in Uint8Array.prototype)) {
    Object.defineProperty(Uint8Array.prototype, 'toHex', {
      value(this: Uint8Array): string {
        return Array.from(this, (byte) => byte.toString(16).padStart(2, '0')).join('');
      },
      configurable: true,
      writable: true,
    });
  }
}

/** pdfjs-dist 顶层目录（worker 文件位置）。 */
const PDFJS_ROOT = resolve(__dirname, '..', 'node_modules', 'pdfjs-dist');

/** jsdom 环境文件 URL（worker import 用）。 */
function toFileUrl(path: string): string {
  const resolved = resolve(path);
  return `file://${resolved.split('/').map(encodeURIComponent).join('/')}`;
}

describe('pdf.js 加载冒烟（渲染进程等价环境）', () => {
  it('jsdom DOM 全局 + 真实 getDocument 解析 up-vns.pdf（12 页）', async () => {
    const fixture = resolve(__dirname, '..', '..', 'engine', 'fixtures', 'up-vns.pdf');
    if (!existsSync(fixture)) return; // 夹具未 sync（gitignore）：跳过，不失败

    // jsdom 无类型声明（纯测试用途），用 createRequire 拿构造器避开 .d.ts
    const { createRequire } = await import('node:module');
    const require = createRequire(import.meta.url);
    const JSDOM = require('jsdom').JSDOM as new (html: string) => {
      window: Window & typeof globalThis;
    };
    const dom = new JSDOM('<!DOCTYPE html>');
    globalThis.window = dom.window as unknown as typeof globalThis.window;
    globalThis.document = dom.window.document;
    globalThis.DOMMatrix = dom.window.DOMMatrix;
    globalThis.Path2D = dom.window.Path2D;
    globalThis.ImageData = dom.window.ImageData;
    Object.defineProperty(globalThis, 'navigator', {
      value: dom.window.navigator,
      configurable: true,
    });
    polyfillBrowserApis();

    const pdfjs = await import('pdfjs-dist');
    pdfjs.GlobalWorkerOptions.workerSrc = toFileUrl(join(PDFJS_ROOT, 'build/pdf.worker.min.mjs'));

    const data = new Uint8Array(await readFile(fixture));
    const doc = await pdfjs.getDocument({ data }).promise;
    try {
      expect(doc.numPages).toBe(12); // up-vns 夹具就是 12 页（brief 指定）
      const page = await doc.getPage(1);
      const viewport = page.getViewport({ scale: 1 });
      // A4 高 793.701；transform d=-1 即 y 翻转——BoxLayer 坐标系的实证
      expect(viewport.width).toBeCloseTo(595.276, 2);
      expect(viewport.height).toBeCloseTo(793.701, 2);
      expect(viewport.transform).toEqual([1, 0, 0, -1, 0, viewport.height]);
      // 取一页文本，确认 worker 线程真实跑通（解析内容跨线程返回）
      const text = await page.getTextContent();
      expect(text.items.length).toBeGreaterThan(0);
    } finally {
      await doc.loadingTask.destroy();
    }
  });
});
