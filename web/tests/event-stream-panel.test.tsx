/**
 * 事件面板（W06）：尾部 200 条窗口、kind 分组过滤、空态、SSE 状态行、展开原始 JSON、
 * 「载入更早」与「新事件」浮标。jsdom 没有原生 EventSource，所以：
 * - 面板渲染测试直接喂 `EventFeed` 数据（面板是纯渲染）；
 * - `useEventStream` / `useEventWindow` 的连接/关闭/重建用 **stub 类**覆盖，并断言它拿到的 URL。
 */
import { QueryClientProvider } from '@tanstack/react-query';
import { act, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react';
import type { ReactElement, ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { createQueryClient } from '../src/app/App';
import { EventRow, eventTime, TL_ROW_CLASS } from '../src/components/events/EventRow';
import { EventStreamPanel } from '../src/components/events/EventStreamPanel';
import { useEventStream, type EventSourceLike } from '../src/components/events/useEventStream';
import { useEventWindow } from '../src/components/events/useEventWindow';
import { EVENTS_WINDOW_SIZE, type RunEvent } from '../src/lib/events';
import { STAGE_NAMES } from '../src/lib/humanize';
import { uiStore } from '../src/stores/ui';
import { jsonResponse, makeEvent, makeEventFeed, mockApiFetch } from './helpers';

const DID = 'ccs3764-dyn';
const RUN_ID = '20260916T132829Z-000183';
const STAGE_STATE_URL = `/api/v1/documents/${DID}/stage-state`;
const JOBS_URL = `/api/v1/documents/${DID}/jobs`;

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={createQueryClient()}>{children}</QueryClientProvider>;
}

/**
 * 面板的渲染入口：阶段条是 stage-state 的第二个订阅方（与 WorkbenchScreen 共用同一份
 * 查询），所以渲染前先 mock 这两个端点；未 mock 的路径返回 404 会落到「阶段状态不可用」。
 */
function renderPanel(ui: ReactElement, routes: Record<string, () => Response> = {}) {
  mockApiFetch({
    [STAGE_STATE_URL]: () => jsonResponse(stageStateOf([])),
    [JOBS_URL]: () => jsonResponse([]),
    ...routes,
  });
  const result = render(wrapper({ children: ui }));
  // 重渲染必须保住 QueryClientProvider（阶段条/任务查询都靠它）
  return { ...result, rerender: (next: ReactElement) => result.rerender(wrapper({ children: next })) };
}

/** `GET /stage-state` 的替身：`entries` 里给的阶段用该状态与耗时，其余 `not_run`。 */
function stageStateOf(entries: [stage: string, status: string, durationS?: number][]) {
  const byStage = new Map<string, string>(entries.map(([stage, status]) => [stage, status]));
  const durations = new Map<string, number>(
    entries.flatMap(([stage, , durationS]) =>
      durationS === undefined ? [] : ([[stage, durationS]] as const),
    ),
  );
  return {
    did: DID,
    run_id: RUN_ID,
    stages: STAGE_NAMES.map((stage) => ({
      stage,
      status: byStage.get(stage) ?? 'not_run',
      ok: byStage.get(stage) === 'ok',
      started_at: '2026-09-16T13:28:29.000Z',
      finished_at: '2026-09-16T13:28:29.000Z',
      duration_s: durations.get(stage) ?? null,
      timing_source: 'manifest',
    })),
  };
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

// 定位桥（`locate`）是全局 store 的会话态：每个用例都从「没点过」开始。
beforeEach(() => {
  uiStore.setState({ locate: null });
});

/** 7 段全 ok 的 stage-state（阶段条「N 阶段完成」的口径来源）。 */
const STAGE_STATES_ALL_OK: [stage: string, status: string, durationS: number][] = [
  ['parse', 'ok', 15.83],
  ['translate', 'ok', 248.93],
  ['apply', 'ok', 4.07],
  ['build', 'ok', 23.85],
  ['check', 'ok', 25.23],
  ['review', 'ok', 0.32],
  ['report', 'ok', 0],
];

describe('eventTime（北京时间显示）', () => {
  it('带时区的 UTC 归档时刻按 Asia/Shanghai 换算（+8h），不跟浏览器本地时区', () => {
    expect(eventTime('2026-09-19T09:16:25.710+00:00')).toBe('17:16:25');
    expect(eventTime('2026-09-19T09:16:25.710Z')).toBe('17:16:25');
    // 跨日：UTC 的 2026-09-19T20:00:00Z 是北京的 09-20 04:00。
    expect(eventTime('2026-09-19T20:00:00+00:00')).toBe('04:00:00');
    // UTC 00:30 → 北京 08:30（不因跨日错位）。
    expect(eventTime('2026-09-19T00:30:00+00:00')).toBe('08:30:00');
  });

  it('无时区的裸串按 UTC 解释（job_events.created_at 是 SQLite CURRENT_TIMESTAMP）', () => {
    expect(eventTime('2026-09-19 09:22:04')).toBe('17:22:04');
  });

  it('region=utc 保留原始 UTC 时刻；坏值 → —', () => {
    expect(eventTime('2026-09-19T09:16:25.710+00:00', 'utc')).toBe('09:16:25');
    expect(eventTime('')).toBe('—');
    expect(eventTime('not-a-time')).toBe('—');
  });

  it('行内显示北京时间，title 里同时给出 UTC 原文与北京时间', () => {
    render(
      <EventRow
        event={makeEvent(1, { at: '2026-09-19T09:16:25.710+00:00' })}
        expanded={false}
        onToggle={() => {}}
      />,
    );
    const stamp = screen.getByText('17:16:25');
    expect(stamp).toBeInTheDocument();
    expect(stamp.getAttribute('title')).toContain('2026-09-19T09:16:25.710+00:00');
  });
});

describe('EventStreamPanel', () => {
  it('窗口里的行全部渲染（默认 200 条上限）且最新在上', () => {
    renderPanel(
      <EventStreamPanel
        did={DID}
        compileEvents={[]}
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
    renderPanel(<EventStreamPanel did={DID} compileEvents={[]} feed={makeEventFeed({ events, runId: RUN_ID })} />);
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
    renderPanel(
      <EventStreamPanel did={DID} compileEvents={[]} feed={makeEventFeed({ events: windowOf(2), runId: RUN_ID })} />,
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
    const { unmount } = renderPanel(
      <EventStreamPanel did={DID} compileEvents={[]} feed={makeEventFeed({ isPending: true, hasArchive: false })} />,
    );
    expect(screen.getByText('正在加载事件…')).toBeInTheDocument();
    unmount();

    const { unmount: unmountArchive } = renderPanel(
      <EventStreamPanel did={DID} compileEvents={[]} feed={makeEventFeed({ hasArchive: false })} />,
    );
    expect(screen.getByText(/该文档没有 run 归档/)).toBeInTheDocument();
    unmountArchive();

    renderPanel(
      <EventStreamPanel did={DID} compileEvents={[]} feed={makeEventFeed({ events: [], hasArchive: true })} />,
    );
    expect(screen.getByText(/这个 run 还没有事件/)).toBeInTheDocument();
  });

  it('首拉真错误 → 错误文案 + 重试按钮', () => {
    const retry = vi.fn();
    renderPanel(
      <EventStreamPanel
        did={DID}
        compileEvents={[]}
        feed={makeEventFeed({ hasArchive: false, error: new Error('boom'), retry })}
      />,
    );
    expect(screen.getByText('请求失败')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '重试' }));
    expect(retry).toHaveBeenCalledTimes(1);
  });

  it('SSE 状态行：open「实时」带脉冲；断线/关闭/不支持各有文案且不给脉冲', () => {
    const { unmount } = renderPanel(
      <EventStreamPanel did={DID} compileEvents={[]} feed={makeEventFeed({ connection: 'open' })} />,
    );
    expect(screen.getByText('实时').querySelector('.pulse-dot')).not.toBeNull();
    unmount();

    const { unmount: unmountBroken } = renderPanel(
      <EventStreamPanel did={DID} compileEvents={[]} feed={makeEventFeed({ connection: 'reconnecting' })} />,
    );
    const status = screen.getByText('连接断开，重试中').closest('[data-od-id="event-stream-status"]');
    expect(status).toHaveAttribute('data-status', 'reconnecting');
    unmountBroken();

    const { unmount: unmountClosed } = renderPanel(
      <EventStreamPanel did={DID} compileEvents={[]} feed={makeEventFeed({ connection: 'closed' })} />,
    );
    expect(screen.getByText('连接已关闭')).toBeInTheDocument();
    unmountClosed();

    renderPanel(
      <EventStreamPanel did={DID} compileEvents={[]} feed={makeEventFeed({ connection: 'unsupported' })} />,
    );
    expect(screen.getByText('此环境不支持实时推送')).toBeInTheDocument();
    expect(document.querySelectorAll('.pulse-dot')).toHaveLength(0);
  });

  it('「载入更早」按 hasEarlier 禁用，点击回调一次', () => {
    const loadEarlier = vi.fn();
    const { unmount } = renderPanel(
      <EventStreamPanel
        did={DID}
        compileEvents={[]}
        feed={makeEventFeed({ events: windowOf(3), hasEarlier: false, loadEarlier })}
      />,
    );
    expect(screen.getByRole('button', { name: '载入更早' })).toBeDisabled();
    unmount();

    renderPanel(
      <EventStreamPanel
        did={DID}
        compileEvents={[]}
        feed={makeEventFeed({ events: windowOf(3), hasEarlier: true, loadEarlier })}
      />,
    );
    fireEvent.click(screen.getByRole('button', { name: '载入更早' }));
    expect(loadEarlier).toHaveBeenCalledTimes(1);
  });

  it('新事件到达：停在顶部不加浮标；滚走后累计「↑ N 条新事件」并点击回顶', async () => {
    const events = windowOf(3);
    const { rerender } = renderPanel(
      <EventStreamPanel did={DID} compileEvents={[]} feed={makeEventFeed({ events, runId: RUN_ID })} />,
    );
    const scroll = document.querySelector('[data-od-id="event-stream-scroll"]') as HTMLElement;

    scroll.scrollTop = 120;
    fireEvent.scroll(scroll);
    rerender(
      <EventStreamPanel
        did={DID}
        compileEvents={[]}
        feed={makeEventFeed({ events: [...events, makeEvent(4), makeEvent(5)], runId: RUN_ID })}
      />,
    );
    const floater = await screen.findByRole('button', { name: '↑ 2 条新事件' });
    fireEvent.click(floater);
    await waitFor(() =>
      expect(document.querySelector('[data-od-id="event-new-events"]')).toBeNull(),
    );
  });

  it('换 run（重新开始翻译）：滚动贴回顶部、浮标清零、展开行收起、seq 从头显示', async () => {
    const first = windowOf(3);
    const { rerender } = renderPanel(
      <EventStreamPanel did={DID} compileEvents={[]} feed={makeEventFeed({ events: first, runId: RUN_ID })} />,
    );
    const scroll = document.querySelector('[data-od-id="event-stream-scroll"]') as HTMLElement;

    // 滚走 + 展开一行 + 累计浮标：换 run 前先造出「非干净」的面板状态。
    scroll.scrollTop = 120;
    fireEvent.scroll(scroll);
    fireEvent.click(document.querySelector('[data-od-id="event-row"] button') as HTMLElement);
    expect(document.querySelector('[data-od-id="event-row-json"]')).not.toBeNull();
    rerender(
      <EventStreamPanel
        did={DID}
        compileEvents={[]}
        feed={makeEventFeed({ events: [...first, makeEvent(4)], runId: RUN_ID })}
      />,
    );
    await screen.findByRole('button', { name: '↑ 1 条新事件' });

    // 新 run：seq 从 1 重新计数（同一份 windowOf 只是换了 run_id）。
    const NEW_RUN = '20260920T010000Z-000200';
    rerender(
      <EventStreamPanel did={DID} compileEvents={[]} feed={makeEventFeed({ events: windowOf(2), runId: NEW_RUN })} />,
    );

    expect(screen.getByText(new RegExp(NEW_RUN))).toBeInTheDocument();
    expect(document.querySelector('[data-od-id="event-new-events"]')).toBeNull();
    expect(document.querySelector('[data-od-id="event-row-json"]')).toBeNull();
    expect(scroll.scrollTop).toBe(0);
    const rows = document.querySelectorAll('[data-od-id="event-row"]');
    expect(rows).toHaveLength(2);
    expect(rows[0].getAttribute('data-seq')).toBe('2');
    expect(rows[1].getAttribute('data-seq')).toBe('1');
  });
});

describe('阶段条头部（stage-strip）', () => {
  it('渲染 7 段（顺序 = STAGE_NAMES）且逐段带状态色钩子；全 ok → 「7 阶段完成」', async () => {
    renderPanel(
      <EventStreamPanel did={DID} compileEvents={[]} feed={makeEventFeed({ events: windowOf(2), runId: RUN_ID })} />,
      {
        [STAGE_STATE_URL]: () => jsonResponse(stageStateOf(STAGE_STATES_ALL_OK)),
      },
    );
    await waitFor(() => expect(screen.getByText('7 阶段完成')).toBeInTheDocument());
    const segments = document.querySelectorAll('[data-od-id="stage-strip-segment"]');
    expect(segments).toHaveLength(7);
    expect([...segments].map((node) => node.getAttribute('data-stage'))).toEqual([...STAGE_NAMES]);
    expect([...segments].map((node) => node.getAttribute('data-state'))).toEqual(
      Array.from({ length: 7 }, () => 'ok'),
    );
    // 均分（不按耗时造假比例）：title 里给真实的阶段 / 状态 / 实测耗时
    expect(segments[0].getAttribute('title')).toBe('解析 · 已完成 · 15s');
    // 非 live 不给脉冲（全站唯一动效只出现在真正运行中时；SSE 状态行的点不算）
    const strip = document.querySelector('[data-od-id="stage-strip"]') as HTMLElement;
    expect(strip.querySelectorAll('.pulse-dot')).toHaveLength(0);
  });

  it('有失败阶段 → 「{阶段}失败」红字；live 段 → 脉冲点 + 「{阶段}中」', async () => {
    const failed = renderPanel(
      <EventStreamPanel did={DID} compileEvents={[]} feed={makeEventFeed({ events: windowOf(2), runId: RUN_ID })} />,
      {
        [STAGE_STATE_URL]: () =>
          jsonResponse(
            stageStateOf([
              ['parse', 'ok', 15.83],
              ['translate', 'failed', 12.4],
            ]),
          ),
      },
    );
    const failedStatus = await screen.findByText('翻译失败');
    expect(failedStatus.getAttribute('data-live')).toBe('false');
    failed.unmount();

    renderPanel(
      <EventStreamPanel did={DID} compileEvents={[]} feed={makeEventFeed({ events: windowOf(2), runId: RUN_ID })} />,
      {
        [STAGE_STATE_URL]: () => jsonResponse(stageStateOf([['translate', 'running']])),
      },
    );
    const liveStatus = await screen.findByText(/^翻译中/);
    expect(liveStatus.getAttribute('data-live')).toBe('true');
    expect(liveStatus.querySelector('.pulse-dot')).not.toBeNull();
  });

  it('stage-state 读不到 → 整条替换为「阶段状态不可用」微字', async () => {
    renderPanel(
      <EventStreamPanel did={DID} compileEvents={[]} feed={makeEventFeed({ events: windowOf(1), runId: RUN_ID })} />,
      {
        [STAGE_STATE_URL]: () => jsonResponse({ error: { code: 'internal_error', message: '炸了' } }, 500),
      },
    );
    expect(await screen.findByText('阶段状态不可用')).toBeInTheDocument();
    expect(document.querySelectorAll('[data-od-id="stage-strip-segment"]')).toHaveLength(0);
  });
});

describe('节点重排 + 在预览中定位', () => {
  it('节点 = 类型 chip + seq 右对齐 + 叙述行 + 展开详情；全量保留 od-id', () => {
    renderPanel(
      <EventStreamPanel
        did={DID}
        compileEvents={[]}
        feed={makeEventFeed({
          events: [makeEvent(1, { kind: 'stage_finished', stage: 'parse', data: { status: 'ok' } })],
          runId: RUN_ID,
        })}
      />,
    );
    const row = document.querySelector('[data-od-id="event-row"]') as HTMLElement;
    expect(row).not.toBeNull();
    expect(row.textContent).toContain('阶段完成'); // kind chip 人话标签
    expect(row.textContent).toContain('解析');
    expect(row.textContent).toContain('展开详情');
    fireEvent.click(row.querySelector('button') as HTMLElement);
    expect(document.querySelector('[data-od-id="event-row-json"]')?.textContent).toContain(
      '"kind": "stage_finished"',
    );
  });

  it('事件带 page → 渲染「在预览中定位」并带 paragraph_id 写进 store；无 page 不渲染', () => {
    renderPanel(
      <EventStreamPanel
        did={DID}
        compileEvents={[]}
        feed={makeEventFeed({
          events: [
            makeEvent(1, { kind: 'paragraph_done', data: { page: 5, paragraph_id: 'P02-003' } }),
            makeEvent(2, { kind: 'call_started', data: { attempt: 1 } }),
          ],
          runId: RUN_ID,
        })}
      />,
    );
    const links = document.querySelectorAll('[data-od-id="event-locate"]');
    expect(links).toHaveLength(1);
    fireEvent.click(links[0]);
    expect(uiStore.getState().locate).toMatchObject({ nonce: 1, page: 5, paragraphId: 'P02-003' });

    // 同页重复点击也要能再触发一次（nonce 自增，不是同值短路）
    fireEvent.click(links[0]);
    expect(uiStore.getState().locate).toMatchObject({ nonce: 2, page: 5 });
  });

  it('编译列：带 page 的行有定位链接（无 page 的没有），且计数行保留', () => {
    renderPanel(
      <EventStreamPanel
        did={DID}
        compileEvents={[
          { seq: 13, type: 'preview_ready', blockId: null, page: null, at: null, data: { page: 1, asset: 'a' } },
          { seq: 15, type: 'preview_failed', blockId: 'P02-012', page: null, at: null, data: { message: '单块编译失败' } },
        ]}
        feed={makeEventFeed({ events: windowOf(1), runId: RUN_ID })}
      />,
    );
    const rows = document.querySelectorAll('[data-od-id="compile-timeline-row"]');
    expect(rows).toHaveLength(2);
    expect(rows[0].querySelector('[data-od-id="event-locate"]')).toBeNull();
    expect(rows[1].querySelector('[data-od-id="event-locate"]')).not.toBeNull();
    fireEvent.click(rows[1].querySelector('[data-od-id="event-locate"]') as HTMLElement);
    expect(uiStore.getState().locate).toMatchObject({ nonce: 1, page: 1 });
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

describe('编译时间线（右列，持久事件流窗口）', () => {
  it('渲染编译侧事件：最新在上，计数行给出块/页合计', () => {
    const compileEvents = [
      { seq: 11, type: 'translation_block_completed', blockId: 'P01-001', page: null, at: null, data: { index: 1, total: 187 } },
      { seq: 12, type: 'translation_block_completed', blockId: 'P01-002', page: null, at: null, data: { index: 2, total: 187 } },
      { seq: 13, type: 'preview_ready', blockId: null, page: null, at: null, data: { page: 1, asset: 'sha-1', complete: true } },
      { seq: 14, type: 'compile_float', blockId: 'P02-011', page: null, at: null, data: { paragraph_id: 'P02-011', kind: 'expand', page: 2 } },
      { seq: 15, type: 'preview_failed', blockId: 'P02-012', page: null, at: null, data: { message: '单块编译失败' } },
    ];
    render(
      wrapper({
        children: (
          <EventStreamPanel did={DID} compileEvents={compileEvents} feed={makeEventFeed({ events: windowOf(1), runId: RUN_ID })} />
        ),
      }),
    );
    const rows = document.querySelectorAll('[data-od-id="compile-timeline-row"]');
    expect(rows).toHaveLength(5);
    // 最新在上：第一行是最后到达的 preview_failed。
    expect(rows[0].getAttribute('data-type')).toBe('preview_failed');
    expect(rows[0].textContent).toContain('P02-012');
    expect(rows[2].textContent).toContain('第 1 页');
    expect(screen.getByText(/块 2 · 页 1/)).toBeInTheDocument();
  });

  it('没有编译事件时空态说明（不是错误）', () => {
    render(
      wrapper({
        children: <EventStreamPanel did={DID} compileEvents={[]} feed={makeEventFeed({ events: windowOf(1), runId: RUN_ID })} />,
      }),
    );
    expect(document.querySelector('[data-od-id="compile-timeline-empty"]')).not.toBeNull();
  });
});

/**
 * 两列样式统一（W06 收尾）：节点圆标、行容器与滚动容器都必须是**同一套**呈现，
 * 不再出现「左列 20px 描边圆 / 右列 16px」「右列逐行横线」这类分叉。
 * jsdom 不过 Tailwind 样式表，所以断言的是 className 本身（唯一拼写来源 = EventRow
 * 导出的 `TL_ROW_CLASS` / `TlNode`）。
 */
describe('双时间线：样式统一与独立滚动', () => {
  function renderBoth() {
    return renderPanel(
      <EventStreamPanel
        did={DID}
        compileEvents={[
          { seq: 11, type: 'translation_block_completed', blockId: 'P01-001', page: null, at: null, data: {} },
          { seq: 12, type: 'preview_failed', blockId: 'P02-012', page: null, at: null, data: { message: '单块编译失败' } },
        ]}
        feed={makeEventFeed({ events: windowOf(3), runId: RUN_ID })}
      />,
    );
  }

  it('两个滚动容器都在，且各自 overflow-auto（不靠外层裁切）', () => {
    renderBoth();
    const left = document.querySelector('[data-od-id="event-stream-scroll"]') as HTMLElement;
    const right = document.querySelector('[data-od-id="compile-timeline-scroll"]') as HTMLElement;
    expect(left).not.toBeNull();
    expect(right).not.toBeNull();
    for (const column of [left, right]) {
      expect(column.className).toContain('overflow-auto');
      // 高度必须由 flex 收缩约束（min-h-0 + flex-1），否则子节点会长到内容高度、滚动条出现不了。
      expect(column.className).toContain('min-h-0');
      expect(column.className).toContain('flex-1');
    }
    // 两列是兄弟滚动容器（互不嵌套）——滚一列不会带动另一列。
    expect(left.contains(right)).toBe(false);
    expect(right.contains(left)).toBe(false);
  });

  it('两列的行容器用同一份 TL_ROW_CLASS（无逐行横线），节点圆标尺寸完全一致', () => {
    renderBoth();
    const rows = [
      ...document.querySelectorAll('[data-od-id="event-row"]'),
      ...document.querySelectorAll('[data-od-id="compile-timeline-row"]'),
    ];
    expect(rows.length).toBeGreaterThan(2);
    for (const row of rows) {
      // 行几何必须逐字来自共享常量（行内再加语气文字色）。
      expect(row.className).toContain(TL_ROW_CLASS);
      // 稀疏横线：只留竖向连接线（before:），不再逐行 border-b。
      expect(row.className).not.toContain('border-b');
      expect(row.className).toContain('before:bg-hair');
    }

    const nodes = document.querySelectorAll('[data-od-id="tl-node"]');
    expect(nodes).toHaveLength(rows.length);
    for (const node of nodes) {
      // 8px 实心小圆：尺寸 class 只有一处拼写，两列不许各写一套。
      expect(node.className).toContain('h-2');
      expect(node.className).toContain('w-2');
      expect(node.className).toContain('rounded-full');
      expect(node.className).not.toMatch(/h-4|w-4|h-5|w-5|border/);
    }
  });
});
