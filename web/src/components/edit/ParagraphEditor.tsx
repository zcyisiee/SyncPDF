/**
 * 右侧面板「段落」tab：选中段的译文编辑 + 排版参数（api.md §3.3 的草稿写入口）。
 *
 * 数据口径：
 * - 译文 = **草稿覆盖优先**于 `GET /paragraphs` 的基线（`translated.jsonl`），
 *   草稿里有覆盖 → 顶部「草稿已修改」chip + 「恢复」按钮（PATCH `target: null`）；
 * - 排版 = 四个数值覆盖（范围同 `layout_overrides.PARAGRAPH_FLOAT_KEYS`）+ 只读 `box`
 *   （`box` 只能在预览里拖拽，见 `BboxEditor`）；
 * - 编译样式 = `GET /paragraphs` 的 `style` 只读摘要（源文字号/字体/加粗/斜体/衬线）+
 *   覆盖下拉（加粗/斜体/衬线：跟随原文/开启/关闭 → 草稿 `layout.bold/italic/serif`，
 *   跟随原文 = 删键），保存走同一条防抖链路；
 * - 段落级字体族 = `GET /fonts` 清单里的 id（`layout.font_family`，跟随默认 = 删键）；
 *   清单加载失败只是这一项不可改，不影响译文与其它参数；
 * - 「字号」pt 下拉是 `font_scale` 的另一种写法（`pt / style.font_size`），与高级区里的
 *   「字号缩放」输入框共用同一个 state —— 两处永远同步；
 * - 保存 = 本地 1.5s 防抖（与服务端防抖叠加没关系：服务端才是真源）+ 失焦 / Cmd+S 立即存；
 *   **没有任何字段变化时不发 PATCH**（不白涨 revision）。
 *
 * 布局（设计稿 §2.4 的段落详情坞）：头部固定，下面是撑满剩余高度的「坞」——原文块定高可滚，
 * 译文框吃掉剩余高度；样式 / 高级参数 / 按钮跟在坞后面，整体超出面板时才滚动。
 *
 * 冲突分支（服务端错误码，不匹配 message 文案）：
 * - `revision_conflict`（两个标签页）→ 提示 + 「刷新草稿」；
 * - `document_busy`（活动 job 期间草稿只读）→ 提示「编译中，稍后再试」；
 * - `draft_invalid` → 字段/范围不合法 + `detail.errors` 原文。
 *
 * 面板下半部分的「AI 重译候选」是 W11（api.md §3.6，见 `CandidatePanel`）：候选未采用之前
 * 不改译文/草稿/产物，采用后走的是**服务端**写草稿那一条链（这里只把返回的新草稿写进缓存）。
 */
import { useEffect, useMemo, useRef, useState } from 'react';

import type { FontFamilyItem, ParagraphItem } from '../../api/types';
import { ApiError, describeApiError } from '../../lib/api';
import {
  LAYOUT_FIELDS,
  STYLE_FIELDS,
  boxSummary,
  draftParagraphOf,
  hasLayoutOverride,
  layoutBool,
  layoutBox,
  layoutFontFamily,
  layoutInputsOf,
  layoutNumber,
  layoutPatch,
  layoutValuesOf,
  paragraphTargetView,
  restoreEntry,
  styleBoolsOf,
  targetPatch,
  validateLayoutInput,
  type LayoutFieldKey,
  type LayoutInputs,
  type StyleBools,
  type StyleFieldKey,
} from '../../lib/draft';
import { layoutBoxOfRow } from '../../lib/preview';
import {
  useCompileBlockMutation,
  useDraft,
  useFonts,
  useParagraphs,
  usePatchDraftMutation,
} from '../../lib/queries';
import { useUiStore } from '../../stores/ui';
import { Button } from '../ui/Button';
import { Chip } from '../ui/Chip';
import { ErrorCard } from '../ui/ErrorCard';
import { Tooltip } from '../ui/Tooltip';
import { CandidatePanel } from './CandidatePanel';

/** 本地自动保存防抖（与服务端 1.5s 防抖同量级；服务端才是真源）。 */
export const SAVE_DEBOUNCE_MS = 1_500;

