/**
 * credentials 写入测试：mode 0o600 验收。
 */
import { describe, expect, it } from 'vitest';
import { chmod, mkdtemp, readFile, rm, stat } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import {
  credentialsPath,
  readCredentialsFile,
  writeCredentialsFile,
} from '../src/main/credentials';

describe('credentials', () => {
  it('写入后 mode 为 0o600，且可读回', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'syncpdf-cred-'));
    try {
      const path = credentialsPath(dir);
      await writeCredentialsFile(path, {
        provider: 'openai_compatible',
        base_url: 'http://localhost:8000',
        model: 'gpt-test',
        api_key: 'sk-test',
      });
      const info = await stat(path);
      // POSIX 权限位（macOS / Linux）
      expect(info.mode & 0o777).toBe(0o600);

      const read = await readCredentialsFile(path);
      expect(read).toEqual({
        provider: 'openai_compatible',
        base_url: 'http://localhost:8000',
        model: 'gpt-test',
        api_key: 'sk-test',
      });
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  it('覆盖写入（已存在宽权限文件）后仍收紧到 0o600', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'syncpdf-cred-'));
    try {
      const path = credentialsPath(dir);
      // 预置一个 0644 的旧文件（模拟被外部改宽）
      const { writeFile } = await import('node:fs/promises');
      await writeFile(path, '{}\n', { mode: 0o644 });
      await chmod(path, 0o644);
      const before = await stat(path);
      expect(before.mode & 0o777).toBe(0o644);

      await writeCredentialsFile(path, {
        provider: 'anthropic',
        base_url: 'https://api.anthropic.com',
        model: 'claude-test',
        api_key: 'sk-ant',
      });
      const after = await stat(path);
      expect(after.mode & 0o777).toBe(0o600);
      const text = await readFile(path, 'utf8');
      expect(JSON.parse(text).api_key).toBe('sk-ant');
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  it('文件不存在 → read 返回 null；结构非法 → 抛错', async () => {
    const dir = await mkdtemp(join(tmpdir(), 'syncpdf-cred-'));
    try {
      const path = credentialsPath(dir);
      expect(await readCredentialsFile(path)).toBeNull();

      const { writeFile } = await import('node:fs/promises');
      await writeFile(path, '{"provider":1}\n');
      await expect(readCredentialsFile(path)).rejects.toThrow();
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });
});
