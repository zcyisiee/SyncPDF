/**
 * 论文库（主进程）：`<root>/library.db`（node:sqlite）+ `blobs/<sha>.pdf` + `docs/<id>/`。
 *
 * - 原文按 sha256 去重：同一份 PDF 再加一次返回已有条目；
 * - 译文只保留最新一份 `docs/<id>/translated.pdf`（引擎就地覆盖）；
 * - 引擎事件经 `ingest` 落库（版面、段落、问题、状态），重新打开论文不必重跑引擎。
 *
 * 不依赖 electron，便于在 vitest 里直接测。
 */
import { createHash } from 'node:crypto';
import { existsSync, mkdirSync, readFileSync, rmSync, statSync, copyFileSync } from 'node:fs';
import { basename, join } from 'node:path';
import { DatabaseSync } from 'node:sqlite';
import { isEngineEvent, type EngineEvent, type LayoutRegion } from '../shared/protocol';
import {
  META_SOURCES,
  type DocSnapshot,
  type DocStatus,
  type IssueRecord,
  type LibraryDoc,
  type MetaSource,
  type ParagraphRecord,
} from '../shared/library';

const SCHEMA = `
CREATE TABLE IF NOT EXISTS docs (
  id TEXT PRIMARY KEY,
  sha256 TEXT NOT NULL UNIQUE,
  file_name TEXT NOT NULL,
  title TEXT NOT NULL,
  title_source TEXT NOT NULL,
  authors TEXT,
  authors_source TEXT NOT NULL,
  pages INTEGER,
  size INTEGER NOT NULL,
  status TEXT NOT NULL,
  progress REAL NOT NULL DEFAULT 0,
  fallbacks INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  elapsed_ms INTEGER,
  model TEXT,
  added_at INTEGER NOT NULL,
  updated_at INTEGER NOT NULL,
  opened_at INTEGER
);
CREATE TABLE IF NOT EXISTS doc_layout (
  doc_id TEXT NOT NULL,
  page INTEGER NOT NULL,
  regions TEXT NOT NULL,
  PRIMARY KEY (doc_id, page)
);
CREATE TABLE IF NOT EXISTS doc_paragraphs (
  doc_id TEXT NOT NULL,
  paragraph_id TEXT NOT NULL,
  data TEXT NOT NULL,
  PRIMARY KEY (doc_id, paragraph_id)
);
CREATE TABLE IF NOT EXISTS doc_issues (
  doc_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  data TEXT NOT NULL
);
`;

interface DocRow {
  id: string;
  sha256: string;
  file_name: string;
  title: string;
  title_source: string;
  authors: string | null;
  authors_source: string;
  pages: number | null;
  size: number;
  status: string;
  progress: number;
  fallbacks: number;
  error: string | null;
  elapsed_ms: number | null;
  model: string | null;
  added_at: number;
  updated_at: number;
  opened_at: number | null;
}

const rank = (source: MetaSource): number => META_SOURCES.indexOf(source);

/** 文件名 → 兜底标题（去扩展名）。 */
export function titleFromFileName(fileName: string): string {
  return fileName.replace(/\.pdf$/i, '');
}

export class Library {
  readonly root: string;
  private readonly db: DatabaseSync;
  /** 本轮已发布译文（收到 document_finished）的论文：run_finished 据此判完成。 */
  private readonly published = new Set<string>();

  constructor(root: string) {
    this.root = root;
    mkdirSync(join(root, 'blobs'), { recursive: true });
    mkdirSync(join(root, 'docs'), { recursive: true });
    this.db = new DatabaseSync(join(root, 'library.db'));
    this.db.exec('PRAGMA journal_mode = WAL;');
    this.db.exec(SCHEMA);
  }

  close(): void {
    this.db.close();
  }

  blobPath(sha256: string): string {
    return join(this.root, 'blobs', `${sha256}.pdf`);
  }

