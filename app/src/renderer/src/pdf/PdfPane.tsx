/**
 * PDF 栏（源栏 / 译文栏共用）：缩放工具条 + 竖向滚动的页列表 + 段落框叠加层。
 *
 * - 缩放：`fit-width`（默认，随栏宽实时重算）或固定倍率（25%–400%）；
 * - 懒渲染在 `PageCanvas` 内；本组件只负责布局、滚动与选中联动；
 * - 滚动同步：按比例广播 / 接收（`scrollSync`），由 uiStore 开关控制；
 * - 选中段落变化时把它所在页滚进视野。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { BoxLayer, type ParagraphBoxes } from './BoxLayer';
import { PageCanvas } from './PageCanvas';
import { usePdfDocument } from './usePdfDocument';
import {
  applyScrollRatio,
  publishScroll,
  scrollRatioOf,
  subscribeScroll,
  type ScrollOrigin,
} from './scrollSync';
import type { PageViewport } from './pdfjs';

/** 允许的固定倍率档位。 */
export const ZOOM_STEPS = [0.25, 0.5, 0.75, 1, 1.25, 1.5, 2, 3, 4] as const;

export type ZoomValue = number | 'fit-width';

export interface PdfPaneProps {
  /** PDF 绝对路径（白名单内）。 */
  path: string | null;
  /** 整册修订号：变化 → 重新 `getDocument`。 */
  revision?: number;
  /** 页 → 该页修订号（变化 → 只重渲染该页）。 */
  pageRevisions?: Record<number, number>;
  /** 页号（1 基）→ 该页段落框。 */
  boxesByPage?: Map<number, ParagraphBoxes[]>;
  /** 选中段落 id。 */
  selectedId?: string | null;
  onSelect?: (id: string) => void;
  /** 只读叠加层（译文栏：只高亮选中段，不接鼠标）。 */
  readOnlyBoxes?: boolean;
  /** 滚动同步身份。 */
  origin: ScrollOrigin;
  /** 是否参与滚动同步。 */
  syncScroll: boolean;
  /** 空状态文案。 */
  emptyHint: string;
}

export function PdfPane({
  path,
  revision = 0,
  pageRevisions,
  boxesByPage,
  selectedId = null,
  onSelect,
  readOnlyBoxes = false,
  origin,
  syncScroll,
  emptyHint,
}: PdfPaneProps): JSX.Element {
  const { doc, loading, error } = usePdfDocument(path, revision);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const [containerWidth, setContainerWidth] = useState(600);
  const [zoom, setZoom] = useState<ZoomValue>('fit-width');
  const [hoveredId, setHoveredId] = useState<string | null>(null);
  const viewportsRef = useRef(new Map<number, PageViewport>());
  /** 正在应用外部滚动：避免回灌成环。 */
  const applyingRef = useRef(false);

  // 栏宽（fit-width 依赖）
  useEffect(() => {
    const node = scrollRef.current;
    if (node === null) return;
    const measure = (): void => setContainerWidth(node.clientWidth);
    measure();
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, [doc]);

  // 滚动同步：接收
  useEffect(() => {
    if (!syncScroll) return;
    return subscribeScroll((ratio, from) => {
      if (from === origin) return;
      const node = scrollRef.current;
      if (node === null) return;
      applyingRef.current = true;
      applyScrollRatio(node, ratio);
      // 下一帧再解锁（scroll 事件是异步派发的）
      requestAnimationFrame(() => {
        applyingRef.current = false;
      });
    });
  }, [syncScroll, origin]);

  // 滚动同步：广播
  const onScroll = useCallback(() => {
    if (!syncScroll || applyingRef.current) return;
    const node = scrollRef.current;
    if (node === null) return;
    publishScroll(scrollRatioOf(node), origin);
  }, [syncScroll, origin]);

  // 选中段落 → 滚动到它所在页
  useEffect(() => {
    if (selectedId === null) return;
    const node = scrollRef.current;
    if (node === null) return;
    const page = pageOfParagraph(selectedId, boxesByPage);
    if (page === null) return;
    const element = node.querySelector<HTMLElement>(`[data-page="${page}"]`);
    element?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
  }, [selectedId, boxesByPage]);

  const onViewport = useCallback((pageNumber: number, viewport: PageViewport | null) => {
    if (viewport === null) viewportsRef.current.delete(pageNumber);
    else viewportsRef.current.set(pageNumber, viewport);
  }, []);

  const pageNumbers = useMemo(
    () => (doc === null ? [] : Array.from({ length: doc.numPages }, (_value, index) => index + 1)),
    [doc],
  );

  const body = ((): JSX.Element => {
    if (path === null || path === '') {
      return <Hint text={emptyHint} icon="codicon-file-pdf" />;
    }
    if (error !== null) {
      return <Hint text={`加载失败：${error}`} icon="codicon-error" tone="error" />;
    }
    if (doc === null) {
      return <Hint text={loading ? '加载中…' : emptyHint} icon="codicon-loading" />;
    }
    return (
      <>
        {pageNumbers.map((pageNumber) => (
          <PageCanvas
            key={pageNumber}
            doc={doc}
            pageNumber={pageNumber}
            zoom={zoom}
            containerWidth={containerWidth}
            revision={pageRevisions?.[pageNumber] ?? 0}
            onViewport={onViewport}
            renderOverlay={(viewport) => {
              const paragraphs = boxesByPage?.get(pageNumber);
              if (paragraphs === undefined || paragraphs.length === 0) return null;
              return (
                <BoxLayer
                  viewport={viewport}
                  paragraphs={paragraphs}
                  selectedId={selectedId}
                  hoveredId={hoveredId}
                  onSelect={(id) => onSelect?.(id)}
                  onHover={setHoveredId}
                  readOnly={readOnlyBoxes}
                />
              );
            }}
          />
        ))}
      </>
    );
  })();

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', minWidth: 0 }}>
      <ZoomBar
        zoom={zoom}
        onZoom={setZoom}
        pageCount={doc?.numPages ?? 0}
        busy={loading}
      />
      <div
        ref={scrollRef}
        className="syncpdf-scroll"
        onScroll={onScroll}
        style={{
          flex: 1,
          minHeight: 0,
          overflow: 'auto',
          padding: '12px 0',
          background: 'var(--vscode-editorWidget-background, #252526)',
        }}
      >
        {body}
      </div>
    </div>
  );
}

