/**
 * `lib/events.ts` 的纯函数：SSE id / URL、live 判定（brief 冻结规则的 4 个断言）、
 * kind 分组、data 摘要、窗口合并。
 */
import { describe, expect, it } from 'vitest';

import {
  dataSummary,
  eventLevel,
  eventSourceUrl,
  EVENTS_WINDOW_SIZE,
  isRunLive,
  kindGroup,
  mergeEarlierWindow,
  mergeTailWindow,
  parseSseId,
  parseStreamEvent,
  runEventsFromPage,
  toRunEvent,
  type RunEvent,
} from '../src/lib/events';

function event(seq: number, kind: string, stage = 'translate'): RunEvent {
  return { seq, at: `2026-09-16T13:28:${String(seq).padStart(2, '0')}.000Z`, stage, kind, data: {} };
}

describe('parseSseId', () => {
  it('`<run_id>:<seq>` → 结构化（run_id 里有 `-` 没有 `:`，按最后一个冒号切）', () => {
    expect(parseSseId('20260917T125056Z-0001e6:3758')).toEqual({
      runId: '20260917T125056Z-0001e6',
      seq: 3758,
    });
  });

  it('不合法 → null（空 / 没冒号 / seq 非数字 / 负数 / 非字符串）', () => {
    expect(parseSseId('')).toBeNull();
    expect(parseSseId('20260917T125056Z-0001e6')).toBeNull();
    expect(parseSseId(':12')).toBeNull();
    expect(parseSseId('run:abc')).toBeNull();
    expect(parseSseId('run:')).toBeNull();
    expect(parseSseId('run:-3')).toBeNull();
    expect(parseSseId(null)).toBeNull();
    expect(parseSseId(undefined)).toBeNull();
  });
});

describe('eventSourceUrl', () => {
  it('同源相对路径 + 固定 run_id + after_seq（换 run 必须换 run_id）', () => {
    expect(eventSourceUrl('ccs3764-dyn', '20260917T125056Z-0001e6', 3758)).toBe(
      '/api/v1/documents/ccs3764-dyn/events/stream?run_id=20260917T125056Z-0001e6&after_seq=3758',
    );
  });

  it('did 要转义、after_seq 负数归零', () => {
    expect(eventSourceUrl('a b', 'r1', -5)).toBe(
      '/api/v1/documents/a%20b/events/stream?run_id=r1&after_seq=0',
    );
  });
});

describe('isRunLive（brief 冻结规则：末条不是 stage_finished 或 stage != report → live）', () => {
  it('最后一条是 stage_finished + report → false（run 收尾了）', () => {
    expect(isRunLive([event(1, 'stage_started', 'report'), event(2, 'stage_finished', 'report')])).toBe(
      false,
    );
  });

  it('最后一条是未配对的 stage_started → true', () => {
    expect(isRunLive([event(1, 'stage_started', 'parse'), event(2, 'stage_started', 'translate')])).toBe(
      true,
    );
  });

  it('最后一条是运行中间的事件（stage_finished 但不是 report）→ true', () => {
    expect(isRunLive([event(1, 'stage_finished', 'check'), event(2, 'call_started', 'check')])).toBe(
      true,
    );
    expect(isRunLive([event(1, 'stage_finished', 'check')])).toBe(true);
  });

  it('空数组 → false（没有事件不等于在跑）', () => {
    expect(isRunLive([])).toBe(false);
  });
});

describe('kindGroup', () => {
  it('阶段/调用/缓存/候选/编译各自归组，其余进 other', () => {
    expect(kindGroup('stage_started')).toBe('stage');
    expect(kindGroup('stage_finished')).toBe('stage');
    expect(kindGroup('stage_error')).toBe('stage');
    expect(kindGroup('call_started')).toBe('call');
    expect(kindGroup('call_finished')).toBe('call');
    expect(kindGroup('cache_hit')).toBe('cache');
    expect(kindGroup('cache_miss')).toBe('cache');
    expect(kindGroup('candidate_evaluated')).toBe('candidate');
    expect(kindGroup('canonical_writeback')).toBe('candidate');
    expect(kindGroup('compile_fallback')).toBe('compile');
    expect(kindGroup('artifact_bundle')).toBe('compile');
    expect(kindGroup('inline_math')).toBe('other');
    expect(kindGroup('未来新增的 kind')).toBe('other');
  });
});

