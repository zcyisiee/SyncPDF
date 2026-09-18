/**
 * W14 的 job_update 链路（前端侧）：SSE 帧 → 缓存失效 → 轮询节流。
 *
 * 三段分开测（各自的最小依赖）：
 * 1. `useEventStream` 的 `job_update` 分支（stub EventSource，jsdom 没有原生实现）；
 * 2. `useJobUpdates` → 真 QueryClient 的 `invalidateQueries` 调用集合（按 action 分流）；
 * 3. 节流：`useJobs` 的静默窗口 + `useJobUpdates` 的定时器一起构成
 *    「收到推送后 5s 内不轮询，窗口一过按 5s 兜底」这条链路。
 */
import { QueryClientProvider } from '@tanstack/react-query';
import { act, renderHook } from '@testing-library/react';
import type { ReactNode } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createQueryClient } from '../src/app/App';
import type { EventSourceLike } from '../src/components/events/useEventStream';
import { useEventStream } from '../src/components/events/useEventStream';
import { useJobUpdates } from '../src/components/events/useJobUpdates';
import { JOB_UPDATE_KIND, type JobUpdate } from '../src/lib/events';
import { JOBS_UPDATE_QUIET_MS } from '../src/lib/jobs';
import { useJobs } from '../src/lib/queries';
import { jsonResponse, makeJob, mockApiFetch } from './helpers';

const DID = 'alpha';
const RUN_ID = '20260917T110000Z-0000b2';
const JOB_ID = 'j_01M2RDB312K20Q280DHCTX7N19';

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={createQueryClient()}>{children}</QueryClientProvider>;
}

type Listener = (event: MessageEvent<string>) => void;

/** 最小 EventSource 替身（与 event-stream-panel.test.tsx 同一套做法）。 */
class StubEventSource implements EventSourceLike {
  readyState = 1;
  readonly listeners = new Map<string, Listener[]>();
  closeCount = 0;
  constructor(readonly url: string) {}

  addEventListener(type: string, listener: Listener): void {
    const existing = this.listeners.get(type) ?? [];
    existing.push(listener);
    this.listeners.set(type, existing);
  }

  close(): void {
    this.closeCount += 1;
    this.readyState = 2;
  }

  emit(type: string, data: string): void {
    for (const listener of this.listeners.get(type) ?? []) {
      listener({ data } as MessageEvent<string>);
    }
  }
}

function stubFactory(sources: StubEventSource[]) {
  return (url: string) => {
    const source = new StubEventSource(url);
    sources.push(source);
    return source;
  };
}

function frameFor(overrides: Partial<Record<string, unknown>> = {}): string {
  return JSON.stringify({
    kind: JOB_UPDATE_KIND,
    data: {
      job_id: JOB_ID,
      action: 'run',
      status: 'running',
      from_stage: 'translate',
      error_code: null,
      ...overrides,
    },
  });
}

