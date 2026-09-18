/**
 * pdf.js 运行时配置的唯一入口。worker / cmap / 标准字体全部走本地 `public/pdfjs/`
 * （由 `scripts/sync-pdfjs-assets.mjs` 从 `node_modules/pdfjs-dist` 复制），**不使用 CDN**：
 * 本模块被 `PdfCanvas` 动态 import，所以 pdf.js 只在实际需要预览时才加载。
 */
import { GlobalWorkerOptions, getDocument } from 'pdfjs-dist';
import type { PDFDocumentLoadingTask } from 'pdfjs-dist';

const ASSET_BASE = `${import.meta.env.BASE_URL}pdfjs/`;

/** `pdf.worker.min.mjs`：pdf.js 用 `new Worker(src, {type: 'module'})` 加载（同源，无 CDN）。 */
GlobalWorkerOptions.workerSrc = `${ASSET_BASE}pdf.worker.min.mjs`;

/** `cmaps/`：CID 字体映射（学术 PDF 的 CJK 常见），缺失会导致中文乱码/空白。 */
export const PDF_CMAP_URL = `${ASSET_BASE}cmaps/`;

/** `standard_fonts/`：14 个标准字体（Helvetica/Times 等未嵌入字体的回退）。 */
export const PDF_STANDARD_FONT_DATA_URL = `${ASSET_BASE}standard_fonts/`;

/**
 * 打开产物 PDF。`url` 是服务端 `artifacts/{name}`（`Accept-Ranges: bytes`），
 * pdf.js 自己发 Range 请求按需取字节——**不要**先 fetch 成 blob 再喂进来，
 * 否则 84MB 级产物会整文件下载。
 */
export function loadPdfDocument(url: string): PDFDocumentLoadingTask {
  return getDocument({
    // 必须是绝对 URL：pdf.js 用 `/^https?:/i.test(url)` 判断 isHttp，相对路径会被判成非 HTTP，
    // 于是既不发 Range 头也不启用分块（84MB 级产物会整文件下载）。
    url: new URL(url, window.location.href).href,
    cMapUrl: PDF_CMAP_URL,
    cMapPacked: true,
    standardFontDataUrl: PDF_STANDARD_FONT_DATA_URL,
  });
}
