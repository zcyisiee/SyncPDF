/**
 * 原生 `EventSource` 生命周期（**禁库**，也不用 fetch-stream）。浏览器原生实现只支持
 * GET + 自动重连，而自动重连正好会带上 `Last-Event-ID` 头（api.md §1.4 的续传口径），
 * 所以断线重连不需要自己写 backoff。
 *
 * 三条必须知道的实现约束：
 * 1. `event: <kind>` 是**命名分发**，`onmessage` 只收默认类型（服务端 kind 缺失时才发
 *    `message`）——所以要为每个 kind 单独 `addEventListener`。清单来自
 *    `humanize.EVENT_KINDS`（本仓库归档里真实出现过的 kind + 虚拟 kind `job_update`），
 *    新 kind 必须补表，否则收不到。`job_update`（W14）单独走 `onJobUpdate`：它是 job 状态
 *    通知，不是归档里的事件（没有 `seq`），**不进事件窗口**。
 * 2. 非 200（例如 404 `events_unavailable`）会让 EventSource 直接进 CLOSED 并触发 `error`：
 *    调用方**先**用分页接口首拉一次（`useEventWindow`），没有 run 归档就不建 EventSource。
 * 3. 心跳 `: ping` 是注释行，浏览器不会派发任何事件——不需要代码处理。
 */
import { useEffect, useRef, useState } from 'react';

import {
  JOB_UPDATE_KIND,
  eventSourceUrl,
  parseJobUpdate,
  parseStreamEvent,
  type JobUpdate,
  type RunEvent,
} from '../../lib/events';

/** `EventSource.readyState` 的三个取值（常量在本文件冻结，不依赖 DOM 全局）。 */
export const EVENT_SOURCE_CONNECTING = 0;
export const EVENT_SOURCE_OPEN = 1;
export const EVENT_SOURCE_CLOSED = 2;

export type SseStatus =
  | 'idle'
  /** 已建连接，还没 open。 */
  | 'connecting'
  /** 连接正常（服务端在推或发心跳）。 */
  | 'open'
  /** `error` + readyState=CONNECTING：浏览器正在自动重连。 */
  | 'reconnecting'
  /** `error` + readyState=CLOSED：不会自己恢复了（例如 404 / 跨域被拒）。 */
  | 'closed'
  /** 环境里没有 EventSource（jsdom 测试、老浏览器）。 */
  | 'unsupported';

/** EventSource 的最小接口（测试注入 stub 用；jsdom 没有原生实现）。 */
export interface EventSourceLike {
  readonly readyState: number;
  addEventListener(type: string, listener: (event: MessageEvent<string>) => void): void;
  close(): void;
}

export type EventSourceFactory = (url: string) => EventSourceLike;

function defaultEventSourceFactory(url: string): EventSourceLike {
  if (typeof EventSource === 'undefined') throw new Error('EventSource 不可用');
  // 原生 EventSource 的 addEventListener 签名与本接口兼容（这里显式收窄，避免把 DOM 类型漏进业务层）
  return new EventSource(url) as unknown as EventSourceLike;
}

export interface UseEventStreamOptions {
  did: string | null;
  /** 固定订阅哪个 run（换 run 必须换它，否则续传游标指向另一个 run）。 */
  runId: string | null;
  /** SSE 起点 = 首拉窗口扫过的最大 seq；null = 首拉还没完成，不建连接。 */
  afterSeq: number | null;
  /** 要监听的 kind（`EVENT_KINDS`；见文件头注释 1）。 */
  kinds: readonly string[];
  /** 每收到一条事件回调一次（父级负责去重/裁剪窗口）。 */
  onEvents: (events: RunEvent[]) => void;
  /**
   * 每收到一条 `job_update` 回调一次（W14：虚拟 kind，不会进 `onEvents`）。
   * 不给就只当没这个 kind（照样注册监听，丢在解析那一步）。
   */
  onJobUpdate?: (update: JobUpdate) => void;
  /** 测试注入 stub；生产用原生 EventSource。 */
  createEventSource?: EventSourceFactory;
}

export function useEventStream(options: UseEventStreamOptions): SseStatus {
  const { did, runId, afterSeq, kinds, onEvents, onJobUpdate, createEventSource } =
    options;
  // 状态带 key：连接参数一变，导出值立刻回到 connecting/idle（不需要在 effect 里同步 setState）。
  const [state, setState] = useState<{ key: string; status: SseStatus }>({
    key: '',
    status: 'connecting',
  });
  const handlerRef = useRef(onEvents);
  const jobUpdateRef = useRef(onJobUpdate);
  const kindsKey = kinds.join('\u0000');
  const subscriptionKey = `${did ?? ''}\u0000${runId ?? ''}\u0000${afterSeq ?? ''}`;
  const active = did !== null && runId !== null && afterSeq !== null;
  // 环境里没 EventSource（jsdom 单测，没注入 stub）→ 直接报 unsupported，不在 effect 里改状态。
  const canConnect = createEventSource !== undefined || typeof EventSource !== 'undefined';

  // 回调放 ref：事件到达时用最新的 handler，但不把它塞进 effect 依赖（否则每次渲染都重连）。
  useEffect(() => {
    handlerRef.current = onEvents;
  }, [onEvents]);

  useEffect(() => {
    jobUpdateRef.current = onJobUpdate;
  }, [onJobUpdate]);

  useEffect(() => {
    if (!active || !canConnect || did === null || runId === null || afterSeq === null) return;
    const factory = createEventSource ?? defaultEventSourceFactory;
    let source: EventSourceLike;
    try {
      source = factory(eventSourceUrl(did, runId, afterSeq));
    } catch {
      // 工厂抛错（stub 行为异常）：不建连接，状态停在 connecting，不让异常冒到渲染层。
      return;
    }

    const handleMessage = (message: MessageEvent<string>) => {
      const parsed = parseStreamEvent(message.data);
      if (parsed !== null) handlerRef.current([parsed]);
    };

    // job_update 走自己的分支：它不是归档里的 run 事件（没有 seq），不进事件窗口。
    const handleJobUpdate = (message: MessageEvent<string>) => {
      const update = parseJobUpdate(message.data);
      if (update !== null) jobUpdateRef.current?.(update);
    };

    source.addEventListener('open', () => setState({ key: subscriptionKey, status: 'open' }));
    source.addEventListener('message', handleMessage);
    if (kindsKey !== '') {
      for (const kind of kindsKey.split('\u0000')) {
        source.addEventListener(kind, kind === JOB_UPDATE_KIND ? handleJobUpdate : handleMessage);
      }
    }
    source.addEventListener('error', () =>
      setState({
        key: subscriptionKey,
        status:
          source.readyState === EVENT_SOURCE_CLOSED ? 'closed' : 'reconnecting',
      }),
    );

    return () => source.close();
  }, [active, canConnect, did, runId, afterSeq, subscriptionKey, kindsKey, createEventSource]);

  if (!active) return 'idle';
  if (!canConnect) return 'unsupported';
  return state.key === subscriptionKey ? state.status : 'connecting';
}
