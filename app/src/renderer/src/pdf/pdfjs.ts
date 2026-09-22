/**
 * pdf.js 的唯一入口（bare API，禁用官方 viewer 组件 / react-pdf / iframe）。
 *
 * - worker：`pdf.worker.min.mjs` 经 Vite `?url` 拿到同源 URL 后交给
 *   `GlobalWorkerOptions.workerSrc`（index.html 的 CSP 放行 worker-src 'self' blob:）；
 * - cmaps / 标准字体 / wasm：由 electron.vite.config.ts 的 `pdfjsAssets()` 插件
 *   复制到 `<renderer>/pdfjs/` 并在 dev server 上提供，这里按 `document.baseURI` 拼相对 URL，
 *   dev（http://localhost:*）与打包后（file://…/out/renderer/index.html）都成立。
 */
import { getDocument, GlobalWorkerOptions } from 'pdfjs-dist';
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url';

GlobalWorkerOptions.workerSrc = workerUrl;

/** `<renderer>/pdfjs/` 的绝对 URL（末尾带 `/`）。 */
function assetBase(): string {
  try {
    return new URL('pdfjs/', document.baseURI).href;
  } catch {
    return 'pdfjs/';
  }
}

/** `getDocument` 的参数类型（pdf.js 未从入口导出 `DocumentInitParameters`）。 */
export type DocumentInitParameters = NonNullable<Parameters<typeof getDocument>[0]>;

/** 所有 `getDocument` 调用共用的参数（资源路径 + 关闭不需要的能力）。 */
export function documentInitParameters(data: ArrayBuffer): DocumentInitParameters {
  const base = assetBase();
  return {
    // pdf.js 会 transfer/detach 这个 buffer，调用方必须每次给新副本
    data: new Uint8Array(data),
    cMapUrl: `${base}cmaps/`,
    cMapPacked: true,
    standardFontDataUrl: `${base}standard_fonts/`,
    wasmUrl: `${base}wasm/`,
    iccUrl: `${base}iccs/`,
  };
}

export { getDocument, GlobalWorkerOptions, workerUrl };
export type {
  PDFDocumentProxy,
  PDFPageProxy,
  PageViewport,
  RenderTask,
} from 'pdfjs-dist';