  docDir(id: string): string {
    return join(this.root, 'docs', id);
  }

  translatedPath(id: string): string {
    return join(this.docDir(id), 'translated.pdf');
  }

  /** 本篇阶段缓存库（引擎写入；删除论文时随工作目录一起清掉）。 */
  storePath(id: string): string {
    return join(this.docDir(id), 'store.db');
  }

  list(): LibraryDoc[] {
    const rows = this.db.prepare('SELECT * FROM docs ORDER BY added_at DESC').all() as unknown as DocRow[];
    return rows.map((row) => this.toDoc(row));
  }

  get(id: string): LibraryDoc | null {
    const row = this.db.prepare('SELECT * FROM docs WHERE id = ?').get(id) as unknown as DocRow | undefined;
    return row === undefined ? null : this.toDoc(row);
  }

  /** 加入一份本地 PDF；同内容已存在则返回已有条目（`added=false`）。 */
  addFile(path: string, now = Date.now()): { doc: LibraryDoc; added: boolean } {
    const bytes = readFileSync(path);
    if (bytes.subarray(0, 5).toString('latin1') !== '%PDF-') {
      throw new Error(`不是 PDF 文件：${basename(path)}`);
    }
    const sha256 = createHash('sha256').update(bytes).digest('hex');
    const existing = this.db.prepare('SELECT id FROM docs WHERE sha256 = ?').get(sha256) as
      | { id: string }
      | undefined;
    if (existing !== undefined) {
      return { doc: this.get(existing.id) as LibraryDoc, added: false };
    }
    const blob = this.blobPath(sha256);
    if (!existsSync(blob)) copyFileSync(path, blob);
    const id = sha256.slice(0, 16);
    mkdirSync(this.docDir(id), { recursive: true });
    const fileName = basename(path);
    this.db
      .prepare(
        `INSERT INTO docs (id, sha256, file_name, title, title_source, authors, authors_source,
           pages, size, status, added_at, updated_at)
         VALUES (?, ?, ?, ?, 'filename', NULL, 'filename', NULL, ?, 'new', ?, ?)`,
      )
      .run(id, sha256, fileName, titleFromFileName(fileName), bytes.length, now, now);
    return { doc: this.get(id) as LibraryDoc, added: true };
  }

  /** 删除条目、工作目录与原文 blob（sha256 唯一，blob 只属于这一条）。 */
  remove(id: string): void {
    const doc = this.get(id);
    if (doc === null) return;
    for (const table of ['doc_layout', 'doc_paragraphs', 'doc_issues']) {
      this.db.prepare(`DELETE FROM ${table} WHERE doc_id = ?`).run(id);
    }
    this.db.prepare('DELETE FROM docs WHERE id = ?').run(id);
    rmSync(this.docDir(id), { recursive: true, force: true });
    rmSync(doc.sourcePath, { force: true });
  }

  /**
   * 按来源优先级更新标题 / 作者：低优先级来源不覆盖高优先级（用户手改最高）。
   * 空串视为无值。返回是否有字段被更新。
   */
  updateMeta(
    id: string,
    meta: { title?: string | null; authors?: string | null },
    source: MetaSource,
  ): boolean {
    const doc = this.get(id);
    if (doc === null) return false;
    let changed = false;
    const title = meta.title?.trim() ?? '';
    if (title !== '' && rank(source) >= rank(doc.titleSource)) {
      this.db
        .prepare('UPDATE docs SET title = ?, title_source = ?, updated_at = ? WHERE id = ?')
        .run(title, source, Date.now(), id);
      changed = true;
    }
    const authors = meta.authors?.trim() ?? '';
    if (authors !== '' && rank(source) >= rank(doc.authorsSource)) {
      this.db
        .prepare('UPDATE docs SET authors = ?, authors_source = ?, updated_at = ? WHERE id = ?')
        .run(authors, source, Date.now(), id);
      changed = true;
    }
    return changed;
  }

