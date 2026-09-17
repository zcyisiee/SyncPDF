import { describe, expect, it } from 'vitest';

import {
  MAX_UPLOAD_BYTES,
  checkUpload,
  formatBytes,
  hasPdfMagic,
  readPdfMagic,
} from '../src/lib/uploads';
import { makePdfFile, makeTextFile } from './helpers';

describe('上传预检（lib/uploads.ts）', () => {
  it('上限与服务端一致（serve/uploads.py::MAX_UPLOAD_BYTES）', () => {
    expect(MAX_UPLOAD_BYTES).toBe(200 * 1024 * 1024);
  });

  it('魔数只看前 5 字节，不看扩展名', () => {
    const pdf = new TextEncoder().encode('%PDF-1.7\n');
    expect(hasPdfMagic(pdf)).toBe(true);
    expect(hasPdfMagic(new TextEncoder().encode('<html>'))).toBe(false);
    // 太短的输入不算 PDF（不能靠"前几字节碰巧相同"蒙过去）
    expect(hasPdfMagic(new TextEncoder().encode('%PDF'))).toBe(false);
    expect(hasPdfMagic(new Uint8Array())).toBe(false);
  });

  it('readPdfMagic 走 file.slice（只读前 5 字节，不整文件进内存）', async () => {
    expect(await readPdfMagic(makePdfFile())).toBe(true);
    expect(await readPdfMagic(makeTextFile())).toBe(false);
  });

  it('大小超限：拒收并给出人话（413 口径）', () => {
    const result = checkUpload({ name: 'big.pdf', size: MAX_UPLOAD_BYTES + 1 });
    expect(result.ok).toBe(false);
    expect(result.rejection).toBe('too_large');
    expect(result.message).toContain('200 MB');
    expect(checkUpload({ name: 'exact.pdf', size: MAX_UPLOAD_BYTES }).ok).toBe(true);
  });

  it('非 .pdf 扩展名：前端先挡一次（服务端还会校验魔数）', () => {
    const result = checkUpload({ name: 'notes.txt', size: 1024 });
    expect(result.ok).toBe(false);
    expect(result.rejection).toBe('not_pdf');
    expect(result.message).toContain('只接受 .pdf');
  });

  it('合法文件通过（大小写扩展名都算）', () => {
    expect(checkUpload({ name: 'paper.PDF', size: 1234 }).ok).toBe(true);
    expect(checkUpload({ name: '论文.pdf', size: 0 }).ok).toBe(true);
  });

  it('formatBytes：只保留一位小数', () => {
    expect(formatBytes(512)).toBe('512 B');
    expect(formatBytes(2048)).toBe('2 KB');
    expect(formatBytes(200 * 1024 * 1024)).toBe('200 MB');
    expect(formatBytes(1536 * 1024)).toBe('1.5 MB');
  });
});
