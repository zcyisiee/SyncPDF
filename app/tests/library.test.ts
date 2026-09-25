/**
 * Library：sha256 去重、标题来源优先级、引擎事件落库与快照、删除清理。
 */
import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { existsSync, mkdtempSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { Library } from '../src/main/library';
import type { EngineEvent, EngineEventBody } from '../src/shared/protocol';

let dir: string;
let library: Library;

function pdf(name: string, body = name): string {
  const path = join(dir, name);
  writeFileSync(path, `%PDF-1.4\n${body}\n%%EOF\n`);
  return path;
}

let seq = 0;
const ev = (body: EngineEventBody): EngineEvent => ({ seq: (seq += 1), ts: 1, ...body }) as EngineEvent;

beforeEach(() => {
  dir = mkdtempSync(join(tmpdir(), 'syncpdf-library-'));
  library = new Library(join(dir, 'root'));
});
afterEach(() => {
  library.close();
  rmSync(dir, { recursive: true, force: true });
});

describe('Library.addFile', () => {
  it('新 PDF：拷进 blobs、标题取文件名、状态 new', () => {
    const { doc, added } = library.addFile(pdf('2609.20519v1.pdf'));
    expect(added).toBe(true);
    expect(doc).toMatchObject({ title: '2609.20519v1', titleSource: 'filename', status: 'new', translatedPath: null });
    expect(doc.id).toMatch(/^[0-9a-f]{16}$/);
    expect(existsSync(doc.sourcePath)).toBe(true);
  });

  it('同内容不同文件名：返回已有条目，不重复加入', () => {
    const first = library.addFile(pdf('a.pdf', 'same')).doc;
    const second = library.addFile(pdf('b.pdf', 'same'));
    expect(second.added).toBe(false);
    expect(second.doc.id).toBe(first.id);
    expect(library.list()).toHaveLength(1);
  });

  it('非 PDF 拒绝', () => {
    const path = join(dir, 'x.pdf');
    writeFileSync(path, 'hello');
    expect(() => library.addFile(path)).toThrow(/不是 PDF/);
    expect(library.list()).toHaveLength(0);
  });
});

describe('Library.updateMeta', () => {
  it('高优先级来源覆盖低优先级，反之不覆盖；空值忽略', () => {
    const { id } = library.addFile(pdf('a.pdf')).doc;
    expect(library.updateMeta(id, { title: 'Info Title' }, 'pdf_info')).toBe(true);
    expect(library.updateMeta(id, { title: 'Layout Title', authors: 'A, B' }, 'layout')).toBe(true);
    expect(library.updateMeta(id, { title: 'Late Info', authors: 'C' }, 'pdf_info')).toBe(false);
    expect(library.updateMeta(id, { title: '  ', authors: null }, 'user')).toBe(false);
    expect(library.get(id)).toMatchObject({ title: 'Layout Title', titleSource: 'layout', authors: 'A, B' });
    expect(library.updateMeta(id, { title: 'Mine' }, 'user')).toBe(true);
    expect(library.updateMeta(id, { title: 'Relayout' }, 'layout')).toBe(false);
    expect(library.get(id)?.title).toBe('Mine');
  });
});

describe('Library.ingest / snapshot', () => {
  it('一轮运行落库：版面、段落（后到覆盖）、问题、进度与完成状态', () => {
    const { id } = library.addFile(pdf('a.pdf')).doc;
    library.setStatus(id, 'running');
    const rect = { x0: 0, y0: 0, x1: 10, y1: 10 };
    const paragraph = {
      type: 'paragraph' as const,
      paragraph_id: 'P01-001',
      page: 1,
      status: 'pending' as const,
      boxes: [rect],
      coord_system: 'pdf_user' as const,
      translated_html: null,
      kind: 'text' as const,
      source_text: 'Hello',
      source_bbox: rect,
    };
    library.ingest(id, ev({ type: 'run_started', protocol_version: 1, engine_version: 'x', doc_id: id, pages: 3 }));
    library.ingest(id, ev({ type: 'layout', page: 1, regions: [{ kind: 'text', inline: false, bbox: rect }] }));
    expect(library.ingest(id, ev({ type: 'doc_meta', title: 'Real Title', authors: 'Ada' }))).toBe(true);
    library.ingest(id, ev(paragraph));
    library.ingest(id, ev({ ...paragraph, status: 'typeset', translated_html: '<p>你好</p>' }));
    library.ingest(id, ev({ type: 'issue', severity: 'warning', code: 'fallback', paragraph_id: 'P01-001', page: 1, message: 'm' }));
    library.ingest(id, ev({ type: 'progress', stage: 'translating', done: 1, total: 4 }));
    const edits = [{ paragraph_id: 'P01-001', manual: true, style: { font_family: 'sans' as const } }];
    library.ingest(id, ev({ type: 'block_edits', edits }));
    expect(library.get(id)).toMatchObject({ pages: 3, progress: 0.25, title: 'Real Title', status: 'running' });
    library.ingest(id, ev({ type: 'document_finished', output: 'o', stats: { fonts: 1, expansion_ratio: 1, fallbacks: 2 } }));
    library.ingest(id, ev({ type: 'run_finished', ok: true, elapsed_ms: 1234 }));

    expect(library.get(id)).toMatchObject({ status: 'done', progress: 1, fallbacks: 2, elapsedMs: 1234 });
    const snapshot = library.snapshot(id);
    expect(Object.keys(snapshot.layout)).toEqual(['1']);
    expect(snapshot.paragraphs).toHaveLength(1);
    expect(snapshot.paragraphs[0]).toMatchObject({ status: 'typeset', translated_html: '<p>你好</p>' });
    expect(snapshot.issues).toMatchObject([{ code: 'fallback' }]);
    expect(snapshot.edits).toEqual(edits);

    // 旧版引擎缓存的段落记录缺 source_bbox：快照丢弃，不交给渲染端
    const { source_bbox: _box, ...legacy } = paragraph;
    library.ingest(id, ev({ ...legacy, paragraph_id: 'P01-002' } as never));
    expect(library.snapshot(id).paragraphs.map((p) => p.paragraph_id)).toEqual(['P01-001']);

    // 下一轮 run_started 清空上一轮数据；单块覆盖等新一轮 block_edits 整表替换
    library.ingest(id, ev({ type: 'run_started', protocol_version: 1, engine_version: 'x', doc_id: id, pages: 3 }));
    expect(library.snapshot(id)).toEqual({ layout: {}, paragraphs: [], issues: [], edits });
  });

  it('page_reopened 只清该页问题，保留版面、段落与其它页问题', () => {
    const { id } = library.addFile(pdf('reopen.pdf')).doc;
    const issue = (page: number | null) =>
      ev({ type: 'issue', severity: 'warning', code: 'c', paragraph_id: null, page, message: `p${page}` });
    library.ingest(id, ev({ type: 'run_started', protocol_version: 1, engine_version: 'x', doc_id: id, pages: 2 }));
    library.ingest(id, ev({ type: 'layout', page: 2, regions: [] }));
    library.ingest(id, issue(1));
    library.ingest(id, issue(2));
    library.ingest(id, issue(null));
    library.ingest(id, ev({ type: 'page_reopened', page: 2 }));
    const snapshot = library.snapshot(id);
    expect(snapshot.issues.map((i) => i.message)).toEqual(['p1', 'pnull']);
    expect(Object.keys(snapshot.layout)).toEqual(['2']);
  });

  it('run_finished ok:false：用户已取消记 cancelled，否则记 failed 并保留致命错误', () => {
    const a = library.addFile(pdf('a.pdf')).doc.id;
    const b = library.addFile(pdf('b.pdf')).doc.id;
    library.setStatus(a, 'cancelled');
    library.ingest(a, ev({ type: 'run_started', protocol_version: 1, engine_version: 'x', doc_id: a, pages: 1 }));
    library.ingest(a, ev({ type: 'run_finished', ok: false, elapsed_ms: 1 }));
    library.setStatus(b, 'running');
    library.ingest(b, ev({ type: 'error', fatal: true, code: 'translate', message: 'boom' }));
    library.ingest(b, ev({ type: 'run_finished', ok: false, elapsed_ms: 1 }));
    expect(library.get(a)?.status).toBe('cancelled');
    expect(library.get(b)).toMatchObject({ status: 'failed', error: 'boom' });
  });

  // 回归：引擎 ok 还要求无质量问题；译文已发布却 ok:false 的论文曾被标成失败
  it('已发布译文（有 document_finished）即完成，即使 ok:false；未发布的 ok:false 仍失败', () => {
    const published = library.addFile(pdf('a.pdf')).doc.id;
    const unpublished = library.addFile(pdf('b.pdf')).doc.id;
    for (const id of [published, unpublished]) {
      library.setStatus(id, 'running');
      library.ingest(id, ev({ type: 'run_started', protocol_version: 1, engine_version: 'x', doc_id: id, pages: 1 }));
    }
    library.ingest(published, ev({ type: 'issue', severity: 'warning', code: 'coverage_gap', paragraph_id: null, page: 1, message: 'm' }));
    library.ingest(published, ev({ type: 'document_finished', output: 'o', stats: { fonts: 1, expansion_ratio: 1, fallbacks: 0 } }));
    library.ingest(published, ev({ type: 'run_finished', ok: false, elapsed_ms: 1 }));
    library.ingest(unpublished, ev({ type: 'run_finished', ok: false, elapsed_ms: 1 }));
    expect(library.get(published)).toMatchObject({ status: 'done', progress: 1 });
    expect(library.get(unpublished)?.status).toBe('failed');

    // 上一轮的发布标记不带到下一轮
    library.ingest(published, ev({ type: 'run_started', protocol_version: 1, engine_version: 'x', doc_id: published, pages: 1 }));
    library.ingest(published, ev({ type: 'run_finished', ok: false, elapsed_ms: 1 }));
    expect(library.get(published)?.status).toBe('failed');
  });

  // 回归：全篇运行的 run_started 报 0 页，卡片一直显示"0 页"
  it('页数未知时记空，由 layout 事件的页号补齐；只在变大时广播', () => {
    const { id } = library.addFile(pdf('a.pdf')).doc;
    const rect = { x0: 0, y0: 0, x1: 1, y1: 1 };
    library.ingest(id, ev({ type: 'run_started', protocol_version: 1, engine_version: 'x', doc_id: id, pages: 0 }));
    expect(library.get(id)?.pages).toBeNull();
    expect(library.ingest(id, ev({ type: 'layout', page: 2, regions: [{ kind: 'text', inline: false, bbox: rect }] }))).toBe(true);
    expect(library.ingest(id, ev({ type: 'layout', page: 1, regions: [] }))).toBe(false);
    expect(library.get(id)?.pages).toBe(2);
  });

  // 回归：引擎不再把机构 / 邮箱行认作作者后，上一轮存下的错误作者仍挂在卡片上
  it('doc_meta 未识别的字段撤回上一轮 layout 值，回落文件名来源；非 layout 来源不动', () => {
    const { id } = library.addFile(pdf('2604.03136v6.pdf')).doc;
    library.ingest(id, ev({ type: 'doc_meta', title: 'StoryScope', authors: 'University of Maryland' }));
    expect(library.ingest(id, ev({ type: 'doc_meta', title: 'StoryScope', authors: null }))).toBe(true);
    expect(library.get(id)).toMatchObject({ title: 'StoryScope', authors: null, authorsSource: 'filename' });
    library.updateMeta(id, { authors: 'Jenna Russell' }, 'pdf_info');
    library.ingest(id, ev({ type: 'doc_meta', title: null, authors: null }));
    expect(library.get(id)).toMatchObject({
      title: '2604.03136v6',
      titleSource: 'filename',
      authors: 'Jenna Russell',
      authorsSource: 'pdf_info',
    });
  });

  it('setStatus：显式 error:null 清空旧错误，不给 error 则保留', () => {
    const { id } = library.addFile(pdf('a.pdf')).doc;
    library.setStatus(id, 'failed', { error: 'boom' });
    library.setStatus(id, 'failed');
    expect(library.get(id)?.error).toBe('boom');
    library.setStatus(id, 'queued', { error: null });
    expect(library.get(id)?.error).toBeNull();
  });
});

describe('Library.remove', () => {
  it('删除条目、工作目录、原文 blob 与缓存数据，其他论文不受影响', () => {
    const a = library.addFile(pdf('a.pdf')).doc;
    const b = library.addFile(pdf('b.pdf')).doc;
    library.ingest(a.id, ev({ type: 'layout', page: 1, regions: [] }));
    library.remove(a.id);
    expect(library.get(a.id)).toBeNull();
    expect(existsSync(a.sourcePath)).toBe(false);
    expect(existsSync(library.docDir(a.id))).toBe(false);
    expect(library.snapshot(a.id).layout).toEqual({});
    expect(existsSync(b.sourcePath)).toBe(true);
  });
});
