/**
 * 当前论文的事件归约与快照合并。
 * @vitest-environment node
 */
import { describe, expect, it } from 'vitest';
import type { DocSnapshot } from '@shared/library';
import type { EngineEvent, EventOf } from '@shared/protocol';
import { emptyOpenDoc, mergeSnapshot, reduceOpenDoc, relayoutPendingPages } from '@/store/library';

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
  edits: [{ paragraph_id: 'P01-001', manual: true, style: {} }],
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

  it('page_ready 只记本页修订号；run_finished 只重载文件不标脏页', () => {
    let open = emptyOpenDoc('d');
    const ready = (page: number): EngineEvent => ({ seq: 1, ts: 0, type: 'page_ready', page, preview_path: null, revision: 0 });
    open = reduceOpenDoc(open, ready(1));
    open = reduceOpenDoc(open, ready(2));
    open = reduceOpenDoc(open, { seq: 3, ts: 0, type: 'run_finished', ok: true, elapsed_ms: 1 });
    expect(open.pageRevisions).toEqual({ 1: 1, 2: 2 });
    expect(open.revision).toBe(3);
  });

  it('page_reopened：只清该页问题，不像 run_started 那样清空段落与版面', () => {
    const issue = (page: number): EngineEvent => ({
      seq: 1, ts: 0, type: 'issue', severity: 'warning', code: 'c', paragraph_id: null, page, message: String(page),
    });
    let open = reduceOpenDoc(emptyOpenDoc('d'), paragraph('P01-001', '<p>一</p>'));
    open = reduceOpenDoc(open, issue(1));
    open = reduceOpenDoc(open, issue(2));
    open = reduceOpenDoc(open, { seq: 2, ts: 0, type: 'page_reopened', page: 1 });
    expect(open.issues.map((i) => i.page)).toEqual([2]);
    expect(Object.keys(open.paragraphs)).toEqual(['P01-001']);
  });

  it('待动态编译页：只算排版溢出且仍回退的段；翻译校验回退与已被重排救回的段不算', () => {
    const overflow = (id: string): EngineEvent => ({
      seq: 1, ts: 0, type: 'issue', severity: 'warning', code: 'typeset_overflow', paragraph_id: id, page: 1, message: 'm',
    });
    const onPage = (id: string, page: number, html: string | null) => ({ ...paragraph(id, html), page });
    let open = emptyOpenDoc('d');
    open = reduceOpenDoc(open, onPage('P01-001', 1, null));
    open = reduceOpenDoc(open, overflow('P01-001'));
    open = reduceOpenDoc(open, onPage('P02-001', 2, null));
    open = reduceOpenDoc(open, { ...overflow('P02-001'), code: 'translate_fallback' } as EngineEvent);
    open = reduceOpenDoc(open, onPage('P03-001', 3, null));
    open = reduceOpenDoc(open, overflow('P03-001'));
    open = reduceOpenDoc(open, onPage('P03-001', 3, '<p>救回</p>'));
    expect([...relayoutPendingPages(open)]).toEqual([1]);
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
    expect(merged.edits?.['P01-001']?.manual).toBe(true);
  });

  it('单块覆盖：本次会话的 block_edits 覆盖快照，新一轮 run_started 不清空', () => {
    let open = reduceOpenDoc(emptyOpenDoc('d'), {
      seq: 1,
      ts: 0,
      type: 'block_edits',
      edits: [{ paragraph_id: 'P01-002', manual: false, style: { font_scale: 0.9 } }],
    });
    open = mergeSnapshot(open, snapshot);
    expect(Object.keys(open.edits ?? {})).toEqual(['P01-002']);
    open = reduceOpenDoc(open, runStarted);
    expect(open.edits?.['P01-002']?.style.font_scale).toBe(0.9);
    open = reduceOpenDoc(open, { seq: 2, ts: 0, type: 'block_edits', edits: [] });
    expect(open.edits).toEqual({});
  });

  it('加载期间开始了新一轮 run：丢弃过时快照', () => {
    const open = reduceOpenDoc(emptyOpenDoc('d'), runStarted);
    const merged = mergeSnapshot(open, snapshot);
    expect(merged.paragraphs).toEqual({});
    expect(merged.layout).toEqual({});
    expect(merged.restarted).toBe(false);
  });
});
