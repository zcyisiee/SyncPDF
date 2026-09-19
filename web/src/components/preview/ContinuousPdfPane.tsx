import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import type { PageViewport } from 'pdfjs-dist';

import { clampPage, geometryBboxes, type GeometryBboxes } from '../../lib/preview';
import { useGeometry } from '../../lib/queries';
import { readVisibility, useBboxStore } from '../../stores/bbox';
import { useUiStore } from '../../stores/ui';
import { BboxLayer, type PdfPointViewport, type ScreenViewport } from './BboxLayer';
import { acquireDocument, PdfCanvas, type PdfPageInfo } from './PdfCanvas';

export interface BboxPaneData {
  mode: 'parse' | 'layout';
  data: GeometryBboxes;
  selectedId: string | null;
  onSelect: (id: string) => void;
}
export interface ReaderPosition { pane: string; page: number; fraction: number }
interface Props {
  did: string;
  url: string | null;
  pageNumber: number;
  pageCount: number;
  navigation: { page: number; revision: number; pane?: string };
  position: ReaderPosition | null;
  initialPosition: ReaderPosition | null;
  paneId: string;
  onPosition: (position: ReaderPosition, programmatic?: boolean) => void;
  geometryKind: 'parse' | 'layout' | null;
  bbox: BboxPaneData | null;
  recognition?: boolean;
  overlay?: (viewport: ScreenViewport & PdfPointViewport) => ReactNode;
  odId: string;
  onPageInfo?: (info: PdfPageInfo) => void;
  onScale?: (scale: number) => void;
  emptyState: ReactNode;
}

const GAP = 20;

function PageLayer({ did, kind, page, viewport, bbox, recognition }: {
  did: string; kind: Props['geometryKind']; page: number;
  viewport: PageViewport; bbox: BboxPaneData | null; recognition: boolean;
}) {
  const preferences = useBboxStore();
  const visibility = preferences.documents[did] ?? readVisibility(did);
  const query = useGeometry(did, bbox === null ? kind : null, page);
  const selected = useUiStore((state) => state.selectedParagraphId);
  const select = useUiStore((state) => state.setSelectedParagraph);
  const setPage = useUiStore((state) => state.setPreviewPage);
  const data = bbox?.data ?? (query.data ? geometryBboxes(query.data, recognition) : null);
  if (data === null || kind === null) return null;
  return <BboxLayer visibility={visibility} strokeWidth={preferences.strokeWidth} fillOpacity={preferences.fillOpacity} boxes={data.boxes} viewport={viewport}
    mode={data.coordSystem === 'pdf_native' ? 'layout' : 'parse'} cropbox={data.cropbox}
    selectedId={selected} onSelect={(id) => { setPage(page); select(id); }} />;
}

/** Lightweight page slots preserve the scroll range; only the viewport and one
 * adjacent page on either side own canvases and geometry subscriptions. */