/** 「字号」pt 下拉的常见档位（值是 pt；换算见 `scaleForFontSize`）。 */
const PRESET_FONT_SIZES = [8, 9, 9.5, 10, 10.5, 11, 12, 14, 15, 16, 18, 20, 22, 24] as const;

export interface ParagraphEditorProps {
  did: string;
  /** 选中段落 id（来自预览点框 / bbox 拖拽）。 */
  paragraphId: string | null;
  /** 只读态：编译进行中或该文档有活动 job（PATCH 也会 409 `document_busy`）。 */
  disabled?: boolean;
  disabledReason?: string;
}

/** 渲染期外的「最新回调」引用：让防抖定时器只依赖输入值，不被回调 identity 重置。 */
function useLatest<T>(value: T) {
  const ref = useRef(value);
  useEffect(() => {
    ref.current = value;
  });
  return ref;
}

export function ParagraphEditor({
  did,
  paragraphId,
  disabled = false,
  disabledReason,
}: ParagraphEditorProps) {
  const paragraphsQuery = useParagraphs(did);
  const draftQuery = useDraft(did);
  const fontsQuery = useFonts();
  const patchMutation = usePatchDraftMutation(did);
  const compileMutation = useCompileBlockMutation(did, paragraphId ?? '');
  // 拖拽编辑只在「版面框（pdf_native）」图层上有意义 → 提示里说清当前图层能不能拖
  const bboxMode = useUiStore((state) => state.bboxMode);
  const draft = draftQuery.data;
  const revision = draft?.revision ?? 0;

  const paragraph: ParagraphItem | null = useMemo(
    () => (paragraphsQuery.data ?? []).find((item) => item.id === paragraphId) ?? null,
    [paragraphsQuery.data, paragraphId],
  );
  const draftParagraph = draftParagraphOf(draft, paragraphId);
  const external = paragraphTargetView(paragraph?.target, draftParagraph);
  const draftLayout = draftParagraph?.layout ?? null;
  /** 字号缩放覆盖（编译样式摘要里有 font_scale 覆盖时显示「× n」）。 */
  const fontScale = layoutNumber(draftLayout, 'font_scale');
  /** 当前生效的框：草稿覆盖优先，否则基线（`GET /paragraphs` 的 layout 几何行）。 */
  const box = layoutBox(draftLayout) ?? layoutBoxOfRow(paragraph?.geometry);
  const modified = typeof draftParagraph?.target === 'string';
  /** 源文字号（`字号` pt 下拉的换算基准；不可用 → 只能走高级区的 font_scale 输入）。 */
  const baseFontSize = paragraph?.style?.font_size;
  const baseSize = typeof baseFontSize === 'number' && Number.isFinite(baseFontSize) ? baseFontSize : null;
  const fontFamilies: FontFamilyItem[] = fontsQuery.data ?? [];

  const [text, setText] = useState(external.target);
  const [inputs, setInputs] = useState<LayoutInputs>(() => layoutInputsOf(draftLayout));
  const [bools, setBools] = useState<StyleBools>(() => styleBoolsOf(draftLayout));
  /** 字体族覆盖：`''` = 跟随默认（补丁里删 `font_family` 键）。 */
  const [fontFamily, setFontFamily] = useState(() => layoutFontFamily(draftLayout) ?? '');

  // 服务端草稿变化（换段 / PATCH 回包 / 刷新）→ 用「外部值」重置本地输入：
  // 键包含外部值，所以用户正在输入时不会被自己的中间态触发重置。
  const externalKey = `${paragraphId ?? ''}\u0000${external.target}\u0000${layoutInputsKey(draftLayout)}\u0000${styleBoolsKey(draftLayout)}\u0000${layoutFontFamily(draftLayout) ?? ''}`;
  const [lastKey, setLastKey] = useState(externalKey);
  if (externalKey !== lastKey) {
    setLastKey(externalKey);
    setText(external.target);
    setInputs(layoutInputsOf(draftLayout));
    setBools(styleBoolsOf(draftLayout));
    setFontFamily(layoutFontFamily(draftLayout) ?? '');
  }

  const errors = useMemo(
    () =>
      LAYOUT_FIELDS.map(
        (field) => [field.key, validateLayoutInput(field.key, inputs[field.key]).error] as const,
      ),
    [inputs],
  );
  const fieldErrors = errors.filter(([, error]) => error !== null);
  const values = useMemo(() => layoutValuesOf(inputs), [inputs]);
  const advancedDirty = layoutChanged(draftLayout, values);
  const dirty =
    text !== external.target ||
    advancedDirty ||
    styleChanged(draftLayout, bools) ||
    fontFamilyChanged(draftLayout, fontFamily);

  // 不用 useCallback：这个函数每次渲染重建即可（只有防抖定时器通过 `saveRef` 取它，
  // 不参与任何依赖数组 —— React Compiler 不允许把「派生的对象」当记忆依赖）。
  const save = (
    nextText: string,
    nextInputs: LayoutInputs,
    nextBools: StyleBools,
    nextFontFamily: string,
  ) => {
    if (paragraphId === null || draft === undefined) return;
    const invalidField = LAYOUT_FIELDS.find(
      (field) => validateLayoutInput(field.key, nextInputs[field.key]).error !== null,
    );
    if (invalidField !== undefined) return; // 有非法值：不保存（服务端也会 422）
    const entry: Record<string, unknown> = {};
    if (nextText !== external.target) entry.target = targetPatch(nextText, paragraph?.target).target;
    const nextValues = layoutValuesOf(nextInputs);
    if (
      layoutChanged(draftLayout, nextValues) ||
      styleChanged(draftLayout, nextBools) ||
      fontFamilyChanged(draftLayout, nextFontFamily)
    ) {
      // layout 是整对象替换：带上草稿里已有的 box 与不认识的键，避免静默丢数据；
      // 样式布尔 / 字体族也走 layoutPatch（空值 = 删键 = 跟随原文/默认）
      entry.layout = layoutPatch(
        nextValues,
        layoutBox(draftLayout),
        draftLayout,
        nextBools,
        nextFontFamily,
      );
    }
    if (Object.keys(entry).length === 0) return;
    patchMutation.mutate({ baseRevision: draft.revision, paragraphs: { [paragraphId]: entry } });
  };
  const saveRef = useLatest(save);

  // 自动保存：本地 1.5s 防抖（只在有改动、无非法值时排定时器）
  useEffect(() => {
    if (disabled || !dirty || fieldErrors.length > 0) return;
    const timer = window.setTimeout(
      () => saveRef.current(text, inputs, bools, fontFamily),
      SAVE_DEBOUNCE_MS,
    );
    return () => window.clearTimeout(timer);
  }, [bools, disabled, dirty, fieldErrors.length, fontFamily, inputs, saveRef, text]);

  if (paragraphId === null) {
    return (
      <div className="p-s5" data-od-id="paragraph-editor-empty">
        <p className="font-serif text-md font-medium leading-[1.4] text-ink-2">段落属性</p>
        <p className="mt-s2 text-tiny text-ink-4">
          点击预览里的段落框查看该段：选中后在这里改译文与排版参数（翻译视图默认叠版面框）。
        </p>
      </div>
    );
  }

  const error = patchMutation.error;
  const conflict = error instanceof ApiError && error.code === 'revision_conflict';
  const busy = error instanceof ApiError && error.code === 'document_busy';
  const invalid = error instanceof ApiError && error.code === 'draft_invalid';
  const described = error === null || error === undefined ? null : describeApiError(error);

  return (
    <div className="flex h-full min-h-0 flex-col" data-od-id="paragraph-editor" data-paragraph-id={paragraphId}>
      <div className="flex flex-none flex-wrap items-center gap-s2 border-b border-hair bg-sand px-s3 py-[6px]">
        <span className="font-mono text-sm text-ink-2" data-od-id="paragraph-editor-id">
          {paragraphId}
        </span>
        {paragraph?.layout_label === null || paragraph?.layout_label === undefined ? null : (
          <span data-od-id="paragraph-editor-label">
            <Chip>{paragraph.layout_label}</Chip>
          </span>
        )}
        {paragraph?.page === null || paragraph?.page === undefined ? null : (
          <span className="font-mono text-micro text-ink-4" data-od-id="paragraph-editor-page">
            第 {paragraph.page} 页
          </span>
        )}
        <span className="ml-auto font-mono text-micro text-ink-4" data-od-id="draft-revision">
          草稿 r{revision}
        </span>
        {modified ? (
          <span data-od-id="paragraph-editor-modified">
            <Chip tone="accent">草稿已修改</Chip>
          </span>
        ) : null}
      </div>

      <div className="flex min-h-0 flex-1 flex-col overflow-auto p-s4">
        {paragraphsQuery.isSuccess && paragraph === null ? (
          <p className="mb-s3 flex-none text-tiny text-ink-4" data-od-id="paragraph-editor-missing">
            该 id 不在段落产物里（可能已被重新解析覆盖）——改选另一段。
          </p>
        ) : null}

        {/* 段落详情坞：原文块定高可滚，译文框吃掉剩余高度（面板整体超出时才滚外层）。 */}
        <div className="flex flex-1 flex-col" data-od-id="paragraph-dock">
          <label className="block flex-none">
            <span className="text-tiny text-ink-3">原文（只读）</span>
            <pre
              data-od-id="paragraph-source"
              className="mt-1 max-h-[150px] overflow-auto whitespace-pre-wrap break-words rounded border border-hair bg-parchment p-s3 font-serif text-sm italic leading-[1.75] text-ink-3"
            >
              {paragraph?.source ?? '（该段没有原文产物）'}
            </pre>
          </label>

          <label className="mt-s4 flex flex-1 flex-col">
            <span className="flex flex-none items-center gap-s2 text-tiny text-ink-3">
              译文
              {disabled ? (
                <Tooltip content={disabledReason ?? '当前不可编辑'}>
                  <span className="font-mono text-micro text-run-ink" data-od-id="paragraph-editor-locked">
                    只读
                  </span>
                </Tooltip>
              ) : null}
            </span>
            <textarea
              data-od-id="paragraph-target"
              aria-label="译文"
              readOnly={disabled}
              value={text}
              onChange={(event) => setText(event.target.value)}
              onBlur={() => {
                if (!disabled) saveRef.current(text, inputs, bools, fontFamily);
              }}
              onKeyDown={(event) => {
                if (!(event.metaKey || event.ctrlKey) || event.key.toLowerCase() !== 's') return;
                event.preventDefault();
                saveRef.current(text, inputs, bools, fontFamily);
              }}
              className="mt-1 min-h-[120px] w-full flex-1 resize-none rounded border border-hair-2 bg-ivory p-s3 font-serif text-body leading-[1.6] text-ink read-only:bg-parchment read-only:text-ink-3"
            />
            <span className="mt-1 flex flex-none items-center gap-s2 font-mono text-micro text-ink-4">
              <span className="min-w-0 flex-1 truncate">
                {disabled
                  ? (disabledReason ?? '当前不可编辑')
                  : patchMutation.isPending
                    ? '正在保存…'
                    : dirty
                      ? '有未保存改动（1.5s 后自动保存，Cmd+S 立即）'
                      : `已保存（r${revision}）`}
              </span>
              <span data-od-id="paragraph-target-count">{characterCount(text)} 字</span>
            </span>
          </label>
        </div>

        <div className="mt-s4 flex-none border-t border-hair pt-s3" data-od-id="paragraph-style">
          <p className="text-tiny text-ink-3">编译样式（源文派生 + 覆盖；本次局部编译生效）</p>
          {paragraph?.style == null ? (
            <p className="mt-2 text-tiny text-ink-4" data-od-id="paragraph-style-unavailable">
              样式信息不可用（该段没有解析状态派生的样式摘要）。
            </p>
          ) : (
            <>
              <p className="mt-2 font-mono text-micro text-ink-4" data-od-id="paragraph-style-info">
                字号 {fontSizeLabel(paragraph.style.font_size)}
                {fontScale === null ? '' : ` × ${fontScale}`}
                {' · '}
                {paragraph.style.font_name ?? '字体未知'} · 加粗 {sourceBoolLabel(paragraph.style.bold)} ·
                斜体 {sourceBoolLabel(paragraph.style.italic)} · 衬线 {sourceBoolLabel(paragraph.style.serif)}
              </p>
              <div className="mt-2 flex flex-col gap-[6px]">
                <FontFamilyRow
                  value={fontFamily}
                  families={fontFamilies}
                  listFailed={fontsQuery.isError}
                  disabled={disabled}
                  dirty={fontFamilyChanged(draftLayout, fontFamily)}
                  onChange={setFontFamily}
                />
                <FontSizeRow
                  base={baseSize}
                  scale={inputs.font_scale}
                  disabled={disabled}
                  dirty={layoutNumber(draftLayout, 'font_scale') !== values.font_scale}
                  error={validateLayoutInput('font_scale', inputs.font_scale).error}
                  onChange={(next) => setInputs((current) => ({ ...current, font_scale: next }))}
                />
                {STYLE_FIELDS.map((field) => (
                  <StyleRow
                    key={field.key}
                    fieldKey={field.key}
                    label={field.label}
                    value={bools[field.key] ?? null}
                    sourceLabel={sourceBoolLabel(paragraph.style?.[field.key])}
                    disabled={disabled}
                    dirty={layoutBool(draftLayout, field.key) !== (bools[field.key] ?? null)}
                    onChange={(next) =>
                      setBools((current) => {
                        const updated = { ...current };
                        if (next === null) delete updated[field.key];
                        else updated[field.key] = next;
                        return updated;
                      })
                    }
                  />
                ))}
              </div>
            </>
          )}
        </div>

        <details className="mt-s4 flex-none border-t border-hair pt-s3" data-od-id="paragraph-advanced">
          <summary className="cursor-pointer select-none text-tiny text-ink-3">
            高级排版参数（数值覆盖，留空 = 用默认）{advancedDirty ? '·有改动' : ''}
          </summary>
          <div className="mt-2 flex flex-col gap-[6px]">
            {LAYOUT_FIELDS.map((field) => (
              <LayoutRow
                key={field.key}
                fieldKey={field.key}
                label={field.label}
                min={field.min}
                max={field.max}
                value={inputs[field.key]}
                error={validateLayoutInput(field.key, inputs[field.key]).error}
                disabled={disabled}
                dirty={layoutNumber(draftLayout, field.key) !== values[field.key]}
                onChange={(next) => setInputs((current) => ({ ...current, [field.key]: next }))}
                onBlur={() => saveRef.current(text, inputs, bools, fontFamily)}
              />
            ))}
          </div>
          <p className="mt-s3 text-tiny text-ink-4" data-od-id="paragraph-box">
            box（PDF y 向上）: <span className="font-mono">{boxSummary(box)}</span> ——{' '}
            {bboxMode === 'layout'
              ? '在预览中拖拽段落框的 8 个手柄调整'
              : '把预览的 bbox 图层切到「版面框」就能在预览里拖拽调整'}
            （无需在这里输入）。
          </p>
        </details>

        <div className="mt-s4 flex flex-none flex-wrap items-center gap-s2">
          <Button
            data-od-id="paragraph-save"
            disabled={disabled || !dirty || fieldErrors.length > 0 || patchMutation.isPending}
            onClick={() => saveRef.current(text, inputs, bools, fontFamily)}
          >
            保存
          </Button>
          <Button
            data-od-id="paragraph-compile"
            disabled={disabled || paragraph === null || dirty || patchMutation.isPending || compileMutation.isPending}
            onClick={() => compileMutation.mutate(revision)}
          >
            {compileMutation.isPending ? '正在提交编译…' : '编译此块'}
          </Button>
          {compileMutation.error ? (
            <span className="text-micro text-err" data-od-id="paragraph-compile-error">
              {describeApiError(compileMutation.error).message}
            </span>
          ) : null}
          {modified || hasLayoutOverride(draftLayout) ? (
            <Button
              data-od-id="paragraph-restore"
              variant="danger"
              disabled={disabled || patchMutation.isPending}
              onClick={() =>
                patchMutation.mutate({
                  baseRevision: revision,
                  paragraphs: { [paragraphId]: restoreEntry(hasLayoutOverride(draftLayout)) },
                })
              }
            >
              恢复基线
            </Button>
          ) : null}
          {conflict ? (
            <Button
              data-od-id="paragraph-refresh-draft"
              onClick={() => {
                patchMutation.reset();
                void draftQuery.refetch();
              }}
            >
              刷新草稿
            </Button>
          ) : null}
        </div>

        {conflict ? (
          <ErrorCard
            className="mt-s3"
            data-od-id="paragraph-conflict"
            title="草稿已被其它会话改动"
            message={`当前草稿 r${revision}。刷新草稿后重做这一步（乐观并发：base_revision 不匹配就不写）。`}
          />
        ) : null}
        {busy ? (
          <ErrorCard
            className="mt-s3"
            data-od-id="paragraph-busy"
            title="编译中，稍后再试"
            message={`活动任务期间草稿只读（服务端 409 document_busy）。${described?.message ?? ''}`}
          />
        ) : null}
        {invalid ? (
          <ErrorCard
            className="mt-s3"
            data-od-id="paragraph-invalid"
            title="草稿字段不合法"
            message={described?.detail ?? described?.message ?? ''}
          />
        ) : null}
        {error !== null && error !== undefined && !conflict && !busy && !invalid ? (
          <ErrorCard className="mt-s3" data-od-id="paragraph-save-error" error={error} title="保存失败" />
        ) : null}

        <CandidatePanel
          did={did}
          pid={paragraphId}
          currentTarget={external.target}
          baselineTarget={paragraph?.target ?? null}
          disabled={disabled}
          disabledReason={disabledReason}
        />
      </div>
    </div>
  );
}

