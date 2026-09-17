/**
 * 右侧面板「段落」tab：选中段的译文编辑 + 排版参数（api.md §3.3 的草稿写入口）。
 *
 * 数据口径：
 * - 译文 = **草稿覆盖优先**于 `GET /paragraphs` 的基线（`translated.jsonl`），
 *   草稿里有覆盖 → 顶部「草稿已修改」chip + 「恢复」按钮（PATCH `target: null`）；
 * - 排版 = 四个数值覆盖（范围同 `layout_overrides.PARAGRAPH_FLOAT_KEYS`）+ 只读 `box`
 *   （`box` 只能在预览里拖拽，见 `BboxEditor`）；
 * - 保存 = 本地 1.5s 防抖（与服务端防抖叠加没关系：服务端才是真源）+ 失焦 / Cmd+S 立即存；
 *   **没有任何字段变化时不发 PATCH**（不白涨 revision）。
 *
 * 冲突分支（服务端错误码，不匹配 message 文案）：
 * - `revision_conflict`（两个标签页）→ 提示 + 「刷新草稿」；
 * - `document_busy`（活动 job 期间草稿只读）→ 提示「编译中，稍后再试」；
 * - `draft_invalid` → 字段/范围不合法 + `detail.errors` 原文。
 */
import { useEffect, useMemo, useRef, useState } from 'react';

import type { ParagraphItem } from '../../api/types';
import { ApiError, describeApiError } from '../../lib/api';
import {
  LAYOUT_FIELDS,
  boxSummary,
  draftParagraphOf,
  hasLayoutOverride,
  layoutBox,
  layoutInputsOf,
  layoutNumber,
  layoutPatch,
  layoutValuesOf,
  paragraphTargetView,
  restoreEntry,
  targetPatch,
  validateLayoutInput,
  type LayoutFieldKey,
  type LayoutInputs,
} from '../../lib/draft';
import { layoutBoxOfRow } from '../../lib/preview';
import { useDraft, useParagraphs, usePatchDraftMutation } from '../../lib/queries';
import { useUiStore } from '../../stores/ui';
import { Button } from '../ui/Button';
import { Chip } from '../ui/Chip';
import { ErrorCard } from '../ui/ErrorCard';
import { Tooltip } from '../ui/Tooltip';

