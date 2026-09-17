/**
 * W13 术语表的前端纯函数：CSV 解析/导出、行校验、行 → 请求体（docs/frontend/api.md §3.8）。
 *
 * 后端 `PUT /glossary` **不收 CSV**：CSV 的解析/导出全在这里，所以引号/换行/逗号/BOM
 * 这些真 CSV 的形状必须在本文件的用例里钉住。
 */
import { describe, expect, it } from 'vitest';

import {
  MAX_SOURCE_CHARS,
  emptyGlossaryRow,
  glossaryToCsv,
  parseGlossaryCsv,
  rowsFromEntries,
  rowsToRequest,
  validateGlossaryRows,
  type GlossaryRow,
} from '../src/lib/glossary';

function row(source: string, target: string, note = '', key = 'r1'): GlossaryRow {
  return { key, source, target, note };
}

describe('parseGlossaryCsv（导入）', () => {
  it('列 source,target,note：note 可缺省', () => {
    expect(parseGlossaryCsv('source,target\nattention,注意力\n')).toEqual([
      { source: 'attention', target: '注意力', note: null },
    ]);
  });

  it('三列时 note 进条目（空串收敛成 null）', () => {
    expect(parseGlossaryCsv('source,target,note\na,甲,首选\nb,乙,\n')).toEqual([
      { source: 'a', target: '甲', note: '首选' },
      { source: 'b', target: '乙', note: null },
    ]);
  });

  it('列顺序任意、多余列忽略、表头大小写/空格容忍', () => {
    expect(parseGlossaryCsv('Note, SOURCE ,target\nx,a,甲\n')).toEqual([
      { source: 'a', target: '甲', note: 'x' },
    ]);
  });

  it('引号里的逗号/换行/双引号按 RFC 4180 解析', () => {
    const csv = 'source,target\n"a, b","甲\n乙"\n"说 ""引号""","丙"\n';
    expect(parseGlossaryCsv(csv)).toEqual([
      { source: 'a, b', target: '甲\n乙', note: null },
      { source: '说 "引号"', target: '丙', note: null },
    ]);
  });

  it('CRLF 行尾与 UTF-8 BOM 都能吃', () => {
    expect(parseGlossaryCsv('\ufeffsource,target\r\na,甲\r\n')).toEqual([
      { source: 'a', target: '甲', note: null },
    ]);
  });

  it('空行跳过；缺 source/target 表头 / 没有数据行 → 明确报错', () => {
    expect(parseGlossaryCsv('source,target\n\na,甲\n\n')).toEqual([
      { source: 'a', target: '甲', note: null },
    ]);
    expect(() => parseGlossaryCsv('term,translation\na,b\n')).toThrow(/source/);
    expect(() => parseGlossaryCsv('source,target\n')).toThrow(/没有数据行/);
    expect(() => parseGlossaryCsv('')).toThrow(/空的/);
  });
});

describe('glossaryToCsv（导出）', () => {
  it('表头固定、含逗号/引号/换行的字段加引号转义', () => {
    const csv = glossaryToCsv([
      { source: 'a, b', target: '甲', note: null },
      { source: 'x', target: '说 "引号"', note: '备注\n换行' },
    ]);
    expect(csv.split('\n')[0]).toBe('source,target,note');
    expect(csv).toContain('"a, b",甲,');
    expect(csv).toContain('x,"说 ""引号""","备注\n换行"');
  });

  it('导出再导入是同一个表（round trip）', () => {
    const entries = [
      { source: 'attention', target: '注意力', note: '首选' },
      { source: 'LTO', target: '链接时优化', note: null },
    ];
    expect(parseGlossaryCsv(glossaryToCsv(entries))).toEqual(entries);
  });
});

describe('validateGlossaryRows（行级校验，与后端 §3.8 同一口径）', () => {
  it('整行全空 = 还没填，不算错；只填一边 = 错', () => {
    expect(validateGlossaryRows([emptyGlossaryRow('k')])).toEqual({});
    expect(validateGlossaryRows([row('', '甲')])).toMatchObject({ r1: expect.stringContaining('source') });
    expect(validateGlossaryRows([row('a', '')])).toMatchObject({ r1: expect.stringContaining('target') });
  });

  it('长度上限（source/target/note）', () => {
    expect(validateGlossaryRows([row('a'.repeat(MAX_SOURCE_CHARS + 1), '甲')])).toMatchObject({
      r1: expect.stringContaining('source'),
    });
    expect(validateGlossaryRows([row('a', '甲', 'n'.repeat(201))])).toMatchObject({
      r1: expect.stringContaining('note'),
    });
  });

  it('按行 key 报告，合法行不受影响', () => {
    const errors = validateGlossaryRows([row('a', '甲', '', 'ok'), row('', '', '', 'blank')]);
    expect(errors).toEqual({});
    const bad = validateGlossaryRows([row('a', '甲', '', 'ok'), row('b', '', '', 'bad')]);
    expect(Object.keys(bad)).toEqual(['bad']);
  });
});

describe('rowsToRequest / rowsFromEntries', () => {
  it('去空白、丢空白行、note 空串收敛成 null', () => {
    expect(
      rowsToRequest([row('  a  ', ' 甲 ', ' 备注 '), row('', '', ''), row('b', '乙', '')]),
    ).toEqual({
      entries: [
        { source: 'a', target: '甲', note: '备注' },
        { source: 'b', target: '乙', note: null },
      ],
    });
  });

  it('服务端条目 → 行：note 的 null 展开成空串，key 由调用方给', () => {
    expect(
      rowsFromEntries(
        [
          { source: 'a', target: '甲', note: null },
          { source: 'b', target: '乙', note: '备注' },
        ],
        (_entry, index) => `srv-${index}`,
      ),
    ).toEqual([
      { key: 'srv-0', source: 'a', target: '甲', note: '' },
      { key: 'srv-1', source: 'b', target: '乙', note: '备注' },
    ]);
  });
});