/** 草稿 layout 的稳定指纹（键顺序无关）：换草稿时用它决定要不要重置输入框。 */
function layoutInputsKey(layout: Record<string, unknown> | null | undefined): string {
  if (layout === null || layout === undefined) return '';
  return LAYOUT_FIELDS.map((field) => `${field.key}=${layoutNumber(layout, field.key) ?? ''}`).join(',');
}

/** 草稿样式布尔的稳定指纹（与 `layoutInputsKey` 同一口径，拼进外部键）。 */
function styleBoolsKey(layout: Record<string, unknown> | null | undefined): string {
  if (layout === null || layout === undefined) return '';
  return STYLE_FIELDS.map((field) => `${field.key}=${layoutBool(layout, field.key) ?? ''}`).join(',');
}

/** 用户输入的排版数值是否与草稿不同（缺省 = 没有覆盖，不是 0）。 */
function layoutChanged(
  layout: Record<string, unknown> | null | undefined,
  values: Partial<Record<LayoutFieldKey, number>>,
): boolean {
  return LAYOUT_FIELDS.some(
    (field) => (layoutNumber(layout, field.key) ?? null) !== (values[field.key] ?? null),
  );
}

/** 样式布尔是否与草稿不同（本地状态里没有的键 = 跟随原文 = 草稿里也没有）。 */
function styleChanged(layout: Record<string, unknown> | null | undefined, bools: StyleBools): boolean {
  return STYLE_FIELDS.some(
    (field) => (layoutBool(layout, field.key) ?? null) !== (bools[field.key] ?? null),
  );
}

