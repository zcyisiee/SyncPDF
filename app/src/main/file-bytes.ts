/**
 * `readFileBytes`（M2-06）：渲染进程拿 PDF 字节的唯一通道。
 *
 * 渲染进程是 sandbox:true、零 Node 的，pdf.js 需要 `getDocument({ data })`，
 * 因此由主进程读字节再经 IPC 结构化克隆送过去。
 * 白名单与 `syncpdf-file://` 同一套（`AllowedRoots` + `isPathAllowed`）：
 * **白名单外的路径一律拒绝**，渲染进程无法借此读任意文件。
 */
import { readFile, stat } from 'node:fs/promises';
import { isPathAllowed, type AllowedRoots } from './protocol-handler';

/** 单文件字节上限（256 MiB）：避免一次 IPC 把主进程内存打爆。 */
export const MAX_FILE_BYTES = 256 * 1024 * 1024;

/** 拒绝原因的稳定错误码（渲染进程按前缀提示用户）。 */
export class FileAccessError extends Error {
  readonly code: 'forbidden' | 'not_found' | 'too_large' | 'invalid_path';

  constructor(code: FileAccessError['code'], message: string) {
    super(message);
    this.name = 'FileAccessError';
    this.code = code;
  }
}

/**
 * 读取白名单内文件的全部字节。
 * 非绝对路径 / 白名单外 / 非普通文件 / 超限都抛 `FileAccessError`。
 */
export async function readAllowedFileBytes(
  path: unknown,
  roots: AllowedRoots,
): Promise<Uint8Array> {
  if (typeof path !== 'string' || path === '') {
    throw new FileAccessError('invalid_path', '路径必须是非空字符串');
  }
  if (!isPathAllowed(path, roots)) {
    throw new FileAccessError('forbidden', `路径不在白名单内：${path}`);
  }
  let info: Awaited<ReturnType<typeof stat>>;
  try {
    info = await stat(path);
  } catch {
    throw new FileAccessError('not_found', `文件不存在：${path}`);
  }
  if (!info.isFile()) {
    throw new FileAccessError('not_found', `不是普通文件：${path}`);
  }
  if (info.size > MAX_FILE_BYTES) {
    throw new FileAccessError('too_large', `文件超过 ${MAX_FILE_BYTES} 字节：${path}`);
  }
  const buffer = await readFile(path);
  return new Uint8Array(buffer);
}
