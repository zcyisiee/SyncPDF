/**
 * 当前论文的事件归约与快照合并。
 * @vitest-environment node
 */
import { describe, expect, it } from 'vitest';
import type { DocSnapshot } from '@shared/library';
import type { EngineEvent, EventOf } from '@shared/protocol';
import { emptyOpenDoc, mergeSnapshot, reduceOpenDoc } from '@/store/library';

const paragraph = (id: string, html: string | null): EventOf<'paragraph'> => ({
  seq: 1,
  ts: 0,
  type: 'paragraph',
  paragraph_id: id,
  page: 1,
  status: html === null ? 'fallback' : 'typeset',
  boxes: [{ x0: 0, y0: 0, x1: 10, y1: 10 }],
  coord_system: 'pdf_user',
  translated_html: html,
  kind: 'text',
  source_text: 'src',
  source_bbox: { x0: 0, y0: 0, x1: 10, y1: 10 },
});

const runStarted: EngineEvent = {
  seq: 1,
  ts: 0,
  type: 'run_started',
  protocol_version: 1,
  engine_version: 'test',
  doc_id: 'd',
  pages: 1,
};

const snapshot: DocSnapshot = {
  layout: { 1: [{ kind: 'figure', inline: false, bbox: { x0: 0, y0: 0, x1: 5, y1: 5 } }] },
  paragraphs: [(({ seq: _s, ts: _t, type: _y, ...rest }) => rest)(paragraph('P01-001', '<p>旧</p>'))],
  issues: [],
};

describe('reduceOpenDoc', () => {
  it('段落事件按 ID 覆盖，page_ready / run_finished 递增修订号', () => {
    let open = emptyOpenDoc('d');
    open = reduceOpenDoc(open, paragraph('P01-001', '<p>一</p>'));
    open = reduceOpenDoc(open, paragraph('P01-001', '<p>二</p>'));
    open = reduceOpenDoc(open, { seq: 2, ts: 0, type: 'page_ready', page: 1, preview_path: null, revision: 1 });
    open = reduceOpenDoc(open, { seq: 3, ts: 0, type: 'run_finished', ok: true, elapsed_ms: 1 });
    expect(open.paragraphs['P01-001'].translated_html).toBe('<p>二</p>');
    expect(open.revision).toBe(2);
  });

  it('无关事件返回同一对象', () => {
    const open = emptyOpenDoc('d');
    expect(reduceOpenDoc(open, { seq: 1, ts: 0, type: 'progress', stage: 'translating', done: 1, total: 2 })).toBe(open);
  });
});

describe('mergeSnapshot', () => {
  it('加载期间到达的事件覆盖快照', () => {
    const open = reduceOpenDoc(emptyOpenDoc('d'), paragraph('P01-001', '<p>新</p>'));
    const merged = mergeSnapshot(open, snapshot);
    expect(merged.loading).toBe(false);
    expect(merged.paragraphs['P01-001'].translated_html).toBe('<p>新</p>');
    expect(merged.layout[1]).toHaveLength(1);
  });

  it('加载期间开始了新一轮 run：丢弃过时快照', () => {
    const open = reduceOpenDoc(emptyOpenDoc('d'), runStarted);
    const merged = mergeSnapshot(open, snapshot);
    expect(merged.paragraphs).toEqual({});
    expect(merged.layout).toEqual({});
    expect(merged.restarted).toBe(false);
  });
});
