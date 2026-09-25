/**
 * 回归：package 为 type:module 时 electron-vite 默认产出 ESM preload，
 * 而 sandbox 渲染进程只能加载 CJS preload——加载失败是静默的（window.syncpdf 缺失、白屏）。
 * 守住：preload 产物为 CJS，且主进程引用的就是这个文件名。
 */
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import config from '../electron.vite.config';

describe('preload 产物', () => {
  it('输出 CJS，主进程按同名加载', () => {
    const output = (config as { preload: { build: { rollupOptions: { output: { format: string; entryFileNames: string } } } } })
      .preload.build.rollupOptions.output;
    expect(output.format).toBe('cjs');
    const main = readFileSync(resolve(__dirname, '../src/main/index.ts'), 'utf8');
    expect(main).toMatch(/sandbox: true/);
    expect(main).toContain(`'../preload/${output.entryFileNames.replace('[name]', 'index')}'`);
  });
});
