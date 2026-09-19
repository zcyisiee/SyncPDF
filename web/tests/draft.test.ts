/**
 * `lib/draft.ts` 的样式布尔扩展：`layoutBool` 读法、`layoutPatch` 的写入/删键
 * （跟随原文）/保留未知键，以及 `styleBoolsOf` / `hasLayoutOverride` 对样式键的口径。
 */
import { describe, expect, it } from 'vitest';

import {
  STYLE_FIELDS,
  hasLayoutOverride,
  layoutBool,
  layoutPatch,
  styleBoolsOf,
} from '../src/lib/draft';

describe('layoutBool', () => {
  it('草稿里有布尔 → 该值；没有 / 非布尔 / 整个 layout 缺 → null（跟随原文）', () => {
    expect(layoutBool({ bold: true }, 'bold')).toBe(true);
    expect(layoutBool({ italic: false }, 'italic')).toBe(false);
    expect(layoutBool({ bold: 'yes' }, 'bold')).toBeNull(); // 非布尔不算覆盖
    expect(layoutBool({}, 'serif')).toBeNull();
    expect(layoutBool(null, 'bold')).toBeNull();
    expect(layoutBool(undefined, 'bold')).toBeNull();
  });

  it('STYLE_FIELDS 恰好是 bold/italic/serif 三个键', () => {
    expect(STYLE_FIELDS.map((field) => field.key)).toEqual(['bold', 'italic', 'serif']);
  });
});

describe('styleBoolsOf / hasLayoutOverride', () => {
  it('styleBoolsOf 只保留有覆盖的键（跟随原文不进对象）', () => {
    expect(styleBoolsOf({ bold: true, italic: false })).toEqual({ bold: true, italic: false });
    expect(styleBoolsOf(null)).toEqual({});
    expect(styleBoolsOf({ bold: 1 })).toEqual({}); // 非布尔当没有覆盖
  });

  it('hasLayoutOverride：只有样式布尔覆盖也算排版覆盖', () => {
    expect(hasLayoutOverride({ serif: false })).toBe(true);
    expect(hasLayoutOverride({})).toBe(false);
    expect(hasLayoutOverride(null)).toBe(false);
  });
});

describe('layoutPatch 带样式布尔', () => {
  it('true/false 写入键；未提供的样式键删掉（跟随原文）', () => {
    expect(layoutPatch({}, null, undefined, { bold: true })).toEqual({ bold: true });
    expect(layoutPatch({}, null, { bold: true, italic: true }, { italic: false })).toEqual({
      italic: false,
    });
    // extra 里只有样式键、bools 全空 → 补丁为空对象 → null（删掉整段排版覆盖）
    expect(layoutPatch({}, null, { bold: true }, {})).toBeNull();
  });

  it('bools 未提供（undefined）时样式键按未知键原样保留（老调用方行为不变）', () => {
    expect(layoutPatch({ font_scale: 1.2 }, null, { bold: true, serif: false })).toEqual({
      bold: true,
      serif: false,
      font_scale: 1.2,
    });
  });

  it('保留未知键 + box；数值、样式布尔、box 同一个补丁里共存', () => {
    const patch = layoutPatch(
      { font_scale: 1.05 },
      [70, 630, 522, 782],
      { force_break_after_text: 'x', bold: true, box: [1, 2, 3, 4] },
      { bold: false, italic: true },
    );
    expect(patch).toEqual({
      force_break_after_text: 'x', // 不认识的键不许静默丢
      font_scale: 1.05,
      bold: false,
      italic: true,
      box: [70, 630, 522, 782], // extra 里的 box 被显式 box 覆盖（拖拽框优先）
    });
  });
});
