/**
 * 草稿的纯逻辑（api.md §3.3）：段落级覆盖的读法、排版参数范围与输入校验、
 * 「基线 vs 草稿」的合并口径、以及 PATCH 请求体的构造。无 React、无 fetch。
 *
 * **服务端才是草稿真源**：这里不复制写盘/合并语义，只做三件事——
 * 1. 把 `draft.paragraphs[id]` 解析成可编辑状态（缺字段 = 没有覆盖，不是 0）；
 * 2. 按 `babeldoc/tools/agent/layout_overrides.py` 的键名与范围校验用户输入
 *    （范围两处硬编码迟早漂移，所以这里逐项注释来源，服务端仍会独立复核）；
 * 3. `layout` 是**整个对象替换**（不是逐字段合并，见 `serve/draft.py::patch`），
 *    所以构造补丁时必须带上当前全部生效字段 + 保留不认识的键（不许静默丢数据）。
 */
import type { DraftParagraph, DraftResponse } from '../api/types';
import type { Box } from './preview';

/** 段落级排版覆盖的四个数值键：键名/范围抄自 `layout_overrides.PARAGRAPH_FLOAT_KEYS`。 */
export const LAYOUT_FIELDS = [
  { key: 'scale_cap', label: '缩放上限', min: 0.1, max: 5 },
  { key: 'font_scale', label: '字号缩放', min: 0.2, max: 5 },
  { key: 'line_skip', label: '行距倍数', min: 0.8, max: 3 },
  { key: 'box_scale', label: '框缩放', min: 0.3, max: 5 },
] as const;

export type LayoutFieldKey = (typeof LAYOUT_FIELDS)[number]['key'];

/** 段落级排版覆盖的三个样式布尔键：键名抄自 `layout_overrides` 的样式覆盖（加粗/斜体/衬线）。 */
export const STYLE_FIELDS = [
  { key: 'bold', label: '加粗' },
  { key: 'italic', label: '斜体' },
  { key: 'serif', label: '衬线' },
] as const;

export type StyleFieldKey = (typeof STYLE_FIELDS)[number]['key'];

/** 草稿里的 `layout` 覆盖对象（值形状由服务端校验，前端只读认识的键）。 */
export type DraftLayout = Record<string, unknown>;

/** 可编辑的排版覆盖状态：空串 = 该字段没有覆盖（不是 0）。 */
export type LayoutInputs = Record<LayoutFieldKey, string>;

/** `draft.paragraphs[id]`（没有该段的覆盖 → undefined）。 */
export function draftParagraphOf(
  draft: DraftResponse | undefined,
  id: string | null,
): DraftParagraph | undefined {
  if (draft === undefined || id === null) return undefined;
  return draft.paragraphs[id];
}

function asFiniteNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

/** 草稿里的某个数值覆盖；没有 / 不是数字 → null。 */
export function layoutNumber(layout: DraftLayout | null | undefined, key: LayoutFieldKey): number | null {
  if (layout === null || layout === undefined) return null;
  return asFiniteNumber(layout[key]);
}

/**
 * 草稿里的某个样式布尔覆盖（加粗/斜体/衬线）；没有 / 不是布尔 → null。
 * `null` = 「跟随原文」：该键在草稿里不存在（存在即覆盖）。
 */
export function layoutBool(layout: DraftLayout | null | undefined, key: StyleFieldKey): boolean | null {
  if (layout === null || layout === undefined) return null;
  const value = layout[key];
  return typeof value === 'boolean' ? value : null;
}

/** 草稿里的 `box`（`[x, y, x2, y2]`，PDF 坐标 y 向上）；没有 / 形状不对 → null。 */
export function layoutBox(layout: DraftLayout | null | undefined): Box | null {
  if (layout === null || layout === undefined) return null;
  const raw = layout.box;
  if (!Array.isArray(raw) || raw.length !== 4) return null;
  const numbers = raw.map(asFiniteNumber);
  if (numbers.some((item) => item === null)) return null;
  return numbers as Box;
}

/** 可编辑的样式覆盖状态：键缺席 = 跟随原文（不是 false）。 */
export type StyleBools = Partial<Record<StyleFieldKey, boolean>>;

/** 草稿 → 样式覆盖状态（跟随原文的键不进对象；用于编辑器的本地状态）。 */
export function styleBoolsOf(layout: DraftLayout | null | undefined): StyleBools {
  const bools: StyleBools = {};
  for (const field of STYLE_FIELDS) {
    const value = layoutBool(layout, field.key);
    if (value !== null) bools[field.key] = value;
  }
  return bools;
}

/** 该段草稿里有没有排版覆盖（四个数值键或三个样式布尔任一存在，或 box 存在）。 */
export function hasLayoutOverride(layout: DraftLayout | null | undefined): boolean {
  if (layout === null || layout === undefined) return false;
  if (layoutBox(layout) !== null) return true;
  if (LAYOUT_FIELDS.some((field) => layoutNumber(layout, field.key) !== null)) return true;
  return STYLE_FIELDS.some((field) => layoutBool(layout, field.key) !== null);
}