/** 本地字体族是否与草稿不同（`''` = 跟随默认 = 草稿里删键）。 */
function fontFamilyChanged(
  layout: Record<string, unknown> | null | undefined,
  fontFamily: string,
): boolean {
  return (layoutFontFamily(layout) ?? '') !== fontFamily;
}

/** 源文字号文本（拿不到 → `—`，不编造默认值）。 */
function fontSizeLabel(size: number | null | undefined): string {
  return typeof size === 'number' && Number.isFinite(size) ? `${size}pt` : '—';
}

/** 源文派生的布尔文本：true → 是 / false → 否 / 缺 → —。 */
function sourceBoolLabel(value: boolean | undefined): string {
  if (value === true) return '是';
  if (value === false) return '否';
  return '—';
}

/** 译文框下方的字数（去空白字符数，与设计稿 ed-meta 同口径）。 */
function characterCount(text: string): number {
  return text.replace(/\s/g, '').length;
}

/** 目标 pt → `font_scale` 输入文本（`pt / 源文字号`；不四舍五入，保住回显时的档位对应）。 */
function scaleForFontSize(size: number, base: number): string {
  return String(size / base);
}

/** `font_scale` → 最接近的档位（用于 pt 下拉回显；档位表见 `PRESET_FONT_SIZES`）。 */
function nearestFontSize(size: number): number {
  return PRESET_FONT_SIZES.reduce((best, candidate) =>
    Math.abs(candidate - size) < Math.abs(best - size) ? candidate : best,
  );
}