  /**
   * 每轮运行重新识别版面元数据：本轮没识别出的字段，撤回上一轮来自 layout 的旧值，
   * 回落到文件名来源（打开论文时再由 PDF Info 补上）。
   */
  private retractLayoutMeta(id: string, meta: { title: string | null; authors: string | null }): boolean {
    const doc = this.get(id);
    if (doc === null) return false;
    const now = Date.now();
    let changed = false;
    if (!meta.title?.trim() && doc.titleSource === 'layout') {
      const row = this.db.prepare('SELECT file_name FROM docs WHERE id = ?').get(id) as { file_name: string };
      this.db
        .prepare("UPDATE docs SET title = ?, title_source = 'filename', updated_at = ? WHERE id = ?")
        .run(titleFromFileName(row.file_name), now, id);
      changed = true;
    }
    if (!meta.authors?.trim() && doc.authorsSource === 'layout') {
      this.db
        .prepare("UPDATE docs SET authors = NULL, authors_source = 'filename', updated_at = ? WHERE id = ?")
        .run(now, id);
      changed = true;
    }
    return changed;
  }

  /** 设置状态；`error` 显式给出（含 null）时覆盖，未给出时保留。 */
  setStatus(id: string, status: DocStatus, fields: { error?: string | null; model?: string } = {}): void {
    const doc = this.get(id);
    if (doc === null) return;
    const error = 'error' in fields ? (fields.error ?? null) : doc.error;
    this.db
      .prepare('UPDATE docs SET status = ?, error = ?, model = ?, updated_at = ? WHERE id = ?')
      .run(status, error, fields.model ?? doc.model, Date.now(), id);
  }

  markOpened(id: string): void {
    this.db.prepare('UPDATE docs SET opened_at = ? WHERE id = ?').run(Date.now(), id);
  }

  /**
   * 落库一个属于 `id` 的引擎事件。返回论文卡片字段是否变化（调用方据此广播）。
   * 新一轮 `run_started` 清空上一轮的版面 / 段落 / 问题。
   */
  ingest(id: string, event: EngineEvent): boolean {
    const now = Date.now();
    switch (event.type) {
      case 'run_started':
        for (const table of ['doc_layout', 'doc_paragraphs', 'doc_issues']) {
          this.db.prepare(`DELETE FROM ${table} WHERE doc_id = ?`).run(id);
        }
        this.db
          .prepare(
            // 状态由队列维护（可能已被取消），这里不动
            'UPDATE docs SET pages = ?, progress = 0, error = NULL, updated_at = ? WHERE id = ?',
          )
          // 全篇运行时引擎 preflight 前不知道页数（报 0），由之后的 layout 事件补上
          .run(event.pages > 0 ? event.pages : null, now, id);
        this.published.delete(id);
        return true;
      case 'progress':
        if (event.stage !== 'translating' || event.total === 0) return false;
        this.db
          .prepare('UPDATE docs SET progress = ?, updated_at = ? WHERE id = ?')
          .run(event.done / event.total, now, id);
        return true;
      case 'layout':
        this.db
          .prepare('INSERT OR REPLACE INTO doc_layout (doc_id, page, regions) VALUES (?, ?, ?)')
          .run(id, event.page, JSON.stringify(event.regions));
        return (
          this.db
            .prepare('UPDATE docs SET pages = ? WHERE id = ? AND (pages IS NULL OR pages < ?)')
            .run(event.page, id, event.page).changes > 0
        );
      case 'doc_meta': {
        const retracted = this.retractLayoutMeta(id, event);
        return this.updateMeta(id, { title: event.title, authors: event.authors }, 'layout') || retracted;
      }
      case 'paragraph': {
        const { seq: _seq, ts: _ts, type: _type, ...record } = event;
        this.db
          .prepare('INSERT OR REPLACE INTO doc_paragraphs (doc_id, paragraph_id, data) VALUES (?, ?, ?)')
          .run(id, event.paragraph_id, JSON.stringify(record));
        return false;
      }
      case 'issue': {
        const { seq, ts: _ts, type: _type, ...record } = event;
        this.db
          .prepare('INSERT INTO doc_issues (doc_id, seq, data) VALUES (?, ?, ?)')
          .run(id, seq, JSON.stringify(record));
        return false;
      }
      case 'page_ready':
        // 译文文件已落盘：卡片的 translatedPath 从 null 变为可用
        return true;
      case 'document_finished':
        this.db
          .prepare('UPDATE docs SET fallbacks = ?, updated_at = ? WHERE id = ?')
          .run(event.stats.fallbacks, now, id);
        this.published.add(id);
        return true;
      case 'error':
        if (!event.fatal) return false;
        this.db.prepare('UPDATE docs SET error = ?, updated_at = ? WHERE id = ?').run(event.message, now, id);
        return true;
      case 'run_finished': {
        // 引擎的 ok 还要求"无质量问题"（回退、源区域冲突、覆盖缺口）；译文已发布
        // （本轮有 document_finished）就是完成，质量问题在问题列表里看。
        // 引擎 ok 还要求零质量问题；译文已发布（document_finished）同样算完成
        const done = this.published.delete(id) || event.ok;
        const doc = this.get(id);
        const status: DocStatus = done ? 'done' : doc?.status === 'cancelled' ? 'cancelled' : 'failed';
        this.db
          .prepare(
            `UPDATE docs SET status = ?, elapsed_ms = ?, progress = CASE WHEN ? THEN 1 ELSE progress END,
               updated_at = ? WHERE id = ?`,
          )
          .run(status, event.elapsed_ms, done ? 1 : 0, now, id);
        return true;
      }
      default:
        return false;
    }
  }

