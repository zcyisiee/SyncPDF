/**
 * 预览工具条：翻页、缩放、源/译/对照、bbox 图层与下载。
 *
 * 单行不换行（`flex-nowrap` + `overflow-x-auto`），各组 `flex-none`，宽度不够时整条横向滚动：
 * 翻页组（‹ 页码 ›）+ 缩放组（− 百分比 ＋ 适宽）+ 模式分段 + bbox 分段 + 最右的下载槽。
 * 触控板捏合 / Ctrl(⌘)+滚轮 仍直接缩放（`ContinuousPdfPane` 的 wheel/gesture 监听），
 * 工具条上的 −/＋/适宽 是同一份 `previewZoom` 的显式入口（`null` = 适宽）。
 *
 * 按钮文案保持简洁，把完整解释放进 tooltip（悬停才展开）：
 * - 「原文/译文/对照」是分段控件，语义自明；
 * - bbox 图层用「原文框 / 译文框 / 关」（识别 IR 框 = 原文侧，套版几何框 = 译文侧），悬停给完整解释；
 * - 下载按钮由 `DownloadButton` 提供（修订号 + 质量徽标 + 悬停说明）。
 */
import { useState } from 'react';
import type { ReactNode } from 'react';

import { clampPage } from '../../lib/preview';
import type { BboxMode } from '../../lib/preview';
import { cn } from '../../lib/cn';
import { useUiStore } from '../../stores/ui';
import type { PreviewMode } from '../../stores/ui';
import { Icon } from '../icons';
import { Button } from '../ui/Button';
import { Tooltip } from '../ui/Tooltip';

interface SegmentedOption<T extends string> {
  value: T;
  label: string;
  /** 悬停时的详细说明（简洁文案说不清的部分）。 */
  title?: string;
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
      className="inline-flex flex-none items-center gap-[2px] rounded-card bg-sand p-[2px]"
    >
      {options.map((option) => {
        const active = option.value === value;
        const button = (
          <button
            key={option.value}
            type="button"
            aria-pressed={active}
            disabled={option.disabled === true}
            title={option.title}
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
  { value: 'source', label: '原文', title: '只看上传的原文 PDF（可叠原文框）' },
  { value: 'target', label: '译文', title: '只看当前编译出的译文 PDF' },
  { value: 'compare', label: '对照', title: '原文与译文并排；左右可联动滚动' },
] as const satisfies readonly SegmentedOption<PreviewMode>[];

const BBOX_MODE_OPTIONS = [
  { value: 'parse', label: '原文框', title: '叠加原文侧识别出的段落框（点击可选中段落）' },
  { value: 'layout', label: '译文框', title: '叠加译文侧套版几何框；选中后可拖拽调整' },
  { value: 'off', label: '关', title: '不显示任何框，只看干净页面' },
] as const satisfies readonly SegmentedOption<BboxMode>[];

export interface PreviewToolbarProps {
  page: number;
  pageCount: number;
  /** 源 PDF 不可用（workdir 没有 source.pdf）→ 原文模式禁用。 */
  sourceAvailable: boolean;
  /** 有可渲染 PDF 才允许翻页。 */
  paged: boolean;
  onPageChange: (page: number) => void;
  onBboxModeChange: (mode: BboxMode) => void;
  /** 工具条最右侧的下载按钮槽（W10：修订号 + 质量徽标，见 `DownloadButton`）。 */
  download?: ReactNode;
  className?: string;
}

export function PreviewToolbar({
  page,
  pageCount,
  sourceAvailable,
  paged,
  onPageChange,
  onBboxModeChange,
  download,
  className,
}: PreviewToolbarProps) {
  const previewMode = useUiStore((state) => state.previewMode);
  const setPreviewMode = useUiStore((state) => state.setPreviewMode);
  const bboxMode = useUiStore((state) => state.bboxMode);
  const linked = useUiStore((state) => state.compareLinked);
  const setLinked = useUiStore((state) => state.setCompareLinked);
  const zoom = useUiStore((state) => state.previewZoom);
  const setPreviewZoom = useUiStore((state) => state.setPreviewZoom);
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

  /** ± 一步 ×1.25 / ÷1.25；适宽（null）时从 1 起步。store 负责 clamp 0.1–4。 */
  const stepZoom = (factor: number) => setPreviewZoom((zoom ?? 1) * factor);

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
        'flex min-h-10 flex-nowrap flex-none items-center gap-s3 overflow-x-auto border-b border-hair bg-ivory px-s5 py-s1',
        className,
      )}
    >
      <div
        role="group"
        aria-label="翻页"
        data-od-id="page-nav"
        className="flex flex-none items-center gap-[2px] text-tiny text-ink-4"
      >
        <Button
          variant="ghost"
          size="icon"
          aria-label="上一页"
          title="上一页"
          disabled={!paged || page <= 1}
          onClick={() => onPageChange(clampPage(page - 1, pageCount))}
        >
          <Icon name="chevronLeft" />
        </Button>
        <input
          aria-label="页码"
          type="number"
          min={1}
          max={pageCount}
          inputMode="numeric"
          disabled={!paged}
          title="输入页码后回车跳页；连续翻页用滚动"
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
        <span data-od-id="page-count" className="whitespace-nowrap">
          / {pageCount} 页
        </span>
        <Button
          variant="ghost"
          size="icon"
          aria-label="下一页"
          title="下一页"
          disabled={!paged || page >= pageCount}
          onClick={() => onPageChange(clampPage(page + 1, pageCount))}
        >
          <Icon name="chevronRight" />
        </Button>
      </div>
      <div
        role="group"
        aria-label="缩放"
        data-od-id="zoom-controls"
        className="flex flex-none items-center gap-[2px]"
      >
        <Button variant="ghost" size="icon" aria-label="缩小" title="缩小" onClick={() => stepZoom(1 / 1.25)}>
          <Icon name="minus" />
        </Button>
        <span
          data-od-id="zoom-level"
          className="w-11 text-center font-mono text-tiny text-ink [font-variant-numeric:tabular-nums]"
        >
          {zoom === null ? '适宽' : `${Math.round(zoom * 100)}%`}
        </span>
        <Button variant="ghost" size="icon" aria-label="放大" title="放大" onClick={() => stepZoom(1.25)}>
          <Icon name="plus" />
        </Button>
        <Button
          variant="ghost"
          size="icon"
          aria-label="适合宽度"
          aria-pressed={zoom === null}
          title="适合宽度（清除缩放，按容器宽自适应）"
          onClick={() => setPreviewZoom(null)}
        >
          <Icon name="fitWidth" />
        </Button>
      </div>
      <SegmentedGroup
        label="预览模式"
        options={modeOptions}
        value={previewMode}
        onChange={setPreviewMode}
      />
      <SegmentedGroup label="bbox 图层" options={BBOX_MODE_OPTIONS} value={bboxMode} onChange={onBboxModeChange} />
      {previewMode === 'compare' ? (
        <Tooltip content="对照模式下左右两栏一起滚动">
          <Button size="sm" aria-pressed={linked} onClick={() => setLinked(!linked)} className="flex-none">
            {linked ? '解除联动' : '联动阅读'}
          </Button>
        </Tooltip>
      ) : null}
      <div className="ml-auto flex flex-none items-center gap-s2">{download}</div>
    </div>
  );
}
