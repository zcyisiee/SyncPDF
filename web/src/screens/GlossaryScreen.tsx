/**
 * `#/glossary` 全局术语表视图（W13，docs/reference/http-api.md）。
 *
 * 一个全局词表（术语 → 指定译名），不是多词表管理：`GET/PUT/DELETE /glossary`。
 * 本屏是**整表编辑**：本地改完一次保存（`PUT` 覆盖整表，不做行级 patch），因此「保存」
 * 只在真有改动时可用，「放弃」把本地草稿丢掉回到服务端那一份。
 *
 * 两条红线写在界面上：
 * 1. **不回溯**：词表变更不会自动重翻任何已翻译内容 —— 顶部常驻提示条
 *    （`data-od-id="glossary-no-retro"`）；
 * 2. **CSV 的解析/导出在前端**：后端 `PUT` 只收 JSON 条目。导入的文件内容在本浏览器解析，
 *    导出也是本地生成（内容就是当前屏幕上的表，含未保存的编辑）。
 *
 * 注入是**服务端**的事：这里只维护词表本体；翻译时要不要带上它由「开始翻译」卡上的
 * 开关（`use_glossary`）决定，注入用的文件路径客户端永远看不到、也传不了。
 */
import { useMemo, useRef, useState } from 'react';

import type { GlossaryEntryModel } from '../api/types';
import { cn } from '../lib/cn';
import { ErrorCard } from '../components/ui/ErrorCard';
import {
  emptyGlossaryRow,
  glossaryToCsv,
  parseGlossaryCsv,
  rowsFromEntries,
  rowsToRequest,
  validateGlossaryRows,
  type GlossaryRow,
} from '../lib/glossary';
import {
  useClearGlossaryMutation,
  useGlossary,
  useReplaceGlossaryMutation,
} from '../lib/queries';
import { Button } from '../components/ui/Button';
import { ScrollArea } from '../components/ui/ScrollArea';

const INPUT_CLASS =
  'h-7 w-full rounded border border-hair bg-ivory px-2 text-sm text-ink-2 ' +
  'placeholder:text-ink-4 focus:border-accent focus:outline-none';

/** 单元格文案（表头 + placeholder 同源，两处不会漂）。 */
const COLUMNS = [
  { key: 'source', label: 'source（源词）', placeholder: 'attention' },
  { key: 'target', label: 'target（指定译名）', placeholder: '注意力' },
  { key: 'note', label: 'note（备注，可选）', placeholder: '首字母小写' },
] as const;

