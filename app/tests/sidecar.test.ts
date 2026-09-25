/**
 * SidecarManager × fake-sidecar：真实 spawn，验证事件转发、stop 收尾与崩溃重启重发 configure。
 */
import { describe, expect, it } from 'vitest';
import { SidecarManager } from '../src/main/sidecar';
import type { EngineEvent } from '../src/shared/protocol';
import { FAKE_SIDECAR, configureRequest, runRequest } from './support/requests';

function waitFor(predicate: () => boolean, timeoutMs = 20_000): Promise<void> {
  return new Promise((resolve, reject) => {
    const started = Date.now();
    const tick = (): void => {
      if (predicate()) return resolve();
      if (Date.now() - started > timeoutMs) return reject(new Error('waitFor 超时'));
      setTimeout(tick, 20);
    };
    tick();
  });
}

function manager(events: EngineEvent[], states: string[] = []): SidecarManager {
  return new SidecarManager({
    onEvent: (event) => events.push(event),
    onLog: () => undefined,
    onStateChange: (state) => states.push(state),
  });
}

const START = { cmd: process.execPath, args: [FAKE_SIDECAR], env: { FAKE_SIDECAR_TICK_MS: '1' } };

describe('SidecarManager × fake-sidecar', () => {
  it('configure → run：事件按 seq 递增转发，stop 后 exited', async () => {
    const events: EngineEvent[] = [];
    const sidecar = manager(events);
    await sidecar.start(START);
    expect(sidecar.getState()).toBe('running');
    sidecar.send(configureRequest());
    sidecar.send(runRequest('doc-test'));
    await waitFor(() => events.some((event) => event.type === 'run_finished'));
    for (let i = 1; i < events.length; i += 1) {
      expect(events[i].seq).toBeGreaterThan(events[i - 1].seq);
    }
    expect(events.filter((event) => event.type === 'paragraph')).toHaveLength(36);
    await sidecar.stop();
    expect(sidecar.getState()).toBe('exited');
    await sidecar.stop(); // 重复 stop 无害
  });

  it('崩溃重启：合成 sidecar_crashed 错误，重启后自动重发 configure', async () => {
    const events: EngineEvent[] = [];
    const states: string[] = [];
    const sidecar = manager(events, states);
    await sidecar.start(START);
    sidecar.send(configureRequest());
    const child = (sidecar as unknown as { child: { kill: (signal: string) => void } }).child;
    child.kill('SIGKILL');
    await waitFor(() => states.includes('restarting') && sidecar.getState() === 'running');
    expect(events.some((event) => event.type === 'error' && event.code === 'sidecar_crashed')).toBe(true);

    // configure 被重发 → run 能正常跑完（否则 fake-sidecar 回致命错误 + ok:false）
    sidecar.send(runRequest('after-restart'));
    await waitFor(() => events.some((event) => event.type === 'run_finished'));
    expect(events.find((event) => event.type === 'run_finished')).toMatchObject({ ok: true });
    await sidecar.stop();
  });

  it('send 非法请求形状 → 抛 TypeError', async () => {
    const sidecar = manager([]);
    await sidecar.start(START);
    expect(() =>
      sidecar.send({ type: 'nonsense' } as unknown as Parameters<typeof sidecar.send>[0]),
    ).toThrow(TypeError);
    await sidecar.stop();
  });
});