/** 本地自动保存防抖（与服务端 1.5s 防抖同量级；服务端才是真源）。 */
export const SAVE_DEBOUNCE_MS = 1_500;

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
  const patchMutation = usePatchDraftMutation(did);
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
  /** 当前生效的框：草稿覆盖优先，否则基线（`GET /paragraphs` 的 layout 几何行）。 */
  const box = layoutBox(draftLayout) ?? layoutBoxOfRow(paragraph?.geometry);
  const modified = typeof draftParagraph?.target === 'string';

  const [text, setText] = useState(external.target);
  const [inputs, setInputs] = useState<LayoutInputs>(() => layoutInputsOf(draftLayout));

  // 服务端草稿变化（换段 / PATCH 回包 / 刷新）→ 用「外部值」重置本地输入：
  // 键包含外部值，所以用户正在输入时不会被自己的中间态触发重置。
  const externalKey = `${paragraphId ?? ''}\u0000${external.target}\u0000${layoutInputsKey(draftLayout)}`;
  const [lastKey, setLastKey] = useState(externalKey);
  if (externalKey !== lastKey) {
    setLastKey(externalKey);
    setText(external.target);
    setInputs(layoutInputsOf(draftLayout));
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
  const dirty = text !== external.target || layoutChanged(draftLayout, values);

  // 不用 useCallback：这个函数每次渲染重建即可（只有防抖定时器通过 `saveRef` 取它，
  // 不参与任何依赖数组 —— React Compiler 不允许把「派生的对象」当记忆依赖）。
  const save = (nextText: string, nextInputs: LayoutInputs) => {
    if (paragraphId === null || draft === undefined) return;
    const invalidField = LAYOUT_FIELDS.find(
      (field) => validateLayoutInput(field.key, nextInputs[field.key]).error !== null,
    );
    if (invalidField !== undefined) return; // 有非法值：不保存（服务端也会 422）
    const entry: Record<string, unknown> = {};
    if (nextText !== external.target) entry.target = targetPatch(nextText, paragraph?.target).target;
    const nextValues = layoutValuesOf(nextInputs);
    if (layoutChanged(draftLayout, nextValues)) {
      // layout 是整对象替换：带上草稿里已有的 box 与不认识的键，避免静默丢数据
      entry.layout = layoutPatch(nextValues, layoutBox(draftLayout), draftLayout);
    }
    if (Object.keys(entry).length === 0) return;
    patchMutation.mutate({ baseRevision: draft.revision, paragraphs: { [paragraphId]: entry } });
  };
  const saveRef = useLatest(save);

  // 自动保存：本地 1.5s 防抖（只在有改动、无非法值时排定时器）
  useEffect(() => {
    if (disabled || !dirty || fieldErrors.length > 0) return;
    const timer = window.setTimeout(() => saveRef.current(text, inputs), SAVE_DEBOUNCE_MS);
    return () => window.clearTimeout(timer);
  }, [disabled, dirty, fieldErrors.length, inputs, saveRef, text]);

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
        <span className="ml-auto font-mono text-micro text-ink-4" data-od-id="draft-revision">
          草稿 r{revision}
        </span>
        {modified ? (
          <span data-od-id="paragraph-editor-modified">
            <Chip tone="accent">草稿已修改</Chip>
          </span>
        ) : null}
      </div>

      <div className="min-h-0 flex-1 overflow-auto p-s4">
        {paragraphsQuery.isSuccess && paragraph === null ? (
          <p className="mb-s3 text-tiny text-ink-4" data-od-id="paragraph-editor-missing">
            该 id 不在段落产物里（可能已被重新解析覆盖）——改选另一段。
          </p>
        ) : null}

        <label className="block">
          <span className="text-tiny text-ink-3">原文（只读）</span>
          <pre
            data-od-id="paragraph-source"
            className="mt-1 max-h-[132px] overflow-auto whitespace-pre-wrap break-words rounded border border-hair bg-parchment p-s3 font-mono text-micro leading-[1.5] text-ink-3"
          >
            {paragraph?.source ?? '（该段没有原文产物）'}
          </pre>
        </label>

        <label className="mt-s4 block">
          <span className="flex items-center gap-s2 text-tiny text-ink-3">
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
            rows={6}
            readOnly={disabled}
            value={text}
            onChange={(event) => setText(event.target.value)}
            onBlur={() => {
              if (!disabled) saveRef.current(text, inputs);
            }}
            onKeyDown={(event) => {
              if (!(event.metaKey || event.ctrlKey) || event.key.toLowerCase() !== 's') return;
              event.preventDefault();
              saveRef.current(text, inputs);
            }}
            className="mt-1 w-full resize-y rounded border border-hair-2 bg-ivory p-s3 font-serif text-body leading-[1.6] text-ink read-only:bg-parchment read-only:text-ink-3"
          />
          <span className="mt-1 flex items-center gap-s2 font-mono text-micro text-ink-4">
            {disabled
              ? (disabledReason ?? '当前不可编辑')
              : patchMutation.isPending
                ? '正在保存…'
                : dirty
                  ? '有未保存改动（1.5s 后自动保存，Cmd+S 立即）'
                  : `已保存（r${revision}）`}
          </span>
        </label>

        <div className="mt-s4 border-t border-hair pt-s3">
          <p className="text-tiny text-ink-3">排版参数（覆盖，留空 = 用默认）</p>
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
                onBlur={() => saveRef.current(text, inputs)}
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
        </div>

        <div className="mt-s4 flex flex-wrap items-center gap-s2">
          <Button
            data-od-id="paragraph-save"
            disabled={disabled || !dirty || fieldErrors.length > 0 || patchMutation.isPending}
            onClick={() => saveRef.current(text, inputs)}
          >
            保存
          </Button>
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
      </div>
    </div>
  );
}

/** 草稿 layout 的稳定指纹（键顺序无关）：换草稿时用它决定要不要重置输入框。 */
function layoutInputsKey(layout: Record<string, unknown> | null | undefined): string {
  if (layout === null || layout === undefined) return '';
  return LAYOUT_FIELDS.map((field) => `${field.key}=${layoutNumber(layout, field.key) ?? ''}`).join(',');
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
