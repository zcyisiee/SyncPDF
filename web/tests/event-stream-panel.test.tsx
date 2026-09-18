/**
 * 事件面板（W06）：尾部 200 条窗口、kind 分组过滤、空态、SSE 状态行、展开原始 JSON、
 * 「载入更早」与「新事件」浮标。jsdom 没有原生 EventSource，所以：
 * - 面板渲染测试直接喂 `EventFeed` 数据（面板是纯渲染）；
 * - `useEventStream` / `useEventWindow` 的连接/关闭/重建用 **stub 类**覆盖，并断言它拿到的 URL。
 */
import { QueryClientProvider } from '@tanstack/react-query';
import { act, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { describe, expect, it, vi } from 'vitest';

import { createQueryClient } from '../src/app/App';
import { EventStreamPanel } from '../src/components/events/EventStreamPanel';
import { useEventStream, type EventSourceLike } from '../src/components/events/useEventStream';
import { useEventWindow } from '../src/components/events/useEventWindow';
import { EVENTS_WINDOW_SIZE, type RunEvent } from '../src/lib/events';
import { jsonResponse, makeEvent, makeEventFeed, mockApiFetch } from './helpers';

const DID = 'ccs3764-dyn';
const RUN_ID = '20260916T132829Z-000183';

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={createQueryClient()}>{children}</QueryClientProvider>;
}

/** 事件窗口的替身：按 seq 升序造 `n` 条，最后一条是 `stage_finished`/`report`（run 收尾）。 */
function windowOf(n: number, offset = 0): RunEvent[] {
  return Array.from({ length: n }, (_unused, index) =>
    makeEvent(offset + index + 1, {
      kind: offset + index + 1 === offset + n ? 'stage_finished' : 'call_started',
      stage: offset + index + 1 === offset + n ? 'report' : 'translate',
    }),
  );
}

