/**
 * 凭据存储（§11）：`userData/credentials.json`，mode 0o600，仅主进程读写。
 * 引擎不读此文件——api_key 只通过 stdin `configure` 传给引擎。
 */
import { app } from 'electron';
import { chmod, readFile, rename, writeFile } from 'node:fs/promises';
import { dirname, join } from 'node:path';
import { mkdir } from 'node:fs/promises';

export interface Credentials {
  provider: string;
  base_url: string;
  model: string;
  api_key: string;
}

/** credentials.json 的落盘路径（导出便于测试注入目录）。 */
export function credentialsPath(userDataDir: string): string {
  return join(userDataDir, 'credentials.json');
}

/** 读取凭据；文件不存在 → null；损坏 → 抛错（调用方决定是否重置）。 */
export async function readCredentialsFile(path: string): Promise<Credentials | null> {
  let text: string;
  try {
    text = await readFile(path, 'utf8');
  } catch (error) {
    if ((error as NodeJS.ErrnoException).code === 'ENOENT') return null;
    throw error;
  }
  const parsed: unknown = JSON.parse(text);
  if (
    typeof parsed !== 'object' ||
    parsed === null ||
    typeof (parsed as Record<string, unknown>).provider !== 'string' ||
    typeof (parsed as Record<string, unknown>).base_url !== 'string' ||
    typeof (parsed as Record<string, unknown>).model !== 'string' ||
    typeof (parsed as Record<string, unknown>).api_key !== 'string'
  ) {
    throw new Error('credentials.json 结构非法');
  }
  return parsed as Credentials;
}

/**
 * 原子写入凭据：先写临时文件（0600）再 rename，保证任意时刻磁盘上不出现宽权限文件。
 * 平台权限收窄只能由创建时的 mode 决定，因此临时文件创建后立即 chmod 双保险。
 */
export async function writeCredentialsFile(path: string, credentials: Credentials): Promise<void> {
  const tmp = `${path}.tmp-${process.pid}-${Date.now()}`;
  await mkdir(dirname(path), { recursive: true });
  await writeFile(tmp, `${JSON.stringify(credentials, null, 2)}\n`, { mode: 0o600 });
  await chmod(tmp, 0o600);
  await rename(tmp, path);
}

/** Electron 主进程使用的门面。 */
export async function readCredentials(): Promise<Credentials | null> {
  return readCredentialsFile(credentialsPath(app.getPath('userData')));
}

export async function writeCredentials(credentials: Credentials): Promise<void> {
  await writeCredentialsFile(credentialsPath(app.getPath('userData')), credentials);
}
