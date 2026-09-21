/**
 * SidecarManager 集成测试：用 fake-sidecar 真实 spawn 跑一遍（M2-03 验收）。
 * 断言：收到 ≥30 个事件、seq 单调递增、cancel 后进程退出。
 */
import { describe, expect, it } from 'vitest';
import { join } from 'node:path';
import { SidecarManager } from '../src/main/sidecar';
import type { EngineEvent } from '../src/shared/protocol';

const FAKE_SIDECAR = join(__dirname, '..', 'scripts', 'fake-sidecar.mjs');

function waitFor<T>(predicate: () => T | undefined, timeoutMs = 20_000): Promise<T> {
  return new Promise((resolve, reject) => {
    const started = Date.now();
    const tick = (): void => {
      const value = predicate();
      if (value !== undefined) {
        resolve(value);
        return;
      }
      if (Date.now() - started > timeoutMs) {
        reject(new Error('waitFor 超时'));
        return;
      }
      setTimeout(tick, 50);
    };
    tick();
  });
}

describe('SidecarManager × fake-sidecar', () => {
  it('完整跑一遍：configure → run → ≥30 事件，seq 单调', async () => {
    const events: EngineEvent[] = [];
    const manager = new SidecarManager({
      onEvent: (event) => events.push(event),
      onLog: () => undefined,
      onStateChange: () => undefined,
    });
    await manager.start({ cmd: process.execPath, args: [FAKE_SIDECAR] });
    expect(manager.getState()).toBe('running');

    manager.send({
      type: 'configure',
      provider: 'openai_compatible',
      base_url: 'http://localhost:1',
      model: 'test-model',
      api_key: 'test-key',
      concurrency: 2,
      cache_dir: '/tmp',
    });
    manager.send({
      type: 'run',
      doc_id: 'doc-test',
      input: '/tmp/input.pdf',
      output: '/tmp/output.pdf',
      source_lang: 'en',
      target_lang: 'zh',
      mode: 'full',
    });

    await waitFor(() => (events.some((event) => event.type === 'run_finished') ? true : undefined));

    expect(events.length).toBeGreaterThanOrEqual(30);
    // seq 单调递增（从 1 开始）
    for (let i = 1; i < events.length; i += 1) {
      expect(events[i].seq).toBeGreaterThan(events[i - 1].seq);
    }
    // 事件构成
    const types = new Set(events.map((event) => event.type));
    expect(types.has('run_started')).toBe(true);
    expect(types.has('paragraph')).toBe(true);
    expect(types.has('page_ready')).toBe(true);
    expect(types.has('document_finished')).toBe(true);
    expect(types.has('run_finished')).toBe(true);
    // 段落数：12 页 × 3 段 = 36
    const paragraphs = events.filter((event) => event.type === 'paragraph');
    expect(paragraphs.length).toBe(36);
    // 页就绪 12 页
    const pageReady = events.filter((event) => event.type === 'page_ready');
    expect(pageReady.length).toBe(13); // 12 页 + publishing 阶段 1 条 final

    // stop：stdin EOF → fake-sidecar 退出（长驻 sidecar 靠 EOF/cancel 收尾）
    await manager.stop();
    expect(manager.getState()).toBe('exited');
  });

  it('cancel：进程随 cancel 退出', async () => {
    const events: EngineEvent[] = [];
    const manager = new SidecarManager({
      onEvent: (event) => events.push(event),
      onLog: () => undefined,
      onStateChange: () => undefined,
    });
    await manager.start({ cmd: process.execPath, args: [FAKE_SIDECAR] });

    manager.send({
      type: 'configure',
      provider: 'anthropic',
      base_url: 'https://api.anthropic.com',
      model: 'test',
      api_key: 'k',
      concurrency: 1,
      cache_dir: '/tmp',
    });
    manager.send({
      type: 'run',
      doc_id: 'doc-cancel',
      input: '/tmp/in.pdf',
      output: '/tmp/out.pdf',
      source_lang: 'en',
      target_lang: 'zh',
      mode: 'full',
    });

    // 等到第一批事件（确认 run 已开始）再 cancel
    await waitFor(() => (events.length >= 5 ? true : undefined));

    await manager.stop();
    expect(manager.getState()).toBe('exited');
    // stop 走 stdin EOF；fake-sidecar 语义：EOF 即取消退出
    // 进程不应还在跑：再次 stop 无害
    await manager.stop();
  });

  it('崩溃重启：进程被 kill 后重启一次并重发 configure', async () => {
    const events: EngineEvent[] = [];
    const states: string[] = [];
    const manager = new SidecarManager({
      onEvent: (event) => events.push(event),
      onLog: () => undefined,
      onStateChange: (state) => states.push(state),
    });
    await manager.start({ cmd: process.execPath, args: [FAKE_SIDECAR] });
    manager.send({
      type: 'configure',
      provider: 'openai_compatible',
      base_url: 'http://localhost',
      model: 'm',
      api_key: 'k',
      concurrency: 1,
      cache_dir: '/tmp',
    });

    // 等到 configure 确认事件（说明 stdin 通了）
    await waitFor(() => (events.length >= 1 ? true : undefined));
    expect(events[0].type).toBe('progress');

    // kill 子进程（模拟崩溃）
    const child = (manager as unknown as { child: { kill: (signal: string) => void } }).child;
    expect(child).not.toBeNull();
    child.kill('SIGKILL');

    // 崩溃 → 合成 error{fatal} 事件（seq=MAX_SAFE_INTEGER）+ 重启
    // （configure 自动重发 → 重启后的 progress 事件 seq 重新从 1 计）
    await waitFor(() => {
      const fatal = events.filter((event) => event.type === 'error');
      const progressCount = events.filter((event) => event.type === 'progress').length;
      return fatal.length >= 1 && progressCount >= 2 ? true : undefined;
    });

    expect(events.some((event) => event.type === 'error')).toBe(true);
    expect(states).toContain('restarting');

    await manager.stop();
  });

  it('send 非法请求形状 → 抛 TypeError', async () => {
    const manager = new SidecarManager({
      onEvent: () => undefined,
      onLog: () => undefined,
      onStateChange: () => undefined,
    });
    await manager.start({ cmd: process.execPath, args: [FAKE_SIDECAR] });
    expect(() =>
      manager.send({ type: 'nonsense' } as unknown as Parameters<typeof manager.send>[0]),
    ).toThrow(TypeError);
    await manager.stop();
  });
});