describe('EventStreamPanel', () => {
  it('窗口里的行全部渲染（默认 200 条上限）且最新在上', () => {
    render(
      <EventStreamPanel
        did={DID}
        feed={makeEventFeed({ events: windowOf(EVENTS_WINDOW_SIZE), runId: RUN_ID })}
      />,
    );
    const rows = document.querySelectorAll('[data-od-id="event-row"]');
    expect(rows).toHaveLength(EVENTS_WINDOW_SIZE);
    expect(rows[0].getAttribute('data-seq')).toBe(String(EVENTS_WINDOW_SIZE));
    expect(rows[EVENTS_WINDOW_SIZE - 1].getAttribute('data-seq')).toBe('1');
    expect(
      screen.getByText(new RegExp(`显示 ${EVENTS_WINDOW_SIZE}/${EVENTS_WINDOW_SIZE}`)),
    ).toBeInTheDocument();
  });

  it('kind 分组过滤只影响显示（窗口条数不变），并显示分组空态', () => {
    const events = [
      makeEvent(1, { kind: 'call_started' }),
      makeEvent(2, { kind: 'cache_miss' }),
      makeEvent(3, { kind: 'candidate_evaluated' }),
    ];
    render(<EventStreamPanel did={DID} feed={makeEventFeed({ events, runId: RUN_ID })} />);
    expect(document.querySelectorAll('[data-od-id="event-row"]')).toHaveLength(3);

    fireEvent.change(screen.getByLabelText('事件分组'), { target: { value: 'cache' } });
    const filtered = document.querySelectorAll('[data-od-id="event-row"]');
    expect(filtered).toHaveLength(1);
    expect(filtered[0].getAttribute('data-kind-group')).toBe('cache');
    expect(screen.getByText(/显示 1\/3/)).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText('事件分组'), { target: { value: 'stage' } });
    expect(document.querySelectorAll('[data-od-id="event-row"]')).toHaveLength(0);
    expect(screen.getByText(/当前分组（阶段）在窗口里没有事件/)).toBeInTheDocument();
    expect(screen.getByText(/显示 0\/3/)).toBeInTheDocument();
  });

  it('点击行展开原始 JSON（再点收起）', () => {
    render(
      <EventStreamPanel did={DID} feed={makeEventFeed({ events: windowOf(2), runId: RUN_ID })} />,
    );
    const row = document.querySelector('[data-od-id="event-row"] button') as HTMLElement;
    expect(document.querySelector('[data-od-id="event-row-json"]')).toBeNull();
    fireEvent.click(row);
    expect(document.querySelector('[data-od-id="event-row-json"]')?.textContent).toContain(
      '"kind": "stage_finished"',
    );
    expect(row.getAttribute('aria-expanded')).toBe('true');
    fireEvent.click(row);
    expect(document.querySelector('[data-od-id="event-row-json"]')).toBeNull();
  });

  it('空态三态：加载中 / 没有 run 归档 / 还没有事件', () => {
    const { unmount } = render(
      <EventStreamPanel did={DID} feed={makeEventFeed({ isPending: true, hasArchive: false })} />,
    );
    expect(screen.getByText('正在加载事件…')).toBeInTheDocument();
    unmount();

    const { unmount: unmountArchive } = render(
      <EventStreamPanel did={DID} feed={makeEventFeed({ hasArchive: false })} />,
    );
    expect(screen.getByText(/该文档没有 run 归档/)).toBeInTheDocument();
    unmountArchive();

    render(
      <EventStreamPanel did={DID} feed={makeEventFeed({ events: [], hasArchive: true })} />,
    );
    expect(screen.getByText(/这个 run 还没有事件/)).toBeInTheDocument();
  });

  it('首拉真错误 → 错误文案 + 重试按钮', () => {
    const retry = vi.fn();
    render(
      <EventStreamPanel
        did={DID}
        feed={makeEventFeed({ hasArchive: false, error: new Error('boom'), retry })}
      />,
    );
    expect(screen.getByText('请求失败')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '重试' }));
    expect(retry).toHaveBeenCalledTimes(1);
  });

  it('SSE 状态行：open「实时」带脉冲；断线/关闭/不支持各有文案且不给脉冲', () => {
    const { unmount } = render(
      <EventStreamPanel did={DID} feed={makeEventFeed({ connection: 'open' })} />,
    );
    expect(screen.getByText('实时').querySelector('.pulse-dot')).not.toBeNull();
    unmount();

    const { unmount: unmountBroken } = render(
      <EventStreamPanel did={DID} feed={makeEventFeed({ connection: 'reconnecting' })} />,
    );
    const status = screen.getByText('连接断开，重试中').closest('[data-od-id="event-stream-status"]');
    expect(status).toHaveAttribute('data-status', 'reconnecting');
    unmountBroken();

    const { unmount: unmountClosed } = render(
      <EventStreamPanel did={DID} feed={makeEventFeed({ connection: 'closed' })} />,
    );
    expect(screen.getByText('连接已关闭')).toBeInTheDocument();
    unmountClosed();

    render(
      <EventStreamPanel did={DID} feed={makeEventFeed({ connection: 'unsupported' })} />,
    );
    expect(screen.getByText('此环境不支持实时推送')).toBeInTheDocument();
    expect(document.querySelectorAll('.pulse-dot')).toHaveLength(0);
  });

  it('「载入更早」按 hasEarlier 禁用，点击回调一次', () => {
    const loadEarlier = vi.fn();
    const { unmount } = render(
      <EventStreamPanel
        did={DID}
        feed={makeEventFeed({ events: windowOf(3), hasEarlier: false, loadEarlier })}
      />,
    );
    expect(screen.getByRole('button', { name: '载入更早' })).toBeDisabled();
    unmount();

    render(
      <EventStreamPanel
        did={DID}
        feed={makeEventFeed({ events: windowOf(3), hasEarlier: true, loadEarlier })}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: '载入更早' }));
    expect(loadEarlier).toHaveBeenCalledTimes(1);
  });

  it('新事件到达：停在顶部不加浮标；滚走后累计「↑ N 条新事件」并点击回顶', async () => {
    const events = windowOf(3);
    const { rerender } = render(
      <EventStreamPanel did={DID} feed={makeEventFeed({ events, runId: RUN_ID })} />,
    );
    const scroll = document.querySelector('[data-od-id="event-stream-scroll"]') as HTMLElement;

    scroll.scrollTop = 120;
    fireEvent.scroll(scroll);
    rerender(
      <EventStreamPanel
        did={DID}
        feed={makeEventFeed({ events: [...events, makeEvent(4), makeEvent(5)], runId: RUN_ID })}
      />,
    );
    const floater = await screen.findByRole('button', { name: '↑ 2 条新事件' });
    fireEvent.click(floater);
    await waitFor(() =>
      expect(document.querySelector('[data-od-id="event-new-events"]')).toBeNull(),
    );
  });
});

