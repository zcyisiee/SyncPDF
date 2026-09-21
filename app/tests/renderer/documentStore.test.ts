/**
 * documentStore reducer 单元测试（M2-07 验收）。
 */
import { describe, expect, it } from 'vitest';
import {
  createDocumentStore,
  pageNumberFromId,
  reduceEvent,
  initialDocumentState,
} from '../../src/renderer/src/store/documentStore';
import type { EngineEvent } from '../../src/shared/protocol';

function event(partial: Record<string, unknown>): EngineEvent {
  return partial as unknown as EngineEvent;
}

describe('reduceEvent', () => {
  it('run_started 重置快照', () => {
    const state = {
      ...initialDocumentState,
      lastSeq: 10,
      paragraphs: { old: {} } as unknown as typeof initialDocumentState.paragraphs,
      paragraphOrder: ['old'],
    };
    const patch = reduceEvent(
      state,
      event({
        seq: 11,
        ts: 1,
        type: 'run_started',
        protocol_version: 1,
        engine_version: 'e-1',
        doc_id: 'doc-1',
        pages: 12,
      }),
    );
    expect(patch).not.toBeNull();
    expect(patch?.docId).toBe('doc-1');
    expect(patch?.pageCount).toBe(12);
    expect(patch?.paragraphs).toEqual({});
    expect(patch?.paragraphOrder).toEqual([]);
    expect(patch?.engineVersion).toBe('e-1');
    expect(patch?.lastSeq).toBe(11);
  });

  it('seq 去重：重复 / 回退事件丢弃', () => {
    const state = { ...initialDocumentState, lastSeq: 5 };
    expect(reduceEvent(state, event({ seq: 5, ts: 1, type: 'cancel' } as never))).toBeNull();
    expect(reduceEvent(state, event({ seq: 4, ts: 1, type: 'cancel' } as never))).toBeNull();
    expect(reduceEvent(state, event({ seq: 6, ts: 1, type: 'stage_started', stage: 'translating' }))).not.toBeNull();
  });

  it('progress / stage 状态推进', () => {
    let state = { ...initialDocumentState, lastSeq: 0 };
    state = { ...state, ...reduceEvent(state, event({ seq: 1, ts: 1, type: 'stage_started', stage: 'translating' }))! };
    expect(state.stage).toBe('translating');
    state = { ...state, ...reduceEvent(state, event({ seq: 2, ts: 1, type: 'progress', stage: 'translating', done: 42, total: 169 }))! };
    expect(state.progress).toEqual({ stage: 'translating', done: 42, total: 169 });
    state = { ...state, ...reduceEvent(state, event({ seq: 3, ts: 1, type: 'stage_finished', stage: 'translating', elapsed_ms: 1200 }))! };
    expect(state.stageElapsedMs.translating).toBe(1200);
  });

  it('paragraph 事件累积段落记录，保持顺序', () => {
    let state = { ...initialDocumentState, lastSeq: 0 };
    const first: EngineEvent = {
      seq: 1,
      ts: 1,
      type: 'paragraph',
      paragraph_id: 'P01-001',
      status: 'translated',
      boxes: [[1, 2, 3, 4]],
      coord_system: 'pdf_native',
      translated_html: '<p id="P01-001">a</p>',
    };
    const second: EngineEvent = {
      seq: 2,
      ts: 1,
      type: 'paragraph',
      paragraph_id: 'P02-001',
      status: 'fallback',
      boxes: null,
      coord_system: 'pdf_native',
      translated_html: '',
    };
    state = { ...state, ...reduceEvent(state, first)! };
    state = { ...state, ...reduceEvent(state, second)! };
    expect(state.paragraphOrder).toEqual(['P01-001', 'P02-001']);
    expect(state.paragraphs['P01-001']).toMatchObject({
      page: 1,
      status: 'translated',
      boxes: [[1, 2, 3, 4]],
    });
    expect(state.paragraphs['P02-001'].boxes).toBeNull();
    expect(state.paragraphs['P02-001'].page).toBe(2);
    // 同 id 重复不改变顺序（重排版场景）
    const again: EngineEvent = { ...first, seq: 3, status: 'typeset' };
    state = { ...state, ...reduceEvent(state, again)! };
    expect(state.paragraphOrder).toEqual(['P01-001', 'P02-001']);
    expect(state.paragraphs['P01-001'].status).toBe('typeset');
  });

  it('page_ready 覆盖同页记录', () => {
    let state = { ...initialDocumentState, lastSeq: 0 };
    state = { ...state, ...reduceEvent(state, event({ seq: 1, ts: 1, type: 'page_ready', page: 3, preview_path: '/tmp/a.pdf' }))! };
    expect(state.pagesReady[3].previewPath).toBe('/tmp/a.pdf');
    state = { ...state, ...reduceEvent(state, event({ seq: 2, ts: 1, type: 'page_ready', page: 3, preview_path: '/tmp/b.pdf' }))! };
    expect(state.pagesReady[3].previewPath).toBe('/tmp/b.pdf');
    expect(Object.keys(state.pagesReady)).toEqual(['3']);
  });

  it('issue 追加、document_finished / run_finished 落定', () => {
    let state: typeof initialDocumentState = { ...initialDocumentState, lastSeq: 0, runState: 'running' };
    state = { ...state, ...reduceEvent(state, event({ seq: 1, ts: 1, type: 'issue', severity: 'error', code: 'x', paragraph_id: 'P01-001', message: 'm' }))! };
    expect(state.issues).toHaveLength(1);
    expect(state.issues[0]).toMatchObject({ code: 'x', paragraphId: 'P01-001' });
    state = { ...state, ...reduceEvent(state, event({ seq: 2, ts: 1, type: 'document_finished', output: '/tmp/o.pdf', stats: { fonts: 2, expansion_ratio: 1.2, fallback_count: 1 } }))! };
    expect(state.output).toBe('/tmp/o.pdf');
    expect(state.stats?.fonts).toBe(2);
    state = { ...state, ...reduceEvent(state, event({ seq: 3, ts: 1, type: 'run_finished', ok: true, elapsed_ms: 5 }))! };
    expect(state.runState).toBe('finished');
  });

  it('error 事件：fatal → runState failed', () => {
    let state: typeof initialDocumentState = { ...initialDocumentState, lastSeq: 0, runState: 'running' };
    state = { ...state, ...reduceEvent(state, event({ seq: 1, ts: 1, type: 'error', fatal: false, code: 'conflict', message: 'm' }))! };
    expect(state.runState).toBe('running'); // 非致命不终止
    expect(state.lastError?.code).toBe('conflict');
    state = { ...state, ...reduceEvent(state, event({ seq: 2, ts: 1, type: 'error', fatal: true, code: 'encrypted', message: 'm' }))! };
    expect(state.runState).toBe('failed');
    expect(state.lastError?.fatal).toBe(true);
  });
});

