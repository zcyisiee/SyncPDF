/**
 * readFileBytes 白名单测试（M2-06 验收）：白名单外路径必须被拒。
 */
import { describe, expect, it } from 'vitest';
import { mkdtemp, rm, writeFile, mkdir } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { AllowedRoots } from '../src/main/protocol-handler';
import { FileAccessError, MAX_FILE_BYTES, readAllowedFileBytes } from '../src/main/file-bytes';

async function withFixture<T>(
  body: (paths: { allowed: string; denied: string; roots: AllowedRoots }) => Promise<T>,
): Promise<T> {
  const base = await mkdtemp(join(tmpdir(), 'syncpdf-bytes-'));
  try {
    const allowedDir = join(base, 'allowed');
    const deniedDir = join(base, 'denied');
    await mkdir(allowedDir);
    await mkdir(deniedDir);
    const allowed = join(allowedDir, 'doc.pdf');
    const denied = join(deniedDir, 'secret.pdf');
    await writeFile(allowed, '%PDF-1.7\n');
    await writeFile(denied, 'secret\n');
    const roots = new AllowedRoots();
    roots.add(allowedDir);
    return await body({ allowed, denied, roots });
  } finally {
    await rm(base, { recursive: true, force: true });
  }
}

describe('readAllowedFileBytes', () => {
  it('白名单内文件正常读出字节', async () => {
    await withFixture(async ({ allowed, roots }) => {
      const bytes = await readAllowedFileBytes(allowed, roots);
      expect(bytes).toBeInstanceOf(Uint8Array);
      expect(new TextDecoder().decode(bytes)).toBe('%PDF-1.7\n');
    });
  });

  it('白名单外路径被拒（code=forbidden）', async () => {
    await withFixture(async ({ denied, roots }) => {
      await expect(readAllowedFileBytes(denied, roots)).rejects.toMatchObject({
        name: 'FileAccessError',
        code: 'forbidden',
      });
    });
  });

  it('用 .. 逃逸出白名单同样被拒', async () => {
    await withFixture(async ({ allowed, denied, roots }) => {
      // 手工拼出带 `..` 的字面路径（join 会提前折叠，这里要的是未折叠形态）
      const escape = `${allowed}/../../denied/secret.pdf`;
      expect(escape).not.toBe(denied);
      expect(escape).toContain('..');
      await expect(readAllowedFileBytes(escape, roots)).rejects.toMatchObject({
        code: 'forbidden',
      });
    });
  });

  it('相对路径 / 空串 / 非字符串被拒', async () => {
    await withFixture(async ({ roots }) => {
      await expect(readAllowedFileBytes('relative/x.pdf', roots)).rejects.toMatchObject({
        code: 'forbidden',
      });
      await expect(readAllowedFileBytes('', roots)).rejects.toMatchObject({
        code: 'invalid_path',
      });
      await expect(readAllowedFileBytes(null, roots)).rejects.toMatchObject({
        code: 'invalid_path',
      });
      await expect(readAllowedFileBytes(42, roots)).rejects.toMatchObject({
        code: 'invalid_path',
      });
    });
  });

  it('白名单内但不存在 / 是目录 → not_found', async () => {
    await withFixture(async ({ allowed, roots }) => {
      const dir = join(allowed, '..');
      await expect(readAllowedFileBytes(join(dir, 'missing.pdf'), roots)).rejects.toMatchObject({
        code: 'not_found',
      });
      await expect(readAllowedFileBytes(dir, roots)).rejects.toMatchObject({
        code: 'not_found',
      });
    });
  });

  it('空白名单拒绝一切', async () => {
    await withFixture(async ({ allowed }) => {
      await expect(readAllowedFileBytes(allowed, new AllowedRoots())).rejects.toMatchObject({
        code: 'forbidden',
      });
    });
  });

  it('FileAccessError 是 Error 且带稳定 code；上限常量为 256 MiB', () => {
    const error = new FileAccessError('too_large', 'x');
    expect(error).toBeInstanceOf(Error);
    expect(error.code).toBe('too_large');
    expect(MAX_FILE_BYTES).toBe(256 * 1024 * 1024);
  });
});
