/**
 * EngineQueue：串行队列、事件归属、取消、崩溃后继续。
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { EngineQueue } from '../src/main/engine';
import { Library } from '../src/main/library';
import type { DocEngineEvent } from '../src/shared/library';
import type { EngineEvent, EngineEventBody, Request } from '../src/shared/protocol';
import { configureRequest } from './support/requests';

let dir: string;
let library: Library;
let sent: Request[];
let pushed: DocEngineEvent[];
let running: boolean;
let queue: EngineQueue;

let seq = 0;
const ev = (body: EngineEventBody): EngineEvent => ({ seq: (seq += 1), ts: 1, ...body }) as EngineEvent;
const flush = (): Promise<void> => new Promise((resolve) => setImmediate(resolve));
const runs = (): string[] => sent.flatMap((r) => (r.type === 'run' ? [r.doc_id] : []));

function addDoc(name: string): string {
  const path = join(dir, name);
  writeFileSync(path, `%PDF-1.4\n${name}\n`);
  return library.addFile(path).doc.id;
}

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), 'syncpdf-queue-'));
  library = new Library(join(dir, 'root'));
  sent = [];
  pushed = [];
  running = false;
  queue = new EngineQueue({
    library,
    session: {
      send: (request) => sent.push(request),
      isRunning: () => running,
      start: async () => {
        running = true;
      },
    },
    configure: () => configureRequest(),
    onEvent: (event) => pushed.push(event),
    onDocChanged: () => undefined,
  });
});
afterEach(() => {
  library.close();
  rmSync(dir, { recursive: true, force: true });
});

describe('EngineQueue', () => {
  it('串行：configure 只发一次，上一篇 run_finished 后才发下一篇', async () => {
    const a = addDoc('a.pdf');
    const b = addDoc('b.pdf');
    queue.enqueue(a);
    queue.enqueue(b);
    queue.enqueue(a); // 重复入队忽略
    await flush();
    expect(sent.map((r) => r.type)).toEqual(['configure', 'run']);
    expect(runs()).toEqual([a]);
    expect(library.get(a)?.status).toBe('running');
    expect(library.get(b)?.status).toBe('queued');

    queue.handleEvent(ev({ type: 'run_finished', ok: true, elapsed_ms: 1 }));
    await flush();
    expect(runs()).toEqual([a, b]);
    expect(sent.filter((r) => r.type === 'configure')).toHaveLength(1);
    expect(library.get(a)?.status).toBe('done');
  });

  it('事件归属当前论文；空闲时的事件 docId 为 null 且不落库', async () => {
    const a = addDoc('a.pdf');
    queue.handleEvent(ev({ type: 'doc_meta', title: 'Nobody', authors: null }));
    queue.enqueue(a);
    await flush();
    queue.handleEvent(ev({ type: 'doc_meta', title: 'Paper A', authors: null }));
    expect(pushed.map((p) => p.docId)).toEqual([null, a]);
    expect(library.get(a)?.title).toBe('Paper A');
  });

  it('取消：排队中的直接出队；正在跑的发 cancel 并以 cancelled 收尾', async () => {
    const a = addDoc('a.pdf');
    const b = addDoc('b.pdf');
    queue.enqueue(a);
    queue.enqueue(b);
    await flush();
    queue.cancel(b);
    expect(library.get(b)?.status).toBe('cancelled');
    queue.cancel(a);
    expect(sent.at(-1)).toEqual({ type: 'cancel' });
    queue.handleEvent(ev({ type: 'run_finished', ok: false, elapsed_ms: 1 }));
    await flush();
    expect(library.get(a)?.status).toBe('cancelled');
    expect(runs()).toEqual([a]); // b 已出队，不会再跑
  });

  it('引擎崩溃：当前论文失败，重启后重新 configure 并继续下一篇', async () => {
    const a = addDoc('a.pdf');
    const b = addDoc('b.pdf');
    queue.enqueue(a);
    queue.enqueue(b);
    await flush();
    queue.handleSessionState('restarting');
    queue.handleEvent(ev({ type: 'error', fatal: true, code: 'sidecar_crashed', message: 'killed' }));
    await flush();
    expect(library.get(a)).toMatchObject({ status: 'failed', error: 'killed' });
    expect(runs()).toEqual([a, b]);
    expect(sent.filter((r) => r.type === 'configure')).toHaveLength(2);
  });

  it('重新入队清掉上次的错误', async () => {
    const a = addDoc('a.pdf');
    library.setStatus(a, 'failed', { error: 'old' });
    queue.enqueue(a);
    await flush();
    expect(library.get(a)?.error).toBeNull();
  });
});