/**
 * 输入框文本 → 覆盖状态：`''` = 没有覆盖，`'0.9'` = 0.9，非法（NaN/空以外的坏值）→ null + 报错。
 * 范围校验与前端提示分开：`error` 非空时**不许**发起 PATCH（服务端也会 422 `draft_invalid`）。
 */
export function validateLayoutInput(
  key: LayoutFieldKey,
  raw: string,
): { value: number | null; error: string | null } {
  const field = LAYOUT_FIELDS.find((item) => item.key === key);
  const text = raw.trim();
  if (text === '') return { value: null, error: null };
  const value = Number(text);
  if (!Number.isFinite(value)) return { value: null, error: '必须是数字' };
  if (field !== undefined && (value < field.min || value > field.max)) {
    return { value: null, error: `范围 ${field.min}–${field.max}` };
  }
  return { value, error: null };
}

/** 草稿 → 输入框文本（覆盖值；没有覆盖 = 空串，显示 placeholder 而不是编造数字）。 */
export function layoutInputsOf(layout: DraftLayout | null | undefined): LayoutInputs {
  const inputs = {} as LayoutInputs;
  for (const field of LAYOUT_FIELDS) {
    const value = layoutNumber(layout, field.key);
    inputs[field.key] = value === null ? '' : String(value);
  }
  return inputs;
}

/** 输入框文本 → 只保留有覆盖的字段（非法值由调用方先拦下，这里按 NaN 丢弃）。 */
export function layoutValuesOf(inputs: LayoutInputs): Partial<Record<LayoutFieldKey, number>> {
  const values: Partial<Record<LayoutFieldKey, number>> = {};
  for (const field of LAYOUT_FIELDS) {
    const { value } = validateLayoutInput(field.key, inputs[field.key]);
    if (value !== null) values[field.key] = value;
  }
  return values;
}

/**
 * 构造 `layout` 补丁（**整个对象**，服务端是替换语义）：
 * 认识的键按当前输入写，`extra` 里不认识的键（如 `force_break_after_text`）原样保留；
 * `bools`（给了才管样式键）：`true/false` 写入键，**没给的样式键从补丁里删掉**
 * （删键 = 跟随原文）；`bools === undefined` 时样式键按「不认识的键」原样保留
 * （老调用方不传 bools 的行为不变）。结果为空对象 → `null`（删掉该段的排版覆盖）。
 */
export function layoutPatch(
  values: Partial<Record<LayoutFieldKey, number>>,
  box: Box | null,
  extra: DraftLayout | null | undefined = undefined,
  bools: StyleBools | undefined = undefined,
): DraftLayout | null {
  const out: DraftLayout = {};
  if (extra !== null && extra !== undefined) {
    for (const [key, value] of Object.entries(extra)) {
      if (key === 'box') continue;
      if (LAYOUT_FIELDS.some((field) => field.key === key)) continue;
      if (bools !== undefined && STYLE_FIELDS.some((field) => field.key === key)) continue;
      out[key] = value;
    }
  }
  for (const [key, value] of Object.entries(values)) out[key] = value;
  if (bools !== undefined) {
    for (const [key, value] of Object.entries(bools)) out[key] = value;
  }
  if (box !== null) out.box = [...box];
  return Object.keys(out).length === 0 ? null : out;
}

/**
 * 译文显示口径：**草稿覆盖优先于 `translated.jsonl` 基线**；
 * `fromDraft` = 该段草稿里确实有 `target` 覆盖（用来显示「草稿已修改」+ 恢复按钮）。
 */
export function paragraphTargetView(
  baseline: string | null | undefined,
  draftParagraph: DraftParagraph | undefined,
): { target: string; fromDraft: boolean } {
  const override = draftParagraph?.target;
  if (typeof override === 'string') return { target: override, fromDraft: true };
  return { target: baseline ?? '', fromDraft: false };
}

/**
 * 译文补丁：与基线相同 → 删掉覆盖（`null`，前端据此判定「未修改」）；
 * 清空且基线为空 → 同样删覆盖；其余写新值。
 */
export function targetPatch(target: string, baseline: string | null | undefined): { target: string | null } {
  const base = baseline ?? '';
  if (target === base || target === '') return { target: null };
  return { target };
}

/** 「恢复」按钮：有排版覆盖 → 只删译文（保留排版）；否则整段删除（`null` 条目）。 */
export function restoreEntry(hasLayout: boolean): { target: null } | null {
  return hasLayout ? { target: null } : null;
}

/** 排版数值 → 只读的一行文本（`—` 表示没有覆盖；不编造默认值）。 */
export function layoutSummary(layout: DraftLayout | null | undefined): string {
  const parts = LAYOUT_FIELDS.map((field) => {
    const value = layoutNumber(layout, field.key);
    return `${field.key}=${value === null ? '—' : value}`;
  });
  return parts.join(' · ');
}

/** `box` 的四值只读文本（`x, y, x2, y2`，PDF y 向上）。 */
export function boxSummary(box: Box | null | undefined): string {
  if (box === null || box === undefined) return '—';
  return box.map((value) => value.toFixed(2)).join(', ');
}
