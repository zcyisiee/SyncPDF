/**
 * protocol.ts 守卫：与 Rust syncpdf-protocol 同形的事件 / 请求才能通过。
 */
import { describe, expect, it } from 'vitest';
import { isEngineEvent, isRequest, type EngineEvent, type EventOf, type Request } from '../src/shared/protocol';
import { configureRequest, runRequest } from './support/requests';

const rect = { x0: 1, y0: 2, x1: 3, y1: 4 };
const paragraph: EventOf<'paragraph'> = {
  seq: 5,
  ts: 1,
  type: 'paragraph',
  paragraph_id: 'P01-001',
  page: 1,
  status: 'typeset',
  boxes: [rect],
  coord_system: 'pdf_user',
  translated_html: '<p>x</p>',
  kind: 'text',
  source_text: 'x',
};

describe('isEngineEvent', () => {
  it('接受每种合法事件', () => {
    const valid: EngineEvent[] = [
      { seq: 1, ts: 1.5, type: 'run_started', protocol_version: 1, engine_version: '0.1', doc_id: 'd', pages: 12 },
      { seq: 2, ts: 1, type: 'stage_started', stage: 'translating' },
      { seq: 3, ts: 1, type: 'stage_finished', stage: 'typesetting', elapsed_ms: 42 },
      { seq: 4, ts: 1, type: 'progress', stage: 'translating', done: 42, total: 169 },
      { seq: 5, ts: 1, type: 'layout', page: 1, regions: [{ kind: 'formula', inline: true, bbox: rect }] },
      { seq: 6, ts: 1, type: 'doc_meta', title: 'T', authors: null },
      paragraph,
      { ...paragraph, seq: 7, status: 'fallback', boxes: null, translated_html: null, coord_system: 'image_top_left' },
      { ...paragraph, seq: 8, status: 'pending', boxes: [] },
      { seq: 9, ts: 1, type: 'page_ready', page: 3, preview_path: null, revision: 2 },
      { seq: 10, ts: 1, type: 'issue', severity: 'warning', code: 'fallback', paragraph_id: 'P01-001', page: 1, message: 'm' },
      { seq: 11, ts: 1, type: 'issue', severity: 'info', code: 'x', paragraph_id: null, page: null, message: 'm' },
      { seq: 12, ts: 1, type: 'document_finished', output: '/o.pdf', stats: { fonts: 1, expansion_ratio: 1.1, fallbacks: 0 } },
      { seq: 13, ts: 1, type: 'run_finished', ok: true, elapsed_ms: 100 },
      { seq: 14, ts: 1, type: 'error', fatal: true, code: 'conflict', message: 'm' },
    ];
    for (const event of valid) {
      expect(isEngineEvent(event), JSON.stringify(event)).toBe(true);
    }
  });

  it('拒绝缺公共字段 / seq 非自然数 / ts 非数', () => {
    const progress = { type: 'progress', stage: 'translating', done: 1, total: 2 };
    expect(isEngineEvent(null)).toBe(false);
    expect(isEngineEvent(progress)).toBe(false);
    expect(isEngineEvent({ ...progress, seq: 1.5, ts: 1 })).toBe(false);
    expect(isEngineEvent({ ...progress, seq: -1, ts: 1 })).toBe(false);
    expect(isEngineEvent({ ...progress, seq: 1, ts: 'x' })).toBe(false);
  });

  it('拒绝未知 type 与旧协议形状', () => {
    expect(isEngineEvent({ seq: 1, ts: 1, type: 'nonsense' })).toBe(false);
    expect(isEngineEvent({ seq: 1, ts: 1, type: 'stage_started', stage: 'cooking' })).toBe(false);
    // 旧格式：tuple 框、pdf_native、缺 kind/source_text、page_ready 缺 revision、fallback_count
    expect(isEngineEvent({ ...paragraph, boxes: [[1, 2, 3, 4]] })).toBe(false);
    expect(isEngineEvent({ ...paragraph, coord_system: 'pdf_native' })).toBe(false);
    expect(isEngineEvent({ ...paragraph, kind: 'figure_caption_x' })).toBe(false);
    const { source_text: _drop, ...noSource } = paragraph;
    expect(isEngineEvent(noSource)).toBe(false);
    expect(isEngineEvent({ seq: 1, ts: 1, type: 'page_ready', page: 1, preview_path: null })).toBe(false);
    expect(
      isEngineEvent({ seq: 1, ts: 1, type: 'document_finished', output: 'o', stats: { fonts: 1, expansion_ratio: 1, fallback_count: 0 } }),
    ).toBe(false);
    expect(isEngineEvent({ seq: 1, ts: 1, type: 'layout', page: 1, regions: [{ kind: 'text', bbox: rect }] })).toBe(false);
    expect(isEngineEvent({ seq: 1, ts: 1, type: 'error', code: 'x', message: 'm' })).toBe(false);
  });
});

describe('isRequest', () => {
  it('接受每种合法请求', () => {
    const requests: Request[] = [
      configureRequest(),
      configureRequest({ provider: 'http', base_url: 'https://x/v1', api_key: 'k', translator: { kind: 'http' } }),
      configureRequest({ provider: 'pi', translator: { kind: 'pi', program: 'pi', model: 'm', thinking: 'low' } }),
      runRequest('d'),
      runRequest('d', { pages: [0, 2], terminology: '/t.json', mode: 'bilingual' }),
      { type: 'retranslate', doc_id: 'd', paragraph_ids: ['P01-001', 'P02-003'] },
      { type: 'apply_edit', doc_id: 'd', paragraph_id: 'P01-001', translated_html: '<p>x</p>', base_revision: 3 },
      { type: 'export', doc_id: 'd', output: '/o.pdf', mode: 'bilingual' },
      { type: 'cancel' },
    ];
    for (const request of requests) {
      expect(isRequest(request), request.type).toBe(true);
    }
  });

  it('拒绝字段缺失 / 非法 / 旧 provider', () => {
    expect(isRequest({ type: 'run', doc_id: 'd' })).toBe(false);
    const { pages: _pages, ...noPages } = runRequest('d');
    expect(isRequest(noPages)).toBe(false);
    expect(isRequest({ ...configureRequest(), provider: 'openai_compatible' })).toBe(false);
    expect(isRequest({ ...configureRequest(), concurrency: 0.5 })).toBe(false);
    expect(isRequest({ ...configureRequest(), translator: { kind: 'agy' } })).toBe(false);
    expect(isRequest({ type: 'retranslate', doc_id: 'd', paragraph_ids: 'P01-001' })).toBe(false);
    expect(isRequest({ type: 'apply_edit', doc_id: 'd', paragraph_id: 'p', translated_html: 'x', base_revision: 3 })).toBe(false);
    expect(isRequest({ type: 'unknown' })).toBe(false);
    expect(isRequest(null)).toBe(false);
  });
});
