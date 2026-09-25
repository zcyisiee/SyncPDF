// 一条任务的进度：job 视图 + SSE 事件归并成界面状态。
import { useCallback, useEffect, useReducer, useRef, useState } from 'react';

import { ACTIVE, api, eventsUrl, type ApiError, type Box, type Job, type JobStatus, type Queue } from './api';
import { clock } from './format';

export interface Item {
  key: number;
  kind: 'm' | 'p' | 'box warn' | 'box err' | 'box hit';
  time: string;
  text: string;
  sub?: string;
}

export interface PageState {
  rev: string;
  boxes: Box[];
  /** 非 0：这一页刚在眼前完成，播一次变中文动画（值变化即重播）。 */
  anim: number;
}

export interface RunState {
  status: JobStatus | null;
  step: number;
  fail: boolean;
  items: Item[];
  pages: Record<number, PageState>;
  done: number;
  queue: Queue | null;
  /** 非 0：缓存命中，所有页一起变中文。 */
  flipAll: number;
  /** 最近一次实时完成的页（预览跟随滚动）。 */
  follow: { page: number; token: number } | null;
}

type Payload = Record<string, unknown> & { seq?: number; ts: number; live: boolean };
type Action = { type: 'reset' } | { type: string; data: Payload };

const initial: RunState = {
  status: null,
  step: -1,
  fail: false,
  items: [],
  pages: {},
  done: 0,
  queue: null,
  flipAll: 0,
  follow: null,
};

const BOXES: Record<string, Item['kind']> = { warn: 'box warn', error: 'box err', hit: 'box hit' };

function reduce(state: RunState, action: Action): RunState {
  if (action.type === 'reset') return initial;
  const { data } = action as { type: string; data: Payload };
  const key = data.seq ?? state.items.length;
  const item = (kind: Item['kind'], text: unknown, sub?: unknown): Item[] => [
    ...state.items,
    { key, kind, time: clock(data.ts), text: String(text), sub: sub ? String(sub) : undefined },
  ];
  switch (action.type) {
    case 'status':
      return { ...state, status: data.status as JobStatus, queue: data.status === 'queued' ? state.queue : null };
    case 'step':
      return { ...state, step: Number(data.step), fail: Boolean(data.fail) };
    case 'queue':
      return { ...state, queue: (data as unknown as Queue | null) ?? null };
    case 'milestone':
      return { ...state, items: item('m', data.text) };
    case 'warn':
    case 'error':
    case 'hit':
      return {
        ...state,
        items: item(BOXES[action.type], data.text, data.sub),
        flipAll: action.type === 'hit' && data.live ? key : state.flipAll,
      };
    case 'page': {
      const page = Number(data.page);
      const known = state.pages[page];
      const animate = data.live && !known;
      return {
        ...state,
        done: Number(data.done),
        pages: {
          ...state.pages,
          [page]: { rev: String(data.rev), boxes: (data.boxes as Box[]) ?? [], anim: animate ? key : known?.anim ?? 0 },
        },
        items: data.text ? item(data.first ? 'm' : 'p', data.text) : state.items,
        follow: animate ? { page, token: key } : state.follow,
      };
    }
    default:
      return state;
  }
}

const KINDS = ['status', 'step', 'queue', 'milestone', 'warn', 'error', 'hit', 'page'];

/** 打开一条任务：先取 job 视图，再用 SSE 回放 + 追实时；结束后刷新视图拿最终统计。 */
export function useJob(id: string | null, onFinished: () => void) {
  const [job, setJob] = useState<Job | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [run, dispatch] = useReducer(reduce, initial);
  const finished = useRef(onFinished);
  finished.current = onFinished;

  const refresh = useCallback(async () => {
    if (!id) return;
    try {
      setJob(await api.job(id));
    } catch (e) {
      setError(e as ApiError);
    }
  }, [id]);

  useEffect(() => {
    setJob(null);
    setError(null);
    dispatch({ type: 'reset' });
    if (!id) return;
    let closed = false;
    void refresh();
    const opened = Date.now();
    const source = new EventSource(eventsUrl(id));
    for (const kind of KINDS) {
      source.addEventListener(kind, (event) => {
        const raw = JSON.parse((event as MessageEvent<string>).data) as Record<string, unknown> | null;
        const ts = typeof raw?.ts === 'number' ? raw.ts : Date.now() / 1000;
        // 打开页面时回放的历史不播动画；打开后才到的、或打开前 2 秒内发生的（上传后秒命中缓存）才播。
        // 前一条不看服务器时间，服务器时钟有偏差也不会吞掉实时动画。
        const live = Date.now() - opened > 800 || ts * 1000 > opened - 2000;
        const seq = (event as MessageEvent).lastEventId ? Number((event as MessageEvent).lastEventId) : undefined;
        dispatch({ type: kind, data: { ...(raw ?? {}), ts, live, seq } as Payload });
        if (kind === 'status' && raw && !ACTIVE.includes(raw.status as JobStatus)) void refresh();
      });
    }
    source.addEventListener('end', () => {
      closed = true;
      source.close();
      void refresh();
      finished.current();
    });
    source.onerror = () => {
      // 连接断了浏览器会带 Last-Event-ID 自动重连；被服务端拒绝（404/401）则彻底关闭
      if (!closed && source.readyState === EventSource.CLOSED) void refresh();
    };
    return () => {
      closed = true;
      source.close();
    };
  }, [id, refresh]);

  const status = run.status ?? job?.status ?? null;
  return { job, error, run, status, refresh };
}