export function ContinuousPdfPane({ did, url, pageNumber, pageCount, navigation, position,
  initialPosition, paneId, onPosition, geometryKind, bbox, recognition = false, overlay, odId, onPageInfo, onScale, emptyState }: Props) {
  const container = useRef<HTMLDivElement>(null);
  const [size, setSize] = useState({ width: 600, height: 800 });
  const [scroll, setScroll] = useState(0);
  const [infos, setInfos] = useState<Record<number, PdfPageInfo>>({});
  const zoom = useUiStore((state) => state.previewZoom);
  const setZoom = useUiStore((state) => state.setPreviewZoom);
  const [actualCount, setActualCount] = useState<number | null>(null);
  const count = actualCount ?? pageCount;
  const gestureScale = useRef(1);
  const anchor = useRef(initialPosition ?? position ?? { page: navigation.page, fraction: 0 });
  const lastNavigation = useRef<Props['navigation']>(navigation);
  const appliedTop = useRef(0);
  const programmatic = useRef<number | null>(null);

  // Keep a document lease while scrolling, including jumps that replace every canvas.
  useEffect(() => {
    if (url === null) return;
    const document = acquireDocument(url);
    return document.release;
  }, [url]);

  useEffect(() => {
    const element = container.current;
    if (!element) return;
    const update = () => setSize({ width: element.clientWidth || 600, height: element.clientHeight || 800 });
    update();
    if (typeof ResizeObserver === 'undefined') return;
    const observer = new ResizeObserver(update);
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const metrics = useMemo(() => {
    let top = GAP;
    const result = [];
    for (let index = 0; index < count; index += 1) {
      const info = infos[index + 1];
      const base = info?.viewport ?? infos[1]?.viewport;
      const width = base?.width ?? 612;
      const height = base?.height ?? 792;
      // Fit must still fit a narrow comparison pane; no arbitrary 50% floor.
      const scale = zoom ?? Math.max(0.01, (size.width - 16) / width);
      const metric = { top, height: height * scale, width: width * scale, scale };
      top += metric.height + GAP;
      result.push(metric);
    }
    return result;
  }, [count, infos, size.width, zoom]);

  const moveTo = useCallback((page: number, fraction: number) => {
    const element = container.current;
    const metric = metrics[clampPage(page, count) - 1];
    if (!element || !metric) return;
    const top = Math.max(0, Math.min(metric.top + metric.height * fraction,
      element.scrollHeight - element.clientHeight));
    appliedTop.current = top;
    programmatic.current = top;
    element.scrollTop = top;
    setScroll(top);
    const index = Math.max(0, metrics.findIndex((item) => item.top + item.height > top));
    const actual = metrics[index];
    onPosition({ pane: paneId, page: index + 1,
      fraction: Math.max(0, Math.min(1, (top - actual.top) / actual.height)) }, true);
  }, [metrics, count, onPosition, paneId]);

  // Preserve page + relative position when page dimensions, zoom, or pane width change.
  const previousMetrics = useRef(metrics);
  useLayoutEffect(() => {
    const top = container.current?.scrollTop ?? appliedTop.current;
    // A scroll event may still be queued when a PDF page finishes loading.
    // Capture that movement before changing the slot dimensions.
    if (Math.abs(top - appliedTop.current) >= 1) {
      const index = Math.max(0, previousMetrics.current.findIndex((item) => item.top + item.height > top));
      const metric = previousMetrics.current[index];
      if (metric) anchor.current = { page: index + 1, fraction: Math.max(0, Math.min(1, (top - metric.top) / metric.height)) };
    }
    previousMetrics.current = metrics;
    moveTo(anchor.current.page, anchor.current.fraction);
  }, [moveTo, metrics]);
  useEffect(() => {
    if (lastNavigation.current === navigation) return;
    lastNavigation.current = navigation;
    if (navigation.pane && navigation.pane !== paneId) return;
    anchor.current = { page: navigation.page, fraction: 0 };
    moveTo(navigation.page, 0);
  }, [navigation, moveTo, paneId]);
  useEffect(() => {
    if (!position || position.pane === paneId) return;
    anchor.current = { page: clampPage(position.page, count), fraction: position.fraction };
    moveTo(anchor.current.page, anchor.current.fraction);
  }, [position, paneId, moveTo, count]);

  const activeIndex = Math.max(0, metrics.findIndex((metric) => metric.top + metric.height > scroll));
  const activeScale = metrics[activeIndex]?.scale ?? 1;
  useEffect(() => { onScale?.(activeScale); }, [activeScale, onScale]);

  useEffect(() => {
    const element = container.current;
    if (!element) return;
    const wheel = (event: WheelEvent) => {
      if (!event.ctrlKey && !event.metaKey) return;
      event.preventDefault();
      setZoom(activeScale * Math.exp(-event.deltaY * 0.01));
    };
    // Safari exposes gesture events rather than ctrl+wheel for trackpad pinch.
    const gestureStart = (event: Event) => { event.preventDefault(); gestureScale.current = activeScale; };
    const gestureChange = (event: Event) => {
      event.preventDefault();
      const scale = (event as Event & { scale?: number }).scale;
      if (typeof scale === 'number') setZoom(gestureScale.current * scale);
    };
    element.addEventListener('wheel', wheel, { passive: false });
    element.addEventListener('gesturestart', gestureStart, { passive: false });
    element.addEventListener('gesturechange', gestureChange, { passive: false });
    return () => {
      element.removeEventListener('wheel', wheel);
      element.removeEventListener('gesturestart', gestureStart);
      element.removeEventListener('gesturechange', gestureChange);
    };
  }, [activeScale, setZoom]);

  const first = Math.max(0, activeIndex - 1);
  const lastVisible = metrics.findIndex((metric) => metric.top > scroll + size.height);
  const last = Math.min(count - 1, (lastVisible < 0 ? count - 1 : lastVisible));
  const totalHeight = (metrics.at(-1)?.top ?? 0) + (metrics.at(-1)?.height ?? 0) + GAP;

  return <div ref={container} data-reader-pane={paneId} aria-label={paneId === 'source' ? '原文连续阅读' : 'PDF 连续阅读'}
    tabIndex={0} className="min-h-0 min-w-0 flex-1 overflow-auto" style={{ overflowAnchor: 'none' }}
    onScroll={(event) => {
      const top = event.currentTarget.scrollTop;
      appliedTop.current = top;
      setScroll(top);
      if (programmatic.current !== null && Math.abs(top - programmatic.current) < 1) {
        programmatic.current = null;
        return;
      }
      programmatic.current = null;
      const index = Math.max(0, metrics.findIndex((metric) => metric.top + metric.height > top));
      const metric = metrics[index];
      const fraction = metric ? Math.max(0, Math.min(1, (top - metric.top) / metric.height)) : 0;
      anchor.current = { page: index + 1, fraction };
      onPosition({ pane: paneId, ...anchor.current });
    }}>
    {url === null ? <div className="grid h-full place-items-center p-s6">{emptyState}</div> :
      <div className="relative" style={{ height: totalHeight, minWidth: Math.max(size.width, ...metrics.map((metric) => metric.width)) }}>
        {metrics.slice(first, last + 1).map((metric, offset) => {
          const page = first + offset + 1;
          const info = infos[page];
          const viewport = info?.viewport.clone({ scale: metric.scale });
          return <div key={page} data-reader-page={page} data-od-id={odId}
            className="absolute bg-white" style={{ top: metric.top, left: Math.max(8, (size.width - metric.width) / 2), width: metric.width, height: metric.height }}>
            <PdfCanvas url={url} pageNumber={page} scale={metric.scale} onPage={(next) => {
              setInfos((previous) => previous[page]?.viewport.width === next.viewport.width && previous[page]?.viewport.height === next.viewport.height && previous[page]?.viewport.rotation === next.viewport.rotation ? previous : { ...previous, [page]: next });
              setActualCount(next.numPages);
              onPageInfo?.(next);
            }} />
            {viewport ? <PageLayer recognition={recognition} did={did} kind={geometryKind} page={page} viewport={viewport}
              bbox={page === pageNumber ? bbox : null} /> : null}
            {viewport && page === pageNumber ? overlay?.(viewport) : null}
          </div>;
        })}
      </div>}
  </div>;
}
