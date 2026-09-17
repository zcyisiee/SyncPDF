#!/usr/bin/env node
/**
 * 把 pdf.js 的运行时静态资源从 `node_modules/pdfjs-dist` 复制进 `public/pdfjs/`
 * （worker + cmaps + standard_fonts）。
 *
 * 为什么必须复制：pdf.js 默认去 CDN 取 worker / cmap / 标准字体；本项目**禁止任何运行时
 * 外网依赖**，所以这些文件必须随前端一起本地打包。`public/pdfjs/` 不入库
 * （见 web/.gitignore），由 `pnpm dev` / `pnpm build` / `pnpm e2e` 前自动同步，
 * 升级 pdfjs-dist 后靠版本戳强制刷新。
 */
import { cpSync, existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const webRoot = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const packageRoot = join(webRoot, 'node_modules', 'pdfjs-dist');
const targetRoot = join(webRoot, 'public', 'pdfjs');
const stampFile = join(targetRoot, '.pdfjs-dist-version');

if (!existsSync(packageRoot)) {
  console.error('同步 pdf.js 静态资源失败：找不到 node_modules/pdfjs-dist，请先 `pnpm install`。');
  process.exit(1);
}

const version = JSON.parse(readFileSync(join(packageRoot, 'package.json'), 'utf8')).version;
const stamp = existsSync(stampFile) ? readFileSync(stampFile, 'utf8').trim() : null;
const required = [
  'pdf.worker.min.mjs',
  'cmaps/UniGB-UCS2-H.bcmap',
  'standard_fonts/FoxitSerif.pfb',
];
if (stamp === version && required.every((name) => existsSync(join(targetRoot, name)))) {
  process.exit(0);
}

rmSync(targetRoot, { recursive: true, force: true });
mkdirSync(targetRoot, { recursive: true });
cpSync(join(packageRoot, 'cmaps'), join(targetRoot, 'cmaps'), { recursive: true });
cpSync(join(packageRoot, 'standard_fonts'), join(targetRoot, 'standard_fonts'), {
  recursive: true,
});
cpSync(join(packageRoot, 'build', 'pdf.worker.min.mjs'), join(targetRoot, 'pdf.worker.min.mjs'));
cpSync(join(packageRoot, 'LICENSE'), join(targetRoot, 'LICENSE'));
writeFileSync(stampFile, `${version}\n`);
console.log(`已同步 pdf.js ${version} 静态资源 → public/pdfjs/（worker + cmaps + standard_fonts）`);
