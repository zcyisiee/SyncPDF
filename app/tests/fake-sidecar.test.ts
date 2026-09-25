/**
 * fake-sidecar 行为：与 `syncpdf-cli run` 长驻会话同语义（串行多 run、cancel、EOF 收尾）。
 */
import { describe, expect, it } from 'vitest';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { isEngineEvent, type EngineEvent, type Request } from '../src/shared/protocol';
import { FAKE_SIDECAR, configureRequest, runRequest } from './support/requests';

interface Session {
  events: EngineEvent[];
  code: number | null;
}

/**
 * 起一个 fake-sidecar，写入 requests；`onEvent` 可以追加写入或返回 true 关 stdin。
 * 不传 onEvent 时写完立即关 stdin。
 */
function session(
  requests: Request[],
  onEvent?: (event: EngineEvent, write: (r: Request) => void) => boolean | void,
  tickMs = 1,
): Promise<Session> {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [FAKE_SIDECAR], {
      env: { ...process.env, FAKE_SIDECAR_TICK_MS: String(tickMs) },
    });
    const events: EngineEvent[] = [];
    const write = (request: Request): void => {
      child.stdin.write(`${JSON.stringify(request)}\n`);
    };
    createInterface({ input: child.stdout }).on('line', (line) => {
      const event = JSON.parse(line) as EngineEvent;
      events.push(event);
      if (onEvent?.(event, write) === true) child.stdin.end();
    });
    child.on('error', reject);
    child.on('exit', (code) => resolve({ events, code }));
    requests.forEach(write);
    if (onEvent === undefined) child.stdin.end();
  });
}

const types = (events: EngineEvent[]): string[] => events.map((event) => event.type);

describe('fake-sidecar', () => {
  it('只 configure：静默，EOF 退出码 0', async () => {
    const result = await session([configureRequest()]);
    expect(result.code).toBe(0);
    expect(result.events).toEqual([]);
  });

  it('完整 run：事件全过守卫、seq 从 1 连续、含 layout/doc_meta/每页 page_ready', async () => {
    const result = await session([configureRequest(), runRequest('d')], (event) => event.type === 'run_finished');
    expect(result.code).toBe(0);
    result.events.forEach((event, index) => {
      expect(isEngineEvent(event), JSON.stringify(event).slice(0, 120)).toBe(true);
      expect(event.seq).toBe(index + 1);
    });
    const all = types(result.events);
    expect(all.filter((t) => t === 'layout')).toHaveLength(12);
    expect(all).toContain('doc_meta');
    const ready = result.events.filter((event) => event.type === 'page_ready');
    expect(ready.map((event) => event.page)).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]);
    expect(all.slice(-2)).toEqual(['document_finished', 'run_finished']);
    expect(result.events.at(-1)).toMatchObject({ ok: true });
  });

  it('两个 run 串行：第二个 run_started 在第一个 run_finished 之后', async () => {
    let finished = 0;
    const result = await session([configureRequest(), runRequest('a'), runRequest('b')], (event) => {
      if (event.type === 'run_finished') finished += 1;
      return finished === 2;
    });
    const starts = result.events.filter((event) => event.type === 'run_started');
    expect(starts.map((event) => event.doc_id)).toEqual(['a', 'b']);
    const firstFinish = result.events.findIndex((event) => event.type === 'run_finished');
    expect(result.events.indexOf(starts[1])).toBeGreaterThan(firstFinish);
  });

  it('cancel：当前 run 以 ok:false 收尾，无 document_finished，会话继续', async () => {
    let cancelled = false;
    const result = await session([configureRequest(), runRequest('a')], (event, write) => {
      if (!cancelled && event.type === 'paragraph' && event.paragraph_id === 'P01-002') {
        cancelled = true;
        write({ type: 'cancel' });
      }
      if (event.type === 'run_finished' && !event.ok) write(runRequest('b'));
      return event.type === 'run_finished' && event.ok;
    }, 20);
    const finishes = result.events.filter((event) => event.type === 'run_finished');
    expect(finishes.map((event) => event.ok)).toEqual([false, true]);
    expect(types(result.events).filter((t) => t === 'document_finished')).toHaveLength(1);
  });

  it('未 configure 就 run：致命错误 + ok:false', async () => {
    const result = await session([runRequest('a')]);
    expect(types(result.events)).toEqual(['run_started', 'error', 'run_finished']);
    expect(result.events.at(-1)).toMatchObject({ ok: false });
  });

  it('编辑请求静默接受；尚未实现的请求回 unsupported_request，不影响会话', async () => {
    const result = await session([
      { type: 'retranslate', doc_id: 'd', store: null, paragraph_ids: ['P01-001'] },
      { type: 'apply_edit', doc_id: 'd', store: null, paragraph_id: 'P01-001', translated_html: null, style: {} },
      { type: 'export', doc_id: 'd', output: '/o.pdf', mode: 'full' },
    ]);
    expect(result.code).toBe(0);
    expect(result.events).toMatchObject([{ type: 'error', fatal: false, code: 'unsupported_request' }]);
  });
});