beforeEach(() => {
  vi.unstubAllGlobals();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('useEventStream：job_update 分支', () => {
  it('注册 job_update 监听 → 回调 JobUpdate（不进 onEvents）', () => {
    const sources: StubEventSource[] = [];
    const onEvents = vi.fn();
    const onJobUpdate = vi.fn();
    renderHook(() =>
      useEventStream({
        did: DID,
        runId: RUN_ID,
        afterSeq: 0,
        kinds: ['stage_started', JOB_UPDATE_KIND],
        onEvents,
        onJobUpdate,
        createEventSource: stubFactory(sources),
      }),
    );

    expect(sources[0].listeners.has(JOB_UPDATE_KIND)).toBe(true);
    act(() => {
      sources[0].emit(JOB_UPDATE_KIND, frameFor());
    });
    expect(onJobUpdate).toHaveBeenCalledTimes(1);
    expect(onJobUpdate.mock.calls[0][0]).toEqual<JobUpdate>({
      job_id: JOB_ID,
      action: 'run',
      status: 'running',
      from_stage: 'translate',
      error_code: null,
    });
    // 它不是归档事件：没有 seq，绝不能混进事件窗口
    expect(onEvents).not.toHaveBeenCalled();
  });

  it('坏 JSON / 不是 job_update / 缺字段 → 丢掉，不炸流也不触发失效', () => {
    const sources: StubEventSource[] = [];
    const onJobUpdate = vi.fn();
    renderHook(() =>
      useEventStream({
        did: DID,
        runId: RUN_ID,
        afterSeq: 0,
        kinds: [JOB_UPDATE_KIND],
        onEvents: () => {},
        onJobUpdate,
        createEventSource: stubFactory(sources),
      }),
    );
    act(() => {
      sources[0].emit(JOB_UPDATE_KIND, 'not json');
      sources[0].emit(JOB_UPDATE_KIND, '{"seq":1,"stage":"parse","kind":"stage_started","data":{}}');
      sources[0].emit(JOB_UPDATE_KIND, JSON.stringify({ kind: JOB_UPDATE_KIND, data: {} }));
    });
    expect(onJobUpdate).not.toHaveBeenCalled();
  });

  it('没给 onJobUpdate 时也不炸（连接照建，帧丢掉）', () => {
    const sources: StubEventSource[] = [];
    const onEvents = vi.fn();
    renderHook(() =>
      useEventStream({
        did: DID,
        runId: RUN_ID,
        afterSeq: 0,
        kinds: [JOB_UPDATE_KIND],
        onEvents,
        createEventSource: stubFactory(sources),
      }),
    );
    act(() => {
      sources[0].emit(JOB_UPDATE_KIND, frameFor());
    });
    expect(onEvents).not.toHaveBeenCalled();
    expect(sources[0].closeCount).toBe(0);
  });
});

describe('useJobUpdates：按 action 失效 + 静默窗口时间戳', () => {
  function setup(did: string | null = DID) {
    const client = createQueryClient();
    const spy = vi.spyOn(client, 'invalidateQueries');
    const hook = renderHook(() => useJobUpdates(did), {
      wrapper: ({ children }: { children: ReactNode }) => (
        <QueryClientProvider client={client}>{children}</QueryClientProvider>
      ),
    });
    return { client, spy, hook };
  }

  const invalidated = (spy: { mock: { calls: unknown[][] } }) =>
    spy.mock.calls.map((call) => {
      const options = call[0] as { queryKey?: readonly unknown[] } | undefined;
      return (options?.queryKey ?? []).join('/');
    });

  it('run 的推送：job 列表 + 详情 + 阶段状态 + 段落 + 事件窗口 + 文档列表，并记下推送时刻', () => {
    const { spy, hook } = setup();
    act(() => {
      hook.result.current.onJobUpdate({
        job_id: JOB_ID,
        action: 'run',
        status: 'running',
        from_stage: 'translate',
        error_code: null,
      });
    });
    expect(invalidated(spy)).toEqual([
      'documents/alpha/jobs',
      'documents/alpha',
      'documents/alpha/stage-state',
      'documents/alpha/paragraphs',
      'documents/alpha/events',
      'documents',
    ]);
    expect(hook.result.current.pushedAtMs).not.toBeNull();
  });

  it('compile → 详情+版本；retranslate → 段落/候选；终态都补产物清单', () => {
    const { spy, hook } = setup();
    act(() => {
      hook.result.current.onJobUpdate({
        job_id: JOB_ID,
        action: 'compile',
        status: 'succeeded',
        from_stage: 'apply',
        error_code: null,
      });
    });
    expect(invalidated(spy)).toContain('documents/alpha/versions');
    expect(invalidated(spy)).toContain('documents/alpha/artifacts');
    expect(invalidated(spy)).not.toContain('documents/alpha/events');

    spy.mockClear();
    act(() => {
      hook.result.current.onJobUpdate({
        job_id: JOB_ID,
        action: 'retranslate',
        status: 'succeeded',
        from_stage: null,
        error_code: null,
      });
    });
    const keys = invalidated(spy);
    expect(keys).toContain('documents/alpha/paragraphs');
    expect(keys).toContain('documents/alpha/artifacts');
    expect(keys).not.toContain('documents/alpha/stage-state');
  });

  it('did 为空时不发任何请求（工作台还没进文档）', () => {
    const { spy, hook } = setup(null);
    act(() => {
      hook.result.current.onJobUpdate({
        job_id: JOB_ID,
        action: 'run',
        status: 'running',
        from_stage: null,
        error_code: null,
      });
    });
    expect(spy).not.toHaveBeenCalled();
    expect(hook.result.current.pushedAtMs).toBeNull();
  });

  it('静默窗口到点后自动清掉时间戳（轮询恢复的唯一出口）', () => {
    vi.useFakeTimers();
    const { hook } = setup();
    act(() => {
      hook.result.current.onJobUpdate({
        job_id: JOB_ID,
        action: 'run',
        status: 'running',
        from_stage: null,
        error_code: null,
      });
    });
    expect(hook.result.current.pushedAtMs).not.toBeNull();

    act(() => {
      vi.advanceTimersByTime(JOBS_UPDATE_QUIET_MS - 1);
    });
    expect(hook.result.current.pushedAtMs).not.toBeNull(); // 窗口内：还压着

    act(() => {
      vi.advanceTimersByTime(1);
    });
    expect(hook.result.current.pushedAtMs).toBeNull(); // 到点 → 恢复正常轮询
  });
});

describe('useJobs 节流：静默窗口内不轮询，窗口结束后按 5s 兜底', () => {
  /** 推进假时钟并冲刷微任务（react-query 的 fetch 在微任务里落地）。 */
  async function tick(ms: number) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(ms);
    });
  }

  it('推送到达时取消已经排上的那次兜底轮询，窗口结束后按 5s 继续', async () => {
    vi.useFakeTimers();
    const fetchMock = mockApiFetch({
      [`/api/v1/documents/${DID}/jobs`]: () => jsonResponse([makeJob({ status: 'running' })]),
    });
    const { rerender } = renderHook(
      ({ quietSinceMs }: { quietSinceMs: number | null }) => useJobs(DID, { quietSinceMs }),
      { wrapper, initialProps: { quietSinceMs: null as number | null } },
    );

    // 首拉 + 兜底计时器起跑（活动 job → 5s 后到点）
    await tick(0);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // t=4.5s：SSE 推了一帧（useJobUpdates 记下时刻并让查询失效）→ 静默窗口开始。
    // 关键断言在下一段：t=5.0s 那次**已经排上的**兜底轮询必须被取消。
    await tick(4_500);
    rerender({ quietSinceMs: Date.now() });
    await tick(1_000); // 到 t=5.5s：没有节流的话这里必然已经发了第二次
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // 窗口结束（时间戳被定时器清掉）→ 兜底轮询按 5s 重新起跑
    rerender({ quietSinceMs: null });
    await tick(5_000);
    expect(fetchMock).toHaveBeenCalledTimes(2);

    await tick(5_000);
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it('refetchMs 显式给定时覆盖节流（W10 的编译状态条要按自己的节奏）', async () => {
    vi.useFakeTimers();
    const fetchMock = mockApiFetch({
      [`/api/v1/documents/${DID}/jobs`]: () => jsonResponse([]),
    });
    renderHook(() => useJobs(DID, { refetchMs: 1_000, quietSinceMs: Date.now() }), {
      wrapper,
    });
    await tick(0);
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await tick(1_000);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