function LayoutRow({
  fieldKey,
  label,
  min,
  max,
  value,
  error,
  disabled,
  dirty,
  onChange,
  onBlur,
}: {
  fieldKey: LayoutFieldKey;
  label: string;
  min: number;
  max: number;
  value: string;
  error: string | null;
  disabled: boolean;
  dirty: boolean;
  onChange: (next: string) => void;
  /** 失焦即存（与译文 textarea 同一口径）；无改动时 `save` 自己会跳过 */
  onBlur: () => void;
}) {
  return (
    <label className="grid grid-cols-[84px_1fr_auto] items-center gap-s2">
      <span className="font-mono text-micro text-ink-3" title={`${fieldKey}（${min}–${max}）`}>
        {label}
      </span>
      <input
        data-od-id={`paragraph-layout-${fieldKey}`}
        aria-label={`${label}（${min}–${max}）`}
        type="number"
        step="0.05"
        min={min}
        max={max}
        disabled={disabled}
        value={value}
        placeholder="—"
        onChange={(event) => onChange(event.target.value)}
        onBlur={onBlur}
        className="h-6 w-full rounded border border-hair-2 bg-ivory px-[6px] font-mono text-tiny text-ink [font-variant-numeric:tabular-nums] disabled:opacity-45"
      />
      <span className={dirty ? 'font-mono text-micro text-accent' : 'font-mono text-micro text-ink-4'}>
        {error ?? (dirty ? '已改' : `${min}–${max}`)}
      </span>
    </label>
  );
}

