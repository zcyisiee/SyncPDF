/**
 * PdfDocumentLoader 缓存 / revision 失效单测（不依赖 pdf.js，依赖项注入）。
 * @vitest-environment node
 */
import { describe, expect, it, vi } from 'vitest';
import { PdfDocumentLoader } from '../../src/renderer/src/pdf/PdfDocumentLoader';
import { loadKey } from '../../src/renderer/src/pdf/usePdfDocument';

interface FakeDoc {
  id: number;
  loadingTask: { destroy: () => Promise<void> };
}

/** 造一个最小的 loader：记录 readBytes 次数、destroy 次数。 */
function makeLoader(options?: { failFirst?: boolean }) {
  let nextId = 0;
  const destroyed: number[] = [];
  const reads: string[] = [];
  let calls = 0;
  const loader = new PdfDocumentLoader({
    readBytes: async (path) => {
      reads.push(path);
      calls += 1;
      if (options?.failFirst === true && calls === 1) throw new Error('文件还没写完');
      return new ArrayBuffer(8);
    },
    open: async () => {
      const id = nextId;
      nextId += 1;
      const doc: FakeDoc = {
        id,
        loadingTask: {
          destroy: async () => {
            destroyed.push(id);
          },
        },
      };
      return doc as unknown as Awaited<ReturnType<PdfDocumentLoader['load']>>;
    },
  });
  return { loader, destroyed, reads };
}

describe('PdfDocumentLoader', () => {
  it('同一路径同一 revision：只读一次字节，复用同一个 Promise', async () => {
    const { loader, reads } = makeLoader();
    const first = loader.load('/tmp/a.pdf', 0);
    const second = loader.load('/tmp/a.pdf', 0);
    expect(first).toBe(second);
    await first;
    expect(reads).toEqual(['/tmp/a.pdf']);
    expect(loader.size).toBe(1);
    expect(loader.revisionOf('/tmp/a.pdf')).toBe(0);
  });

  it('revision 变高 → 重新加载，旧 proxy 在新的就位后才销毁', async () => {
    const { loader, destroyed, reads } = makeLoader();
    const first = (await loader.load('/tmp/a.pdf', 0)) as unknown as FakeDoc;
    expect(destroyed).toEqual([]);
    const second = (await loader.load('/tmp/a.pdf', 1)) as unknown as FakeDoc;
    expect(second.id).not.toBe(first.id);
    expect(reads).toHaveLength(2);
    // finally 里的销毁是微任务，等一轮
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(destroyed).toEqual([first.id]);
    expect(loader.revisionOf('/tmp/a.pdf')).toBe(1);
  });

  it('revision 变低 / 相同 → 不重载', async () => {
    const { loader, reads } = makeLoader();
    await loader.load('/tmp/a.pdf', 3);
    await loader.load('/tmp/a.pdf', 2);
    await loader.load('/tmp/a.pdf', 3);
    expect(reads).toHaveLength(1);
  });

  it('加载失败不留缓存，可重试', async () => {
    const { loader } = makeLoader({ failFirst: true });
    await expect(loader.load('/tmp/a.pdf', 0)).rejects.toThrow('文件还没写完');
    expect(loader.size).toBe(0);
    await expect(loader.load('/tmp/a.pdf', 0)).resolves.toBeDefined();
    expect(loader.size).toBe(1);
  });

  it('release / clear 销毁 proxy', async () => {
    const { loader, destroyed } = makeLoader();
    const a = (await loader.load('/tmp/a.pdf', 0)) as unknown as FakeDoc;
    const b = (await loader.load('/tmp/b.pdf', 0)) as unknown as FakeDoc;
    loader.release('/tmp/a.pdf');
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(destroyed).toEqual([a.id]);
    expect(loader.size).toBe(1);
    loader.clear();
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(destroyed).toEqual([a.id, b.id]);
    expect(loader.size).toBe(0);
  });

  it('不同路径互不影响', async () => {
    const { loader, reads } = makeLoader();
    await loader.load('/tmp/a.pdf', 0);
    await loader.load('/tmp/b.pdf', 0);
    expect(reads).toEqual(['/tmp/a.pdf', '/tmp/b.pdf']);
    expect(loader.size).toBe(2);
  });

  it('readBytes 异常不会泄漏未捕获 rejection', async () => {
    const spy = vi.fn();
    const loader = new PdfDocumentLoader({
      readBytes: async () => {
        throw new Error('forbidden');
      },
      open: async () => {
        spy();
        throw new Error('不应到达');
      },
    });
    await expect(loader.load('/etc/passwd', 0)).rejects.toThrow('forbidden');
    expect(spy).not.toHaveBeenCalled();
  });
});

describe('loadKey', () => {
  it('path 为空 → 空键；否则 path + revision', () => {
    expect(loadKey(null, 3)).toBe('');
    expect(loadKey('', 3)).toBe('');
    expect(loadKey('/a.pdf', 0)).not.toBe(loadKey('/a.pdf', 1));
    expect(loadKey('/a.pdf', 2)).toBe(loadKey('/a.pdf', 2));
  });
});