export function GlossaryScreen() {
  const glossaryQuery = useGlossary();
  const replaceGlossary = useReplaceGlossaryMutation();
  const clearGlossary = useClearGlossaryMutation();

  const entries = useMemo(
    () => glossaryQuery.data?.entries ?? [],
    [glossaryQuery.data],
  );
  // 服务端那一份（保存成功/刷新后跟着变）：只在「用户没改过」时直接显示。
  const serverRows = useMemo(
    () => rowsFromEntries(entries, (_entry, index) => `srv-${index}`),
    [entries],
  );
  const [draftRows, setDraftRows] = useState<GlossaryRow[] | null>(null);
  const [csvError, setCsvError] = useState<string | null>(null);
  const counter = useRef(0);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const rows = draftRows ?? serverRows;
  const dirty = draftRows !== null;
  const rowErrors = validateGlossaryRows(rows);
  const invalidCount = Object.keys(rowErrors).length;
  const canSave = dirty && invalidCount === 0 && !replaceGlossary.isPending;

  const newKey = () => {
    counter.current += 1;
    return `row-${counter.current}`;
  };

  const editRows = (next: GlossaryRow[]) => setDraftRows(next);

  const patchRow = (key: string, patch: Partial<GlossaryRow>) =>
    editRows(rows.map((row) => (row.key === key ? { ...row, ...patch } : row)));

  const addRow = () => editRows([...rows, emptyGlossaryRow(newKey())]);

  const removeRow = (key: string) => editRows(rows.filter((row) => row.key !== key));

  const save = () => {
    replaceGlossary.mutate(rowsToRequest(rows), {
      onSuccess: () => {
        // 服务端已规范化（去重/排序）并把响应写进缓存；本地草稿作废，直接看服务端那份。
        setDraftRows(null);
        setCsvError(null);
      },
    });
  };

  const discard = () => {
    setDraftRows(null);
    setCsvError(null);
  };

  const clearAll = () => {
    if (!window.confirm('清空全局词表？已经翻译过的内容不受影响（词表变更不回溯）。')) return;
    clearGlossary.mutate(undefined, { onSuccess: () => setDraftRows(null) });
  };

  const importCsv = async (file: File) => {
    try {
      const imported = parseGlossaryCsv(await file.text());
      setCsvError(null);
      editRows(
        imported.map((entry: GlossaryEntryModel) => ({
          key: newKey(),
          source: entry.source,
          target: entry.target,
          note: entry.note ?? '',
        })),
      );
    } catch (error) {
      setCsvError(error instanceof Error ? error.message : String(error));
    }
  };

  /**
   * 导出当前屏幕上的表（含未保存的编辑）：本地生成 CSV，不经过后端。
   * `createObjectURL` 在 jsdom 里没有，测试会自己 stub；这里仍然 try 一下防止环境差异炸屏。
   */
  const exportCsv = () => {
    const csv = glossaryToCsv(rowsToRequest(rows).entries);
    const blob = new Blob([csv], { type: 'text/csv;charset=utf-8' });
    const url = URL.createObjectURL(blob);
    const anchor = document.createElement('a');
    anchor.href = url;
    anchor.download = 'glossary.csv';
    document.body.appendChild(anchor);
    anchor.click();
    anchor.remove();
    URL.revokeObjectURL(url);
  };

  // TanStack v5 的 mutation 无错时 `error` 是 **null**（不是 undefined）：必须用 `== null`
  // 判空，否则会把 null 当成真错误渲染成「请求失败 / null」的假错误卡（W13 的真实回归）。
  const failure = replaceGlossary.error ?? clearGlossary.error;

  return (
    <ScrollArea className="h-full" data-od-id="screen-glossary">
      <div className="w-full max-w-[1220px] px-s7 pb-12 pt-s7">
        <div className="flex flex-wrap items-end justify-between gap-s3">
          <div>
            <h1 className="font-serif text-h1 font-medium leading-[1.3] text-ink">词表</h1>
            <p className="mt-s2 text-body text-ink-2">
              全局术语表：翻译时按「源词 → 指定译名」约束译文。
              <span className="font-mono text-tiny text-ink-4">
                {' '}
                · {entries.length} 条
              </span>
              {dirty ? (
                <span className="text-run-ink" data-od-id="glossary-dirty">
                  {' '}
                  · 有未保存的修改
                </span>
              ) : null}
            </p>
          </div>
          <div className="flex flex-wrap items-center gap-s2">
            <Button data-od-id="glossary-add-row" onClick={addRow}>
              添加行
            </Button>
            <Button
              data-od-id="glossary-import"
              onClick={() => fileInputRef.current?.click()}
              title="导入 CSV（列 source,target,note）——解析在本浏览器里做"
            >
              导入 CSV
            </Button>
            <Button
              data-od-id="glossary-export"
              onClick={exportCsv}
              disabled={rows.length === 0}
              title="导出当前表（含未保存的编辑）为 CSV"
            >
              导出 CSV
            </Button>
            <Button
              variant="danger"
              data-od-id="glossary-clear"
              onClick={clearAll}
              disabled={entries.length === 0 || clearGlossary.isPending}
            >
              {clearGlossary.isPending ? '正在清空…' : '清空'}
            </Button>
            <Button data-od-id="glossary-discard" onClick={discard} disabled={!dirty}>
              放弃
            </Button>
            <Button
              variant="primary"
              data-od-id="glossary-save"
              onClick={save}
              disabled={!canSave}
              title={invalidCount > 0 ? '有不合法的行：先改掉再保存' : '整表替换（PUT /glossary）'}
            >
              {replaceGlossary.isPending ? '正在保存…' : '保存'}
            </Button>
          </div>
          <input
            ref={fileInputRef}
            type="file"
            accept=".csv,text/csv"
            data-od-id="glossary-import-input"
            className="hidden"
            onChange={(event) => {
              const file = event.target.files?.[0];
              // 同一个文件连续导入两次也要触发 change：读走之后清空 value。
              event.target.value = '';
              if (file) void importCsv(file);
            }}
          />
        </div>

        <p
          data-od-id="glossary-no-retro"
          className="mt-s4 rounded-none border border-hair border-l-[3px] border-l-run bg-run-soft px-s4 py-s3 text-body text-ink-2"
        >
          <strong className="font-medium text-run-ink">对已翻译段落无追溯效果。</strong>{' '}
          词表只约束<b>之后新跑</b>的翻译：改完保存不会自动重翻任何已有译文，重新跑一次翻译才生效。
        </p>

        {glossaryQuery.isError ? (
          <ErrorCard className="mt-s4" data-od-id="glossary-load-error" error={glossaryQuery.error} />
        ) : null}

        {invalidCount > 0 ? (
          <p className="mt-s3 text-tiny text-err" data-od-id="glossary-row-error-count">
            有 {invalidCount} 行不合法（见行内提示）：source/target 都不能为空，且各不超过 200 字符。
          </p>
        ) : null}
        {csvError === null ? null : (
          <ErrorCard
            className="mt-s3"
            data-od-id="glossary-csv-error"
            title="CSV 导入失败"
            message={csvError}
          />
        )}
        {failure == null ? null : (
          <ErrorCard className="mt-s3" data-od-id="glossary-save-error" error={failure} />
        )}

        {glossaryQuery.isPending ? (
          <p className="mt-s5 text-body text-ink-3" data-od-id="glossary-loading">
            正在读取词表…
          </p>
        ) : rows.length === 0 ? (
          <p className="mt-s5 text-body text-ink-3" data-od-id="glossary-empty">
            还没有词表。点「添加行」或「导入 CSV」开始；词表为空时翻译不会注入任何术语。
          </p>
        ) : (
          <table
            data-od-id="glossary-table"
            className="mt-s5 w-full border-collapse text-left"
          >
            <thead>
              <tr>
                {COLUMNS.map((column) => (
                  <th
                    key={column.key}
                    scope="col"
                    className="border-b border-hair px-s2 py-s2 text-tiny font-medium text-ink-3"
                  >
                    {column.label}
                  </th>
                ))}
                <th scope="col" className="w-[72px] border-b border-hair px-s2 py-s2" />
              </tr>
            </thead>
            <tbody>
              {rows.map((row, index) => {
                const error = rowErrors[row.key];
                return (
                  <tr key={row.key} data-od-id="glossary-row" data-row-key={row.key}>
                    {COLUMNS.map((column) => (
                      <td key={column.key} className="border-b border-hair px-s2 py-s2 align-top">
                        <input
                          aria-label={`第 ${index + 1} 行 ${column.label}`}
                          data-od-id={`glossary-cell-${column.key}`}
                          value={row[column.key]}
                          placeholder={column.placeholder}
                          onChange={(event) => patchRow(row.key, { [column.key]: event.target.value })}
                          className={cn(INPUT_CLASS, error && column.key === 'source' && 'border-err')}
                        />
                        {/* 行级错误只占源词格下方一行：表格结构不变（不插额外列） */}
                        {error && column.key === 'source' ? (
                          <span className="mt-1 block text-tiny text-err" data-od-id="glossary-row-error">
                            {error}
                          </span>
                        ) : null}
                      </td>
                    ))}
                    <td className="border-b border-hair px-s2 py-s2 align-top">
                      <Button
                        size="sm"
                        variant="ghost"
                        data-od-id="glossary-delete-row"
                        aria-label={`删除第 ${index + 1} 行`}
                        onClick={() => removeRow(row.key)}
                      >
                        删除
                      </Button>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        )}

        <p className="mt-s4 font-mono text-tiny text-ink-4">
          保存 = 整表替换（PUT /api/v1/glossary）；服务端按 source 排序去重（同 source 以后者为准）。
        </p>
      </div>
    </ScrollArea>
  );
}