/**
 * 段落级字体族覆盖：`跟随默认`（值 `''`，补丁里删 `font_family` 键）或 `GET /fonts` 里的一个
 * family id。`available=false` 的族**不可选**（字体文件本机没有，选了后端也回落默认族）；
 * 清单加载失败 → 只留「跟随默认」+ 行内提示（译文编辑不受影响）。
 */
function FontFamilyRow({
  value,
  families,
  listFailed,
  disabled,
  dirty,
  onChange,
}: {
  /** 当前覆盖的 family id；`''` = 跟随默认。 */
  value: string;
  families: FontFamilyItem[];
  listFailed: boolean;
  disabled: boolean;
  dirty: boolean;
  onChange: (next: string) => void;
}) {
  // 草稿里的族不在清单里（换机器 / 族被摘掉）：照样回显并保留，不然会静默显示成「跟随默认」
  const missing = value !== '' && !families.some((family) => family.id === value);
  return (
    <label className="grid grid-cols-[84px_1fr_auto] items-center gap-s2 gap-y-1">
      <span className="font-mono text-micro text-ink-3" title="font_family 覆盖（跟随默认 = 删键）">
        字体族
      </span>
      <select
        data-od-id="paragraph-style-font-family"
        aria-label="字体族覆盖"
        disabled={disabled}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        className="h-6 w-full rounded border border-hair-2 bg-ivory px-[6px] font-mono text-tiny text-ink disabled:opacity-45"
      >
        <option value="">跟随默认</option>
        {missing ? <option value={value}>{value}（不在清单里）</option> : null}
        {families.map((family) => (
          <option key={family.id} value={family.id} disabled={!family.available}>
            {family.available ? family.label : `${family.label}（不可用）`}
          </option>
        ))}
      </select>
      <span className={dirty ? 'font-mono text-micro text-accent' : 'font-mono text-micro text-ink-4'}>
        {listFailed ? '清单不可用' : dirty ? '已改' : '默认'}
      </span>
      {listFailed ? (
        <span
          className="col-span-3 font-mono text-micro text-warn-ink"
          data-od-id="paragraph-style-font-family-error"
        >
          字体清单不可用（/fonts）
        </span>
      ) : null}
    </label>
  );
}

