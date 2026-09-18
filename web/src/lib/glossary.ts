/**
 * 术语表的纯函数与形状（W13，docs/reference/http-api.md）。
 *
 * 两条职责，都是**前端自己那一侧**的事：
 *
 * - **CSV 的解析/导出在前端**：后端 `PUT /glossary` 只收 JSON 条目（不收 CSV 文本）。
 *   列顺序 `source,target,note` 与后端落盘的那份一致，但解析器各写各的——
 *   前端这份要处理引号/换行/逗号（RFC 4180 的引号规则），后端那份只读自己写出来的文件。
 * - **行编辑模型**：编辑器的行是「字符串三元组 + 稳定 key」，落库前才收敛成条目。
 *
 * 校验口径与后端 §3.8 **一致**（非空 + 长度上限）：两边同一套规则，前端先挡一道
 * （行级错误就地显示），后端仍然是最终权威（`glossary_invalid` 带 `index`/`field`）。
 */
import type { GlossaryEntryModel, GlossaryUpdateRequest } from '../api/types';

/** 与后端 `babeldoc_tools.glossary` 同一组上限/列顺序。 */
export const MAX_SOURCE_CHARS = 200;
export const MAX_TARGET_CHARS = 200;
export const MAX_NOTE_CHARS = 200;
export const CSV_COLUMNS = ['source', 'target', 'note'] as const;

/** 编辑器里一行（`key` 只用于 React 列表与行级错误定位，不进请求体）。 */
export interface GlossaryRow {
  key: string;
  source: string;
  target: string;
  note: string;
}

/** CSV 形状不对（前端拒绝导入）时抛它；文案直接给用户看。 */
export class GlossaryCsvError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'GlossaryCsvError';
  }
}

/**
 * 行级校验：返回 `{行 key: 错误文案}`（空对象 = 全部合法）。
 *
 * 一行 source/target **都**为空 = 用户还没填，整行忽略（不是错误）；只填了一边才是错误
 * （提交上去后端也会 422，但就地提示比往返一趟强）。
 */
export function validateGlossaryRows(rows: GlossaryRow[]): Record<string, string> {
  const errors: Record<string, string> = {};
  for (const row of rows) {
    const source = row.source.trim();
    const target = row.target.trim();
    const note = row.note.trim();
    if (source === '' && target === '' && note === '') continue;
    if (source === '') errors[row.key] = 'source（源词）不能为空';
    else if (source.length > MAX_SOURCE_CHARS)
      errors[row.key] = `source 超过 ${MAX_SOURCE_CHARS} 字符`;
    else if (target === '') errors[row.key] = 'target（指定译名）不能为空';
    else if (target.length > MAX_TARGET_CHARS)
      errors[row.key] = `target 超过 ${MAX_TARGET_CHARS} 字符`;
    else if (note.length > MAX_NOTE_CHARS) errors[row.key] = `note 超过 ${MAX_NOTE_CHARS} 字符`;
  }
  return errors;
}

/**
 * 行 → 请求体。空白行丢掉；source/target 去空白；`note` 空串收敛成 `null`
 * （与后端「没有备注」同一表示）。
 */
export function rowsToRequest(rows: GlossaryRow[]): GlossaryUpdateRequest {
  const entries: GlossaryEntryModel[] = [];
  for (const row of rows) {
    const source = row.source.trim();
    const target = row.target.trim();
    if (source === '' && target === '') continue;
    const note = row.note.trim();
    entries.push({ source, target, note: note === '' ? null : note });
  }
  return { entries };
}

/** 服务端条目 → 编辑器行（`note` 的 `null` 展开成空串）。 */
export function rowsFromEntries(
  entries: GlossaryEntryModel[],
  makeKey: (entry: GlossaryEntryModel, index: number) => string,
): GlossaryRow[] {
  return entries.map((entry, index) => ({
    key: makeKey(entry, index),
    source: entry.source,
    target: entry.target,
    note: entry.note ?? '',
  }));
}

/** 一条空的追加行。 */
export function emptyGlossaryRow(key: string): GlossaryRow {
  return { key, source: '', target: '', note: '' };
}

/**
 * 解析 RFC 4180 风格的 CSV（只用到这里需要的部分：双引号包裹、`""` 转义、
 * 引号内换行/逗号）。表头必须含 `source` 与 `target`（顺序任意，多出的列忽略）。
 */
export function parseGlossaryCsv(text: string): GlossaryEntryModel[] {
  const rows = parseCsvRows(text.replace(/^\ufeff/, ''));
  const header = rows.shift();
  if (header === undefined) throw new GlossaryCsvError('CSV 是空的：至少要有 source,target 表头。');
  const columns = header.map((column) => column.trim().toLowerCase());
  const sourceIndex = columns.indexOf('source');
  const targetIndex = columns.indexOf('target');
  if (sourceIndex < 0 || targetIndex < 0) {
    throw new GlossaryCsvError('CSV 表头必须含 source 与 target 两列（第三列 note 可选）。');
  }
  const noteIndex = columns.indexOf('note');
  const entries: GlossaryEntryModel[] = [];
  for (const row of rows) {
    const source = (row[sourceIndex] ?? '').trim();
    const target = (row[targetIndex] ?? '').trim();
    if (source === '' && target === '') continue; // 空行/尾行
    const note = noteIndex < 0 ? '' : (row[noteIndex] ?? '').trim();
    entries.push({ source, target, note: note === '' ? null : note });
  }
  if (entries.length === 0) throw new GlossaryCsvError('CSV 里没有数据行。');
  return entries;
}

/** 条目 → CSV 文本（列顺序与后端落盘的一致，行尾 `\n`）。 */
export function glossaryToCsv(entries: GlossaryEntryModel[]): string {
  const lines = [CSV_COLUMNS.join(',')];
  for (const entry of entries) {
    lines.push(
      [entry.source, entry.target, entry.note ?? ''].map(csvField).join(','),
    );
  }
  return `${lines.join('\n')}\n`;
}

/** 需要引号时加引号（含逗号/引号/换行/前后空白才必须引；这里保守一点全都管）。 */
function csvField(value: string): string {
  if (!/[",\n\r]/.test(value)) return value;
  return `"${value.replace(/"/g, '""')}"`;
}

/** 逐字符扫的 CSV 解析（不用正则 split：引号内的逗号/换行不能算分隔符）。 */
function parseCsvRows(text: string): string[][] {
  const rows: string[][] = [];
  let row: string[] = [];
  let field = '';
  let quoted = false;
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (quoted) {
      if (char === '"') {
        if (text[index + 1] === '"') {
          field += '"';
          index += 1;
        } else {
          quoted = false;
        }
      } else {
        field += char;
      }
      continue;
    }
    if (char === '"') {
      quoted = true;
    } else if (char === ',') {
      row.push(field);
      field = '';
    } else if (char === '\n' || char === '\r') {
      if (char === '\r' && text[index + 1] === '\n') index += 1;
      row.push(field);
      rows.push(row);
      row = [];
      field = '';
    } else {
      field += char;
    }
  }
  if (field !== '' || row.length > 0) {
    row.push(field);
    rows.push(row);
  }
  return rows;
}
