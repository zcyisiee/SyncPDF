/**
 * syncpdf-file:// 协议白名单逻辑测试（isPathAllowed / urlToPath / pathToUrl 纯函数）。
 */
import { describe, expect, it } from 'vitest';
import {
  AllowedRoots,
  isPathAllowed,
  pathToUrl,
  urlToPath,
} from '../src/main/protocol-handler';

describe('AllowedRoots / isPathAllowed', () => {
  it('白名单内放行，前缀目录不误伤', () => {
    const roots = new AllowedRoots();
    roots.add('/Users/a/docs');
    expect(isPathAllowed('/Users/a/docs/paper.pdf', roots)).toBe(true);
    expect(isPathAllowed('/Users/a/docs/sub/paper.pdf', roots)).toBe(true);
    // 同前缀不同目录不放行
    expect(isPathAllowed('/Users/a/docs-secret/paper.pdf', roots)).toBe(false);
    expect(isPathAllowed('/Users/a/other/paper.pdf', roots)).toBe(false);
    expect(isPathAllowed('relative/paper.pdf', roots)).toBe(false);
  });

  it('.. 折叠后逃逸拒绝', () => {
    const roots = new AllowedRoots();
    roots.add('/Users/a/docs');
    expect(isPathAllowed('/Users/a/docs/../secret.pdf', roots)).toBe(false);
  });

  it('根目录自身放行', () => {
    const roots = new AllowedRoots();
    roots.add('/Users/a/docs');
    expect(isPathAllowed('/Users/a/docs', roots)).toBe(true);
  });
});

describe('urlToPath / pathToUrl', () => {
  it('往返转换（含空格转义）', () => {
    const path = '/Users/a/My Docs/paper 2026.pdf';
    const url = pathToUrl(path);
    expect(url.startsWith('syncpdf-file://')).toBe(true);
    expect(urlToPath(url)).toBe(path);
  });

  it('非 syncpdf-file 协议抛错', () => {
    expect(() => urlToPath('file:///etc/passwd')).toThrow();
  });
});