/**
 * 「字号」pt 下拉：`font_scale` 的另一种写法，和高级区里的「字号缩放」输入框共用同一个 state。
 * 基准是源文字号 `style.font_size`：`跟随原文（{base}pt）` = 删 `font_scale` 键；
 * 选档位 pt → `font_scale = pt / base`。base 拿不到 → 整行禁用（只能用 font_scale 输入）。
 */
function FontSizeRow({
  base,
  scale,
  error,
  disabled,
  dirty,
  onChange,
}: {
  /** 源文字号 pt；null = 不可用（整行禁用）。 */
  base: number | null;
  /** 当前 `font_scale` 输入文本（`''` = 没有覆盖）。 */
  scale: string;
  error: string | null;
  disabled: boolean;
  dirty: boolean;
  onChange: (next: string) => void;
}) {
  const target = base === null ? null : scale.trim() === '' ? null : base * Number(scale);
  const value =
    target === null || !Number.isFinite(target) ? '' : String(nearestFontSize(target));
  return (
    <label className="grid grid-cols-[84px_1fr_auto] items-center gap-s2 gap-y-1">
      <span className="font-mono text-micro text-ink-3" title="字号覆盖（跟随原文 = 删 font_scale 键）">
        字号
      </span>
      <select
        data-od-id="paragraph-style-font-size"
        aria-label="字号覆盖"
        disabled={disabled || base === null}
        value={value}
        onChange={(event) => {
          const next = event.target.value;
          if (next === '' || base === null) onChange('');
          else onChange(scaleForFontSize(Number(next), base));
        }}
        className="h-6 w-full rounded border border-hair-2 bg-ivory px-[6px] font-mono text-tiny text-ink disabled:opacity-45"
      >
        <option value="">{base === null ? '跟随原文' : `跟随原文（${base}pt）`}</option>
        {PRESET_FONT_SIZES.map((size) => (
          <option key={size} value={String(size)}>
            {size}pt
          </option>
        ))}
      </select>
      <span className={dirty ? 'font-mono text-micro text-accent' : 'font-mono text-micro text-ink-4'}>
        {error ?? (dirty ? '已改' : base === null ? '不可用' : `原文 ${base}pt`)}
      </span>
      {base === null ? (
        <span
          className="col-span-3 font-mono text-micro text-warn-ink"
          data-od-id="paragraph-style-font-size-unavailable"
        >
          源文字号不可用，按 pt 选字号不可用（可在下方「高级排版参数」里改字号缩放）。
        </span>
      ) : null}
    </label>
  );
}

