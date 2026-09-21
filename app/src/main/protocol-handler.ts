/**
 * `syncpdf-file://` 自定义协议 handler（§11 安全规则）：
 * 本地文件只经此协议访问，且仅允许白名单目录（用户打开的文件 + 应用数据目录）。
 * 渲染进程拿不到任意文件路径，未授权路径返回 403。
 */
import { net, protocol } from 'electron';
import { stat } from 'node:fs/promises';
import { isAbsolute, resolve, sep } from 'node:path';

export const FILE_PROTOCOL = 'syncpdf-file';

/** 白名单根目录集合（绝对路径，内部已 normalize）。 */
export class AllowedRoots {
  private readonly roots = new Set<string>();

  add(dir: string): void {
    const absolute = resolve(dir);
    this.roots.add(absolute);
  }

  has(dir: string): boolean {
    return this.roots.has(resolve(dir));
  }

  values(): string[] {
    return [...this.roots];
  }
}

/**
 * 路径是否在任一白名单根目录下。
 * `resolve` 折叠 `..` 段后再前缀比较（`root + sep` 防止 `/data` 匹配 `/database`）。
 */
export function isPathAllowed(path: string, roots: AllowedRoots): boolean {
  if (!isAbsolute(path)) return false;
  const resolved = resolve(path);
  return roots.values().some((root) => {
    if (resolved === root) return true;
    return resolved.startsWith(root + sep);
  });
}

/** `syncpdf-file:///abs/path/file.pdf` → 文件系统绝对路径。 */
export function urlToPath(url: string): string {
  const parsed = new URL(url);
  if (parsed.protocol !== `${FILE_PROTOCOL}:`) {
    throw new Error(`非 ${FILE_PROTOCOL}:// 协议：${parsed.protocol}`);
  }
  // URL host 为空时 pathname 就是绝对路径（file: 约定）；decode 处理空格等编码
  return decodeURIComponent(parsed.pathname);
}

/** 路径 → `syncpdf-file://` URL（渲染进程 img/pdf src 用）。 */
export function pathToUrl(path: string): string {
  const absolute = resolve(path);
  return `${FILE_PROTOCOL}://${absolute
    .split(sep)
    .map((segment) => encodeURIComponent(segment))
    .join('/')}`;
}

const MIME_BY_EXT: Record<string, string> = {
  '.pdf': 'application/pdf',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.svg': 'image/svg+xml',
  '.json': 'application/json',
};

function mimeFor(path: string): string {
  const dot = path.lastIndexOf('.');
  const ext = dot >= 0 ? path.slice(dot).toLowerCase() : '';
  return MIME_BY_EXT[ext] ?? 'application/octet-stream';
}

/**
 * 注册协议 handler。必须在 app ready 前调用 `registerSchemesAsPrivileged`
 * （见 index.ts），ready 后调用本函数。
 */
export function registerFileProtocol(roots: AllowedRoots): void {
  protocol.handle(FILE_PROTOCOL, (request) => {
    return (async () => {
      let path: string;
      try {
        path = urlToPath(request.url);
      } catch {
        return new Response('bad request', { status: 400 });
      }
      if (!isPathAllowed(path, roots)) {
        return new Response('forbidden', { status: 403 });
      }
      try {
        const info = await stat(path);
        if (!info.isFile()) return new Response('not a file', { status: 404 });
      } catch {
        return new Response('not found', { status: 404 });
      }
      // 经 net.fetch 走 file:// 读文件（sandbox 渲染进程无 Node fs）
      return net.fetch(`file://${path.split(sep).map(encodeURIComponent).join('/')}`, {
        headers: { 'content-type': mimeFor(path) },
      });
    })();
  });
}
