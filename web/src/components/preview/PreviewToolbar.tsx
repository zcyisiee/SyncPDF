/**
 * 预览工具条（§3 内部栅格：44px）：源/译/对照三模式 + 页码导航 + 适宽缩放读数 + bbox 图层三态。
 * 缩放控件与连续滚动不在本任务（适宽固定），所以 zoom 只读。
 */
import { useState } from 'react';

import { clampPage } from '../../lib/preview';
import type { BboxMode } from '../../lib/preview';
import { cn } from '../../lib/cn';
import { useUiStore } from '../../stores/ui';
import type { PreviewMode } from '../../stores/ui';
import { Button } from '../ui/Button';
import { Tooltip } from '../ui/Tooltip';

interface SegmentedOption<T extends string> {
  value: T;
  label: string;
  disabled?: boolean;
  /** 禁用原因（tooltip 文案）。 */
  disabledReason?: string;
}

/** §4.2 Segmented control：轨道 `--sand` + padding 2px + r 6px，激活项 ivory + 1px ring。 */
function SegmentedGroup<T extends string>({
  label,
  options,
  value,
  onChange,
}: {
  label: string;
  options: readonly SegmentedOption<T>[];
  value: T;
  onChange: (value: T) => void;
}) {
  return (
    <div
      role="group"
      aria-label={label}
      className="inline-flex items-center gap-[2px] rounded-card bg-sand p-[2px]"
    >
      {options.map((option) => {
        const active = option.value === value;
        const button = (
          <button
            key={option.value}
            type="button"
            aria-pressed={active}
            disabled={option.disabled === true}
            onClick={() => onChange(option.value)}
            className={cn(
              'h-6 rounded px-[9px] text-tiny leading-none tracking-[0.02em] transition-colors',
              active ? 'bg-ivory text-ink shadow-ring' : 'text-ink-4 hover:text-ink-2',
              !active && option.disabled !== true && 'hover:bg-[color-mix(in_oklch,var(--fg)_6%,transparent)]',
              option.disabled === true && 'cursor-not-allowed opacity-45 hover:text-ink-4',
            )}
          >
            {option.label}
          </button>
        );
        if (option.disabled !== true || option.disabledReason === undefined) return button;
        return (
          <Tooltip key={option.value} content={option.disabledReason} className="align-middle">
            {button}
          </Tooltip>
        );
      })}
    </div>
  );
}

const PREVIEW_MODE_OPTIONS = [
  { value: 'source', label: '原文' },
  { value: 'target', label: '译文' },
  { value: 'compare', label: '对照' },
] as const satisfies readonly SegmentedOption<PreviewMode>[];

const BBOX_MODE_OPTIONS = [
  { value: 'parse', label: '段落框' },
  { value: 'layout', label: '版面框' },
  { value: 'off', label: '关' },
] as const satisfies readonly SegmentedOption<BboxMode>[];

export interface PreviewToolbarProps {
  page: number;
  pageCount: number;
  /** 当前适宽 scale（只读显示）。 */
  scale: number;
  /** 源 PDF 不可用（workdir 没有 source.pdf）→ 原文模式禁用。 */
  sourceAvailable: boolean;
  /** 有可渲染 PDF 才允许翻页。 */
  paged: boolean;
  onPageChange: (page: number) => void;
  onBboxModeChange: (mode: BboxMode) => void;
  className?: string;
}

export function PreviewToolbar({
  page,
  pageCount,
  scale,
  sourceAvailable,
  paged,
  onPageChange,
  onBboxModeChange,
  className,
}: PreviewToolbarProps) {
  const previewMode = useUiStore((state) => state.previewMode);
  const setPreviewMode = useUiStore((state) => state.setPreviewMode);
  const bboxMode = useUiStore((state) => state.bboxMode);
  // 受控输入的外部同步走「渲染期调整 state」（React 官方推荐），不在 effect 里同步 setState
  const [draft, setDraft] = useState(String(page));
  const [lastPage, setLastPage] = useState(page);
  if (page !== lastPage) {
    setLastPage(page);
    setDraft(String(page));
  }

  const commit = () => {
    const parsed = Number.parseInt(draft, 10);
    const next = Number.isFinite(parsed) ? clampPage(parsed, pageCount) : page;
    setDraft(String(next));
    if (next !== page) onPageChange(next);
  };

  const modeOptions: readonly SegmentedOption<PreviewMode>[] = PREVIEW_MODE_OPTIONS.map((option) =>
    option.value === 'source' && !sourceAvailable
      ? {
          ...option,
          disabled: true,
          disabledReason: '该文档没有 source.pdf（原文 PDF 在上传时写入），原文预览不可用',
        }
      : option,
  );

  return (
    <div
      role="toolbar"
      aria-label="预览工具条"
      data-od-id="preview-toolbar"
      className={cn(
        'flex h-11 flex-none items-center gap-s4 border-b border-hair bg-ivory px-s5',
        className,
      )}
    >
      <SegmentedGroup
        label="预览模式"
        options={modeOptions}
        value={previewMode}
        onChange={setPreviewMode}
      />
      <div className="flex items-center gap-s2">
        <Button
          size="sm"
          variant="default"
          aria-label="上一页"
          disabled={!paged || page <= 1}
          onClick={() => onPageChange(page - 1)}
        >
          上一页
        </Button>
        <Button
          size="sm"
          variant="default"
          aria-label="下一页"
          disabled={!paged || page >= pageCount}
          onClick={() => onPageChange(page + 1)}
        >
          下一页
        </Button>
        <label className="flex items-center gap-s2 text-tiny text-ink-4">
          <input
            aria-label="页码"
            type="number"
            min={1}
            max={pageCount}
            inputMode="numeric"
            disabled={!paged}
            value={draft}
            onChange={(event) => setDraft(event.target.value)}
            onBlur={commit}
            onKeyDown={(event) => {
              if (event.key !== 'Enter') return;
              event.preventDefault();
              commit();
            }}
            className="h-6 w-11 rounded border border-hair bg-ivory px-[6px] text-center font-mono text-tiny [font-variant-numeric:tabular-nums] disabled:opacity-45"
          />
          <span data-od-id="page-count">
            / {pageCount} 页
          </span>
        </label>
      </div>
      <span className="ml-auto font-mono text-micro text-ink-4" data-od-id="preview-zoom">
        适宽 · {scale.toFixed(2)}×
      </span>
      <SegmentedGroup
        label="bbox 图层"
        options={BBOX_MODE_OPTIONS}
        value={bboxMode}
        onChange={onBboxModeChange}
      />
    </div>
  );
}
