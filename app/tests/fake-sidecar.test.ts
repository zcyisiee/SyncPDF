/**
 * fake-sidecar 自身行为测试（协议正确性：JSONL / seq 单调 / cancel）。
 */
import { describe, expect, it } from 'vitest';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { join } from 'node:path';
import { once } from 'node:events';
import { isEngineEvent } from '../src/shared/protocol';

const FAKE_SIDECAR = join(__dirname, '..', 'scripts', 'fake-sidecar.mjs');

interface RunResult {
  events: unknown[];
  code: number | null;
}

/** 跑完整个 fake-sidecar 会话：requests 全部写入后关 stdin 等退出。 */
function runFakeSidecar(requests: string[], closeAfterMs?: number): Promise<RunResult> {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [FAKE_SIDECAR]);
    const events: unknown[] = [];
    const rl = createInterface({ input: child.stdout });
    rl.on('line', (line) => {
      if (line.trim() === '') return;
      events.push(JSON.parse(line));
    });
    child.on('error', reject);
    child.on('exit', (code) => resolve({ events, code }));
    for (const request of requests) {
      child.stdin.write(`${request}\n`);
    }
    if (closeAfterMs !== undefined) {
      setTimeout(() => child.stdin.end(), closeAfterMs);
    } else {
      child.stdin.end();
    }
  });
}

describe('fake-sidecar', () => {
  it('只发 configure 不 run：EOF 退出，1 个确认事件', async () => {
    const result = await runFakeSidecar([
      JSON.stringify({
        type: 'configure',
        provider: 'openai_compatible',
        base_url: 'x',
        model: 'm',
        api_key: 'k',
        concurrency: 1,
        cache_dir: '/tmp',
      }),
    ]);
    expect(result.code).toBe(0);
    // configure 确认：一条 preflight progress
    expect(result.events).toHaveLength(1);
    expect((result.events[0] as { type: string }).type).toBe('progress');
  });

  it('完整事件流通过 isEngineEvent 且 seq 从 1 单调', async () => {
    const configure = {
      type: 'configure',
      provider: 'openai_compatible',
      base_url: 'x',
      model: 'm',
      api_key: 'k',
      concurrency: 1,
      cache_dir: '/tmp',
    };
    const run = {
      type: 'run',
      doc_id: 'd',
      input: '/tmp/in.pdf',
      output: '/tmp/out.pdf',
      source_lang: 'en',
      target_lang: 'zh',
      mode: 'full',
    };
    // stdin 保持打开直到跑完（sidecar 长驻：stdin 开着不会自然退出），
    // 跑完后发 cancel 收尾退出
    const result = await new Promise<RunResult>((resolve, reject) => {
      const child = spawn(process.execPath, [FAKE_SIDECAR]);
      const events: unknown[] = [];
      const rl = createInterface({ input: child.stdout });
      rl.on('line', (line) => {
        if (line.trim() === '') return;
        events.push(JSON.parse(line));
        // 收到 run_finished 即发 cancel 让子进程退出
        if ((JSON.parse(line) as { type: string }).type === 'run_finished') {
          child.stdin.write(`${JSON.stringify({ type: 'cancel' })}\n`);
        }
      });
      child.on('error', reject);
      child.on('exit', (code) => resolve({ events, code }));
      child.stdin.write(`${JSON.stringify(configure)}\n`);
      child.stdin.write(`${JSON.stringify(run)}\n`);
    });
    expect(result.code).toBe(0);
    expect(result.events.length).toBeGreaterThanOrEqual(30);
    let lastSeq = 0;
    for (const event of result.events) {
      expect(isEngineEvent(event), JSON.stringify(event).slice(0, 80)).toBe(true);
      const seq = (event as { seq: number }).seq;
      expect(seq).toBe(lastSeq + 1);
      lastSeq = seq;
    }
    // 结尾两个事件
    const tail = result.events.slice(-2).map((event) => (event as { type: string }).type);
    expect(tail).toEqual(['document_finished', 'run_finished']);
  });

  it('cancel：正在 run 时发 cancel → 立即退出，不再发后续事件', async () => {
    const configure = JSON.stringify({
      type: 'configure',
      provider: 'openai_compatible',
      base_url: 'x',
      model: 'm',
      api_key: 'k',
      concurrency: 1,
      cache_dir: '/tmp',
    });
    const run = JSON.stringify({
      type: 'run',
      doc_id: 'd',
      input: '/tmp/in.pdf',
      output: '/tmp/out.pdf',
      source_lang: 'en',
      target_lang: 'zh',
      mode: 'full',
    });
    const result = await new Promise<RunResult>((resolve, reject) => {
      const child = spawn(process.execPath, [FAKE_SIDECAR]);
      const events: unknown[] = [];
      const rl = createInterface({ input: child.stdout });
      rl.on('line', (line) => {
        if (line.trim() === '') return;
        events.push(JSON.parse(line));
      });
      child.on('error', reject);
      child.on('exit', (code) => resolve({ events, code }));
      child.stdin.write(`${configure}\n`);
      child.stdin.write(`${run}\n`);
      // 收到若干事件后发 cancel
      const timer = setTimeout(() => {
        child.stdin.write(`${JSON.stringify({ type: 'cancel' })}\n`);
      }, 700);
      void once(child, 'exit').then(() => clearTimeout(timer));
    });
    expect(result.code).toBe(0);
    // cancel 后没有 run_finished（未跑完就退出了）
    const types = result.events.map((event) => (event as { type: string }).type);
    expect(types).not.toContain('run_finished');
    expect(types).not.toContain('document_finished');
  });
});