  snapshot(id: string): DocSnapshot {
    const layout: Record<number, LayoutRegion[]> = {};
    const layoutRows = this.db
      .prepare('SELECT page, regions FROM doc_layout WHERE doc_id = ? ORDER BY page')
      .all(id) as unknown as Array<{ page: number; regions: string }>;
    for (const row of layoutRows) layout[row.page] = JSON.parse(row.regions) as LayoutRegion[];
    const paragraphs = (
      this.db
        .prepare('SELECT data FROM doc_paragraphs WHERE doc_id = ? ORDER BY paragraph_id')
        .all(id) as unknown as Array<{ data: string }>
    )
      .map((row) => JSON.parse(row.data) as unknown)
      // 旧版引擎写入的记录缺新字段：丢弃，重新翻译后再补齐，不让渲染端读到残缺记录
      .filter((data): data is ParagraphRecord => isEngineEvent({ ...(data as object), seq: 0, ts: 0, type: 'paragraph' }));
    const issues = (
      this.db
        .prepare('SELECT data FROM doc_issues WHERE doc_id = ? ORDER BY seq')
        .all(id) as unknown as Array<{ data: string }>
    ).map((row) => JSON.parse(row.data) as IssueRecord);
    return { layout, paragraphs, issues };
  }

  private toDoc(row: DocRow): LibraryDoc {
    const translated = this.translatedPath(row.id);
    return {
      id: row.id,
      sha256: row.sha256,
      fileName: row.file_name,
      title: row.title,
      titleSource: row.title_source as MetaSource,
      authors: row.authors,
      authorsSource: row.authors_source as MetaSource,
      pages: row.pages,
      size: row.size,
      status: row.status as DocStatus,
      progress: row.progress,
      fallbacks: row.fallbacks,
      error: row.error,
      elapsedMs: row.elapsed_ms,
      model: row.model,
      addedAt: row.added_at,
      updatedAt: row.updated_at,
      openedAt: row.opened_at,
      sourcePath: this.blobPath(row.sha256),
      translatedPath: existsSync(translated) && statSync(translated).size > 0 ? translated : null,
    };
  }
}
