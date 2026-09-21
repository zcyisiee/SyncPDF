/**
 * protocol.ts 类型守卫测试（M2-04 验收的一部分）。
 */
import { describe, expect, it } from 'vitest';
import {
  isEngineEvent,
  isRequest,
  isStageName,
  type EngineEvent,
  type Request,
} from '../src/shared/protocol';

describe('isEngineEvent', () => {
  it('接受合法事件', () => {
    const valid: EngineEvent[] = [
      { seq: 1, ts: 1727000000.123, type: 'run_started', protocol_version: 1, engine_version: '0.1', doc_id: 'd', pages: 12 },
      { seq: 2, ts: 1, type: 'stage_started', stage: 'translating' },
      { seq: 3, ts: 1, type: 'stage_finished', stage: 'typesetting', elapsed_ms: 42 },
      { seq: 4, ts: 1, type: 'progress', stage: 'translating', done: 42, total: 169 },
      {
        seq: 5,
        ts: 1,
        type: 'paragraph',
        paragraph_id: 'P01-001',
        status: 'translated',
        boxes: [[1, 2, 3, 4]],
        coord_system: 'pdf_native',
        translated_html: '<p id="P01-001">x</p>',
      },
      { seq: 6, ts: 1, type: 'paragraph', paragraph_id: 'P01-002', status: 'fallback', boxes: null, coord_system: 'pdf_topleft', translated_html: '' },
      { seq: 7, ts: 1, type: 'page_ready', page: 3, preview_path: '/tmp/p.pdf' },
      { seq: 8, ts: 1, type: 'issue', severity: 'warning', code: 'font_missing', message: 'm' },
      { seq: 9, ts: 1, type: 'document_finished', output: '/tmp/o.pdf', stats: { fonts: 1, expansion_ratio: 1.1, fallback_count: 0 } },
      { seq: 10, ts: 1, type: 'run_finished', ok: true, elapsed_ms: 100 },
      { seq: 11, ts: 1, type: 'error', fatal: true, code: 'conflict', message: 'm' },
    ];
    for (const event of valid) {
      expect(isEngineEvent(event), event.type).toBe(true);
    }
  });

  it('拒绝缺公共字段 / seq 非整数 / ts 非数', () => {
    expect(isEngineEvent(null)).toBe(false);
    expect(isEngineEvent({ type: 'progress', stage: 'translating', done: 1, total: 2 })).toBe(false); // 缺 seq/ts
    expect(isEngineEvent({ seq: 1.5, ts: 1, type: 'progress', stage: 'translating', done: 1, total: 2 })).toBe(false);
    expect(isEngineEvent({ seq: 1, ts: 'x', type: 'progress', stage: 'translating', done: 1, total: 2 })).toBe(false);
    expect(isEngineEvent({ seq: -1, ts: 1, type: 'progress', stage: 'translating', done: 1, total: 2 })).toBe(false);
  });

  it('拒绝未知 type 与字段形状错误', () => {
    expect(isEngineEvent({ seq: 1, ts: 1, type: 'nonsense' })).toBe(false);
    // stage 名非法
    expect(isEngineEvent({ seq: 1, ts: 1, type: 'stage_started', stage: 'cooking' })).toBe(false);
    // progress total 非整数
    expect(isEngineEvent({ seq: 1, ts: 1, type: 'progress', stage: 'translating', done: 1, total: 1.5 })).toBe(false);
    // paragraph status 非法
    expect(
      isEngineEvent({
        seq: 1,
        ts: 1,
        type: 'paragraph',
        paragraph_id: 'P01-001',
        status: 'pending',
        boxes: [],
        coord_system: 'pdf_native',
        translated_html: 'x',
      }),
    ).toBe(false);
    // paragraph coord_system 非法
    expect(
      isEngineEvent({
        seq: 1,
        ts: 1,
        type: 'paragraph',
        paragraph_id: 'P01-001',
        status: 'translated',
        boxes: [],
        coord_system: 'screen',
        translated_html: 'x',
      }),
    ).toBe(false);
    // error 缺 fatal
    expect(isEngineEvent({ seq: 1, ts: 1, type: 'error', code: 'x', message: 'm' })).toBe(false);
  });

  it('paragraph 的 boxes 允许 null 与 []（规约 #12/#13）', () => {
    expect(
      isEngineEvent({
        seq: 1,
        ts: 1,
        type: 'paragraph',
        paragraph_id: 'P01-001',
        status: 'not_replaced',
        boxes: null,
        coord_system: 'pdf_native',
        translated_html: '',
      }),
    ).toBe(true);
    expect(
      isEngineEvent({
        seq: 2,
        ts: 1,
        type: 'paragraph',
        paragraph_id: 'P01-002',
        status: 'typeset',
        boxes: [],
        coord_system: 'pdf_topleft',
        translated_html: 'x',
      }),
    ).toBe(true);
  });
});

describe('isStageName', () => {
  it('八阶段名合法', () => {
    for (const stage of [
      'preflight',
      'source_analysis',
      'layout_analysis',
      'paragraph_analysis',
      'translating',
      'typesetting',
      'validating',
      'publishing',
    ] as const) {
      expect(isStageName(stage)).toBe(true);
    }
    expect(isStageName('nope')).toBe(false);
    expect(isStageName(42)).toBe(false);
  });
});

describe('isRequest', () => {
  it('六种请求合法', () => {
    const requests: Request[] = [
      { type: 'configure', provider: 'openai_compatible', base_url: 'http://x', model: 'm', api_key: 'k', concurrency: 4, cache_dir: '/tmp' },
      { type: 'run', doc_id: 'd', input: '/tmp/a.pdf', output: '/tmp/b.pdf', source_lang: 'en', target_lang: 'zh', mode: 'full' },
      { type: 'retranslate', doc_id: 'd', paragraph_ids: ['P01-001', 'P02-003'] },
      { type: 'apply_edit', doc_id: 'd', paragraph_id: 'P01-001', translated_html: '<p>x</p>', base_revision: 3 },
      { type: 'export', doc_id: 'd', output: '/tmp/o.pdf', mode: 'bilingual' },
      { type: 'cancel' },
    ];
    for (const request of requests) {
      expect(isRequest(request), request.type).toBe(true);
    }
  });

  it('拒绝字段缺失 / 非法', () => {
    expect(isRequest({ type: 'run', doc_id: 'd' })).toBe(false);
    expect(isRequest({ type: 'configure', provider: 'x', base_url: 'y', model: 'm', api_key: 'k', concurrency: 0.5, cache_dir: '/tmp' })).toBe(false);
    expect(isRequest({ type: 'retranslate', doc_id: 'd', paragraph_ids: 'P01-001' })).toBe(false);
    expect(isRequest({ type: 'apply_edit', doc_id: 'd', paragraph_id: 'p', translated_html: 'x', base_revision: '3' })).toBe(false);
    expect(isRequest({ type: 'unknown' })).toBe(false);
    expect(isRequest(null)).toBe(false);
  });
});
