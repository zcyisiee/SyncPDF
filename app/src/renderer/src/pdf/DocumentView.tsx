/**
 * 打开的论文：工具栏（原文 / 译文 / 双栏、缩放、同步、叠加框与图例）+ PDF 栏。
 * 打开时若标题 / 作者仍来自文件名，用 PDF Info 补上（来源优先级由主进程裁决）。
 */
import { useEffect, useMemo, useState } from 'react';
import type { LibraryDoc, ParagraphRecord } from '@shared/library';
import { REGION_KINDS } from '@shared/protocol';
import { useLibrary, useOpenDocMeta, type OpenDoc } from '@/store/library';
import { stepZoom, useWorkbench, type ViewMode } from '@/store/workbench';
import { IconButton } from '@/layout/Panel';
import { overlayItems, type OverlayItem, type OverlaySide } from './overlay';
import { PdfPane } from './PdfPane';
import { REGION_STYLE } from './regionKinds';
import { usePdfDocument } from './usePdfDocument';

const MODES: Array<{ id: ViewMode; label: string }> = [
  { id: 'source', label: '原文' },
  { id: 'target', label: '译文' },
  { id: 'dual', label: '双栏' },
];

export function DocumentView(): JSX.Element | null {
  const meta = useOpenDocMeta();
  const open = useLibrary((s) => s.open);
  const viewMode = useWorkbench((s) => s.viewMode);
  const hiddenKinds = useWorkbench((s) => s.hiddenKinds);
  usePdfInfoMeta(meta);

  const sourceItems = useMemo(() => groupItems('source', open, hiddenKinds), [open, hiddenKinds]);
  const targetItems = useMemo(() => groupItems('target', open, hiddenKinds), [open, hiddenKinds]);
  if (meta === null || open === null) return null;

  const source = <PdfPane side="source" path={meta.sourcePath} revision={0} itemsByPage={sourceItems} />;
  const target =
    meta.translatedPath === null ? (
      <TargetPlaceholder doc={meta} />
    ) : (
      <PdfPane side="target" path={meta.translatedPath} revision={open.revision} itemsByPage={targetItems} />
    );
  return (
    <div className="sp-document">
      <Toolbar />
      <div className={`sp-panes mode-${viewMode}`}>
        {viewMode !== 'target' && (
          <section className="sp-pane">
            {viewMode === 'dual' && <div className="sp-pane-label">原文</div>}
            {source}
          </section>
        )}
        {viewMode !== 'source' && (
          <section className="sp-pane">
            {viewMode === 'dual' && <div className="sp-pane-label">译文</div>}
            {target}
          </section>
        )}
      </div>
    </div>
  );
}

function groupItems(
  side: OverlaySide,
  open: OpenDoc | null, hiddenKinds: readonly (typeof REGION_KINDS)[number][]): Map<number, OverlayItem[]> {
  const result = new Map<number, OverlayItem[]>();
  if (open === null) return result;
  const paragraphsByPage = new Map<number, ParagraphRecord[]>();
  for (const p of Object.values(open.paragraphs)) {
    const list = paragraphsByPage.get(p.page) ?? [];
    list.push(p);
    paragraphsByPage.set(p.page, list);
  }
  const pages = new Set([...Object.keys(open.layout).map(Number), ...paragraphsByPage.keys()]);
  for (const page of pages) {
    result.set(page, overlayItems(side, open.layout[page] ?? [], paragraphsByPage.get(page) ?? [], hiddenKinds));
  }
  return result;
}