type Listener = (event: MessageEvent<string>) => void;

/** 最小 EventSource 替身：记录监听器，测试手动触发 open/error/事件。 */
class StubEventSource implements EventSourceLike {
  readyState = 0;
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

  emit(type: string, data = '{}'): void {
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

describe('useEventStream（stub EventSource）', () => {
  it('建连接：URL 带 run_id/after_seq，注册每个 kind + 默认类型；open→open，error→reconnecting/closed', () => {
    const sources: StubEventSource[] = [];
    const onEvents = vi.fn();
    const { result } = renderHook(() =>
      useEventStream({
        did: DID,
        runId: RUN_ID,
        afterSeq: 12,
        kinds: ['stage_started', 'stage_finished'],
        onEvents,
        createEventSource: stubFactory(sources),
      }),
    );
    expect(sources).toHaveLength(1);
    expect(sources[0].url).toBe(
      `/api/v1/documents/${DID}/events/stream?run_id=${RUN_ID}&after_seq=12`,
    );
    expect([...sources[0].listeners.keys()].sort()).toEqual([
      'error',
      'message',
      'open',
      'stage_finished',
      'stage_started',
    ]);
    expect(result.current).toBe('connecting');

    act(() => {
      sources[0].readyState = 1;
      sources[0].emit('open');
    });
    expect(result.current).toBe('open');

    act(() => {
      sources[0].readyState = 0;
      sources[0].emit('error');
    });
    expect(result.current).toBe('reconnecting');

    act(() => {
      sources[0].readyState = 2;
      sources[0].emit('error');
    });
    expect(result.current).toBe('closed');
  });

  it('命名事件与默认类型都会回调；坏 JSON / 坏形状被丢掉（不炸流）', () => {
    const sources: StubEventSource[] = [];
    const onEvents = vi.fn();
    renderHook(() =>
      useEventStream({
        did: DID,
        runId: RUN_ID,
        afterSeq: 0,
        kinds: ['stage_started'],
        onEvents,
        createEventSource: stubFactory(sources),
      }),
    );
    act(() => {
      sources[0].emit(
        'stage_started',
        '{"seq":7,"at":"x","stage":"parse","kind":"stage_started","data":{}}',
      );
      sources[0].emit('message', 'not json');
      sources[0].emit('stage_started', '{"seq":"bad"}');
    });
    expect(onEvents).toHaveBeenCalledTimes(1);
    expect(onEvents.mock.calls[0][0]).toEqual([
      expect.objectContaining({ seq: 7, kind: 'stage_started' }),
    ]);
  });

  it('afterSeq 变化时重建连接并关掉旧连接；unmount 也关闭', () => {
    const sources: StubEventSource[] = [];
    const factory = stubFactory(sources);
    const { rerender, unmount } = renderHook(
      ({ afterSeq }: { afterSeq: number }) =>
        useEventStream({
          did: DID,
          runId: RUN_ID,
          afterSeq,
          kinds: [],
          onEvents: () => {},
          createEventSource: factory,
        }),
      { initialProps: { afterSeq: 12 } },
    );
    expect(sources).toHaveLength(1);

    rerender({ afterSeq: 30 });
    expect(sources).toHaveLength(2);
    expect(sources[0].closeCount).toBe(1);
    expect(sources[1].url).toContain('after_seq=30');

    unmount();
    expect(sources[1].closeCount).toBe(1);
  });

  it('did/runId/afterSeq 缺一 → idle 且不建连接；环境没有 EventSource → unsupported', () => {
    const sources: StubEventSource[] = [];
    const { result } = renderHook(() =>
      useEventStream({
        did: DID,
        runId: null,
        afterSeq: null,
        kinds: [],
        onEvents: () => {},
        createEventSource: stubFactory(sources),
      }),
    );
    expect(result.current).toBe('idle');
    expect(sources).toHaveLength(0);

    // 不注入 factory 且 jsdom 没有 EventSource（有连接参数也不崩）
    const noStub = renderHook(() =>
      useEventStream({ did: DID, runId: RUN_ID, afterSeq: 0, kinds: [], onEvents: () => {} }),
    );
    expect(noStub.result.current).toBe('unsupported');
  });
});

describe('useEventWindow', () => {
  it('首拉尾部窗口 200 条；「载入更早」再拉一页并把窗口放宽到 500', async () => {
    // 单 run 500 条：首拉（after_seq=0&limit=2000）1 次；窗口只留最后 200 条。
    const all = windowOf(500);
    const fetchMock = mockApiFetch({
      [`/api/v1/documents/${DID}/events?after_seq=0&limit=2000`]: () =>
        jsonResponse({ run_id: RUN_ID, events: all, next_after_seq: 500, has_more: false }),
    });
    const { result } = renderHook(() => useEventWindow(DID), { wrapper });
    await waitFor(() => expect(result.current.events).toHaveLength(EVENTS_WINDOW_SIZE));
    expect(result.current.runId).toBe(RUN_ID);
    expect(result.current.hasArchive).toBe(true);
    expect(result.current.events[0].seq).toBe(301);
    expect(result.current.hasEarlier).toBe(true);
    expect(fetchMock).toHaveBeenCalledTimes(1);

    // 最旧 301 → 游标 max(0, 301-1-2000)=0 → 再拉一页，只并 seq<301 的 300 条
    act(() => result.current.loadEarlier());
    await waitFor(() => expect(result.current.events).toHaveLength(500));
    expect(result.current.events[0].seq).toBe(1);
    expect(result.current.hasEarlier).toBe(false);
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('没有 run 归档（404 events_unavailable）→ hasArchive=false、不是错误、不建 SSE', async () => {
    mockApiFetch({
      [`/api/v1/documents/${DID}/events?after_seq=0&limit=2000`]: () =>
        jsonResponse({ error: { code: 'events_unavailable', message: '没有任何 run 归档' } }, 404),
    });
    const { result } = renderHook(() => useEventWindow(DID), { wrapper });
    await waitFor(() => expect(result.current.isPending).toBe(false));
    expect(result.current.hasArchive).toBe(false);
    expect(result.current.error).toBeNull();
    expect(result.current.connection).toBe('idle'); // 没有 run 归档 → 不建 EventSource
  });

  it('真错误（500）留在 error 上，面板据此显示错误卡', async () => {
    mockApiFetch({
      [`/api/v1/documents/${DID}/events?after_seq=0&limit=2000`]: () =>
        jsonResponse({ error: { code: 'internal_error', message: '炸了' } }, 500),
    });
    const { result } = renderHook(() => useEventWindow(DID), { wrapper });
    await waitFor(() => expect(result.current.error).not.toBeNull());
    expect(result.current.hasArchive).toBe(false);
  });
});