/**
 * 三态样式覆盖行：`跟随原文`（值 null，草稿里删键） / `开启`（true） / `关闭`（false）。
 * 变更只进本地状态，保存走与数值字段同一条防抖链路（select 没有有意义的失焦语义）。
 */
function StyleRow({
  fieldKey,
  label,
  value,
  sourceLabel,
  disabled,
  dirty,
  onChange,
}: {
  fieldKey: StyleFieldKey;
  label: string;
  /** 当前覆盖值：null = 跟随原文（草稿里没有该键）。 */
  value: boolean | null;
  /** 源文派生值（只读行同款文本，右侧提示用）。 */
  sourceLabel: string;
  disabled: boolean;
  dirty: boolean;
  onChange: (next: boolean | null) => void;
}) {
  return (
    <label className="grid grid-cols-[84px_1fr_auto] items-center gap-s2">
      <span className="font-mono text-micro text-ink-3" title={`${label}覆盖（跟随原文 = 用源文样式）`}>
        {label}
      </span>
      <select
        data-od-id={`paragraph-style-${fieldKey}`}
        aria-label={`${label}覆盖`}
        disabled={disabled}
        value={value === null ? '' : value ? 'on' : 'off'}
        onChange={(event) => {
          const next = event.target.value;
          onChange(next === '' ? null : next === 'on');
        }}
        className="h-6 w-full rounded border border-hair-2 bg-ivory px-[6px] font-mono text-tiny text-ink disabled:opacity-45"
      >
        <option value="">跟随原文</option>
        <option value="on">开启</option>
        <option value="off">关闭</option>
      </select>
      <span className={dirty ? 'font-mono text-micro text-accent' : 'font-mono text-micro text-ink-4'}>
        {dirty ? '已改' : `原文 ${sourceLabel}`}
      </span>
    </label>
  );
}