/** 段落 id → 页号（1 基）；从 boxesByPage 反查，找不到退回 id 前缀。 */
function pageOfParagraph(
  id: string,
  boxesByPage: Map<number, ParagraphBoxes[]> | undefined,
): number | null {
  if (boxesByPage !== undefined) {
    for (const [page, list] of boxesByPage) {
      if (list.some((item) => item.id === id)) return page;
    }
  }
  const match = /^P(\d+)-/.exec(id);
  return match === null ? null : Number.parseInt(match[1], 10);
}

interface ZoomBarProps {
  zoom: ZoomValue;
  onZoom: (zoom: ZoomValue) => void;
  pageCount: number;
  busy: boolean;
}

function ZoomBar({ zoom, onZoom, pageCount, busy }: ZoomBarProps): JSX.Element {
  const stepZoom = (direction: -1 | 1): void => {
    const current = zoom === 'fit-width' ? 1 : zoom;
    const index = ZOOM_STEPS.findIndex((step) => step >= current - 1e-6);
    const next = ZOOM_STEPS[clampIndex(index + direction)];
    onZoom(next);
  };

  return (
    <div
      style={{
        flex: '0 0 26px',
        display: 'flex',
        alignItems: 'center',
        gap: 4,
        padding: '0 8px',
        fontSize: 11,
        borderBottom: '1px solid var(--vscode-editorGroup-border)',
        color: 'var(--vscode-editor-foreground)',
      }}
    >
      <IconButton label="缩小" icon="codicon-zoom-out" onClick={() => stepZoom(-1)} />
      <button
        type="button"
        onClick={() => onZoom(zoom === 'fit-width' ? 1 : 'fit-width')}
        title="切换 适应宽度 / 100%"
        style={{
          border: 'none',
          background: 'transparent',
          color: 'inherit',
          cursor: 'pointer',
          minWidth: 56,
          fontSize: 11,
        }}
      >
        {zoom === 'fit-width' ? '适应宽度' : `${Math.round(zoom * 100)}%`}
      </button>
      <IconButton label="放大" icon="codicon-zoom-in" onClick={() => stepZoom(1)} />
      <span style={{ marginLeft: 'auto', opacity: 0.7 }}>
        {busy ? '加载中…' : pageCount > 0 ? `${pageCount} 页` : ''}
      </span>
    </div>
  );
}

function IconButton({
  label,
  icon,
  onClick,
}: {
  label: string;
  icon: string;
  onClick: () => void;
}): JSX.Element {
  return (
    <button
      type="button"
      title={label}
      aria-label={label}
      onClick={onClick}
      style={{
        border: 'none',
        background: 'transparent',
        color: 'inherit',
        cursor: 'pointer',
        padding: '2px 4px',
        lineHeight: 1,
      }}
    >
      <span className={`codicon ${icon}`} />
    </button>
  );
}

function clampIndex(index: number): number {
  return Math.min(ZOOM_STEPS.length - 1, Math.max(0, index));
}

function Hint({
  text,
  icon,
  tone,
}: {
  text: string;
  icon: string;
  tone?: 'error';
}): JSX.Element {
  return (
    <div
      style={{
        display: 'grid',
        placeItems: 'center',
        height: '100%',
        gap: 8,
        opacity: tone === 'error' ? 1 : 0.55,
        color: tone === 'error' ? 'var(--vscode-errorForeground)' : 'inherit',
        fontSize: 12,
        textAlign: 'center',
        padding: 24,
      }}
    >
      <span className={`codicon ${icon}`} style={{ fontSize: 32 }} />
      <span>{text}</span>
    </div>
  );
}