describe('eventLevel（api.md §4：事件没有 level，由 kind + data 推导）', () => {
  it('data.status/error_code/returncode 决定 err，cache_miss 等是 warn，其余 info', () => {
    expect(eventLevel({ ...event(1, 'call_finished'), data: { status: 'error' } })).toBe('err');
    expect(eventLevel({ ...event(1, 'call_finished'), data: { returncode: 2 } })).toBe('err');
    expect(eventLevel({ ...event(1, 'call_finished'), data: { error_code: 'timeout' } })).toBe('err');
    expect(eventLevel(event(1, 'stage_error', 'check'))).toBe('err');
    expect(eventLevel(event(1, 'cache_miss'))).toBe('warn');
    expect(eventLevel(event(1, 'call_finished'))).toBe('info');
  });
});

describe('dataSummary', () => {
  it('压平最多 3 个键值对', () => {
    expect(dataSummary({ status: 'ok', replayed: true })).toBe('status=ok replayed=true');
    expect(dataSummary({ a: 1, b: 2, c: 3, d: 4 })).toBe('a=1 b=2 c=3');
  });

  it('嵌套对象/数组只报形状，不展开', () => {
    expect(dataSummary({ box: [1, 2, 3, 4], meta: { x: 1 } })).toBe('box=[4] meta={…}');
    expect(dataSummary({ note: null })).toBe('note=null');
  });

  it('超过 80 字符截断并加省略号', () => {
    const summary = dataSummary({ note: 'x'.repeat(200) });
    expect(summary.length).toBe(80);
    expect(summary.endsWith('…')).toBe(true);
  });

  it('空 data → 空串（行上只显示 kind 标签）', () => {
    expect(dataSummary({})).toBe('');
  });
});

describe('事件归一化', () => {
  it('toRunEvent 丢掉不合法形状（没有整数 seq / 非对象）', () => {
    expect(toRunEvent(null)).toBeNull();
    expect(toRunEvent({ seq: '1' })).toBeNull();
    expect(toRunEvent({ seq: 1.5 })).toBeNull();
    expect(toRunEvent({ seq: 3 })).toEqual({ seq: 3, at: '', stage: '', kind: '', data: {} });
  });

  it('runEventsFromPage 只留合法行', () => {
    const events = runEventsFromPage({
      run_id: 'r1',
      events: [{ seq: 1, at: 'x', stage: 'parse', kind: 'stage_started', data: { a: 1 } }, { nope: 1 }],
      next_after_seq: 1,
      has_more: false,
    });
    expect(events).toHaveLength(1);
    expect(events[0].seq).toBe(1);
  });

  it('parseStreamEvent：正常 JSON → 事件，坏 JSON → null（不炸流）', () => {
    expect(parseStreamEvent('{"seq":2,"at":"x","stage":"parse","kind":"stage_finished","data":{}}')).toEqual(
      expect.objectContaining({ seq: 2, kind: 'stage_finished' }),
    );
    expect(parseStreamEvent('not json')).toBeNull();
    expect(parseStreamEvent('{"seq":"2"}')).toBeNull();
  });
});

describe('窗口合并', () => {
  it('尾部合并按 seq 去重、升序、只留最新 maxSize 条（重连会重发已收到的事件）', () => {
    const merged = mergeTailWindow([event(1, 'stage_started', 'parse'), event(2, 'stage_finished', 'parse')], [
      event(2, 'stage_finished', 'parse'),
      event(3, 'stage_started', 'translate'),
    ], 2);
    expect(merged.map((item) => item.seq)).toEqual([2, 3]);
  });

  it('没有新增时返回原引用（不触发无谓重渲染）', () => {
    const previous = [event(1, 'stage_started', 'parse')];
    expect(mergeTailWindow(previous, [], 200)).toBe(previous);
  });

  it('载入更早：并入更早的事件并放宽上限（默认窗口不被裁掉）', () => {
    const tail = Array.from({ length: EVENTS_WINDOW_SIZE }, (_unused, index) => event(index + 801, 'call_started'));
    const earlier = Array.from({ length: 500 }, (_unused, index) => event(index + 301, 'call_finished'));
    const merged = mergeEarlierWindow(tail, earlier, EVENTS_WINDOW_SIZE);
    expect(merged).toHaveLength(EVENTS_WINDOW_SIZE + 500);
    expect(merged[0].seq).toBe(301);
    expect(merged[merged.length - 1].seq).toBe(1000);
  });
});
