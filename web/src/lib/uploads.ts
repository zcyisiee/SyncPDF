/**
 * 上传的**客户端**预检（W08）：与服务端同一套边界，但**不是**安全边界。
 *
 * `babeldoc_tools/serve/uploads.py` 会独立复核魔数与大小（`%PDF-` 前 5 字节、200MB 上限），
 * 这里的检查只负责"选了文件立刻给反馈"：不合法就别白等一次往返。
 * 两处常量必须一致（改了服务端上限记得同步这里）。
 */

/** 与服务端 `serve/uploads.py::MAX_UPLOAD_BYTES` 一致。 */
export const MAX_UPLOAD_BYTES = 200 * 1024 * 1024;

/** PDF 魔数（读文件前 5 字节比对，服务端也读同一段）。 */
export const PDF_MAGIC = '%PDF-';

export type UploadRejection = 'too_large' | 'not_pdf';

/** 前端能给出的拒收原因（服务端还有更多：缺文件名、目录冲突等）。 */
export interface UploadCheck {
  ok: boolean;
  rejection?: UploadRejection;
  message?: string;
}

/** 魔数字节 → 是否 PDF（纯函数：只比较前 5 字节，不看扩展名）。 */
export function hasPdfMagic(head: Uint8Array): boolean {
  if (head.length < PDF_MAGIC.length) return false;
  return PDF_MAGIC.split('').every((char, index) => head[index] === char.charCodeAt(0));
}

/**
 * 读文件头判断魔数。`Blob.slice()` 只取 5 字节，不把整个文件读进内存
 * （服务端也是流式写盘，两边都不整文件驻留）。
 */
export async function readPdfMagic(file: Blob): Promise<boolean> {
  const head = new Uint8Array(await file.slice(0, PDF_MAGIC.length).arrayBuffer());
  return hasPdfMagic(head);
}

/** 大小 + 扩展名预检（魔数要读文件，留给上传前的那一次读）。 */
export function checkUpload(file: { name: string; size: number }): UploadCheck {
  if (file.size > MAX_UPLOAD_BYTES) {
    return {
      ok: false,
      rejection: 'too_large',
      message: `文件 ${formatBytes(file.size)} 超过上限 ${formatBytes(MAX_UPLOAD_BYTES)}（服务端会拒收 413 file_too_large）`,
    };
  }
  if (!file.name.toLowerCase().endsWith('.pdf')) {
    return {
      ok: false,
      rejection: 'not_pdf',
      message: '只接受 .pdf 文件（服务端还会校验 %PDF- 魔数）',
    };
  }
  return { ok: true };
}

/** 字节数 → 人话（上限提示用；只保留一位小数）。 */
export function formatBytes(bytes: number): string {
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB'];
  let value = bytes / 1024;
  let unit = units[0];
  for (let index = 1; index < units.length && value >= 1024; index += 1) {
    value /= 1024;
    unit = units[index];
  }
  return `${Math.round(value * 10) / 10} ${unit}`;
}