function Toolbar(): JSX.Element {
  const { viewMode, setViewMode, sync, setSync, zoom, setZoom, showBoxes, setShowBoxes } = useWorkbench();
  const [legendOpen, setLegendOpen] = useState(false);
  const zoomAll = (value: number | 'fit-width'): void => {
    setZoom('source', value);
    if (!sync) setZoom('target', value);
  };
  const current = zoom.source;
  return (
    <div className="sp-toolbar">
      <div className="sp-segmented" role="radiogroup" aria-label="视图">
        {MODES.map((mode) => (
          <button
            key={mode.id}
            type="button"
            role="radio"
            aria-checked={viewMode === mode.id}
            className={viewMode === mode.id ? 'is-active' : ''}
            onClick={() => setViewMode(mode.id)}
          >
            {mode.label}
          </button>
        ))}
      </div>
      <span className="sp-toolbar-sep" />
      <IconButton icon="zoom-out" title="缩小" onClick={() => zoomAll(stepZoom(current, -1))} />
      <button type="button" className="sp-zoom-label" title="适合宽度" onClick={() => zoomAll('fit-width')}>
        {current === 'fit-width' ? '适合宽度' : `${Math.round(current * 100)}%`}
      </button>
      <IconButton icon="zoom-in" title="放大" onClick={() => zoomAll(stepZoom(current, 1))} />
      {viewMode === 'dual' && (
        <IconButton icon="link" title="同步滚动与缩放" active={sync} onClick={() => setSync(!sync)} />
      )}
      <span className="sp-spacer" />
      <IconButton icon="eye" title="显示版面框" active={showBoxes} onClick={() => setShowBoxes(!showBoxes)} />
      <div className="sp-popover-anchor">
        <IconButton icon="symbol-color" title="图例与类型筛选" active={legendOpen} onClick={() => setLegendOpen(!legendOpen)} />
        {legendOpen && <Legend onClose={() => setLegendOpen(false)} />}
      </div>
    </div>
  );
}

function Legend({ onClose }: { onClose: () => void }): JSX.Element {
  const hiddenKinds = useWorkbench((s) => s.hiddenKinds);
  const toggleKind = useWorkbench((s) => s.toggleKind);
  useEffect(() => {
    const onKey = (event: KeyboardEvent): void => {
      if (event.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);
  return (
    <div className="sp-popover sp-legend" role="dialog" aria-label="图例">
      {REGION_KINDS.map((kind) => (
        <label key={kind}>
          <input type="checkbox" checked={!hiddenKinds.includes(kind)} onChange={() => toggleKind(kind)} />
          <span className="sp-swatch" style={{ ['--kind' as string]: REGION_STYLE[kind].color }} />
          {REGION_STYLE[kind].label}
        </label>
      ))}
      <div className="sp-legend-note">
        <span className="sp-swatch is-inline" /> 虚线：行内公式
      </div>
    </div>
  );
}

function TargetPlaceholder({ doc }: { doc: LibraryDoc }): JSX.Element {
  const busy = doc.status === 'queued' || doc.status === 'running';
  return (
    <div className="sp-pane-message">
      {busy ? (
        <span>{doc.status === 'queued' ? '排队中，稍后开始翻译…' : '正在分析版面，第一页译好后显示…'}</span>
      ) : (
        <>
          <span>{doc.status === 'failed' ? `翻译失败：${doc.error ?? '未知错误'}` : '尚未翻译'}</span>
          <button type="button" className="sp-button" onClick={() => void window.syncpdf.engine.enqueue(doc.id)}>
            开始翻译
          </button>
        </>
      )}
    </div>
  );
}

/** 标题 / 作者还来自文件名时，用 PDF Info 的 Title / Author 补上。 */
function usePdfInfoMeta(meta: LibraryDoc | null): void {
  const needed = meta !== null && (meta.titleSource === 'filename' || meta.authorsSource === 'filename');
  const { doc } = usePdfDocument(needed ? meta.sourcePath : null);
  const id = meta?.id;
  useEffect(() => {
    if (doc === null || id === undefined) return;
    let cancelled = false;
    void doc.getMetadata().then(({ info }) => {
      const fields = info as { Title?: unknown; Author?: unknown };
      const text = (v: unknown): string | null => (typeof v === 'string' ? v : null);
      if (!cancelled) {
        void window.syncpdf.library.updateMeta(id, { title: text(fields.Title), authors: text(fields.Author) }, 'pdf_info');
      }
    });
    return () => {
      cancelled = true;
    };
  }, [doc, id]);
}