describe('createDocumentStore', () => {
  it('applyEvent 全链路（fake-sidecar 事件序列）', () => {
    const store = createDocumentStore();
    const events: EngineEvent[] = [
      event({ seq: 1, ts: 1, type: 'run_started', protocol_version: 1, engine_version: 'fake', doc_id: 'd', pages: 2 }),
      event({ seq: 2, ts: 1, type: 'stage_started', stage: 'translating' }),
      event({ seq: 3, ts: 1, type: 'progress', stage: 'translating', done: 0, total: 6 }),
      event({ seq: 4, ts: 1, type: 'paragraph', paragraph_id: 'P01-001', status: 'translated', boxes: null, coord_system: 'pdf_native', translated_html: 'x' }),
      event({ seq: 5, ts: 1, type: 'page_ready', page: 1 }),
      event({ seq: 6, ts: 1, type: 'stage_finished', stage: 'translating', elapsed_ms: 10 }),
      event({ seq: 7, ts: 1, type: 'document_finished', output: '/tmp/o.pdf', stats: { fonts: 1, expansion_ratio: 1, fallback_count: 0 } }),
      event({ seq: 8, ts: 1, type: 'run_finished', ok: true, elapsed_ms: 20 }),
    ];
    for (const e of events) store.getState().applyEvent(e);
    const snapshot = store.getState();
    expect(snapshot.docId).toBe('d');
    expect(snapshot.runState).toBe('finished');
    expect(snapshot.paragraphOrder).toEqual(['P01-001']);
    expect(snapshot.lastSeq).toBe(8);
    // 重复事件被去重
    store.getState().applyEvent(events[events.length - 1]);
    expect(store.getState().lastSeq).toBe(8);

    store.getState().reset(null);
    expect(store.getState().docId).toBeNull();
    expect(store.getState().paragraphOrder).toEqual([]);
    expect(store.getState().lastSeq).toBe(0);
  });
});

describe('pageNumberFromId', () => {
  it('P05-002 → 5；非法 → null', () => {
    expect(pageNumberFromId('P05-002')).toBe(5);
    expect(pageNumberFromId('P12-010')).toBe(12);
    expect(pageNumberFromId('X01-001')).toBeNull();
    expect(pageNumberFromId('P-001')).toBeNull();
    expect(pageNumberFromId('')).toBeNull();
  });
});
