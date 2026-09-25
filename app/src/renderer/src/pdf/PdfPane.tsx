/**
 * 一栏 PDF（原文或译文）：懒渲染页面 + 叠加框；双栏同步滚动（按滚动比例）；
 * 响应跳转请求（滚到某页 / 某段）。原文栏负责上报当前页与总页数。
 */
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { ParagraphId } from '@shared/protocol';
import { useLibrary } from '@/store/library';
import { useWorkbench } from '@/store/workbench';
import { BoxOverlay } from './BoxOverlay';
import type { OverlayItem } from './overlay';
import { PageCanvas } from './PageCanvas';
import type { PageViewport } from './pdfjs';
import { captureAnchor, pinchScale, renderedScale, restoreAnchor, type ZoomAnchor } from './pinchZoom';
import {
  applyScrollRatio,
  publishScroll,
  scrollRatioOf,
  subscribeScroll,
  UserScrollGate,
  type ScrollOrigin,
} from './scrollSync';
import { usePdfDocument } from './usePdfDocument';

export interface PdfPaneProps {
  side: ScrollOrigin;
  path: string;
  revision: number;
  itemsByPage: Map<number, OverlayItem[]>;
}

export function PdfPane({ side, path, revision, itemsByPage }: PdfPaneProps): JSX.Element {
  const { doc, error, loading } = usePdfDocument(path, revision);
  const zoom = useWorkbench((s) => s.zoom[side]);
  const showBoxes = useWorkbench((s) => s.showBoxes);
  const selected = useLibrary((s) => s.selected);
  const reveal = useLibrary((s) => s.reveal);
  const scrollRef = useRef<HTMLDivElement | null>(null);
  /** 只广播用户发起的滚动（见 UserScrollGate）。 */
  const [gate] = useState(() => new UserScrollGate());
  const [width, setWidth] = useState(0);

  useEffect(() => {
    const node = scrollRef.current;
    if (node === null) return;
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width));
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (side === 'source' && doc !== null) useLibrary.getState().setPageCount(doc.numPages);
  }, [side, doc]);

  // 双指缩放：必须是非 passive 监听才能 preventDefault；一帧合并一次，避免每个事件都重排全部页面
  const pinchAnchor = useRef<ZoomAnchor | null>(null);
  useEffect(() => {
    const node = scrollRef.current;
    if (node === null) return;
    let frame = 0;
    let delta = 0;
    let anchor: ZoomAnchor | null = null;
    const onWheel = (event: WheelEvent): void => {
      if (!event.ctrlKey) return;
      event.preventDefault();
      delta += event.deltaY;
      anchor ??= captureAnchor(node, event.clientX, event.clientY);
      if (frame !== 0) return;
      frame = requestAnimationFrame(() => {
        frame = 0;
        const current = renderedScale(node);
        const next = current === null ? null : pinchScale(current, delta);
        if (next !== null && next !== current) {
          pinchAnchor.current = anchor;
          useWorkbench.getState().setZoom(side, next);
        }
        delta = 0;
        anchor = null;
      });
    };
    node.addEventListener('wheel', onWheel, { passive: false });
    return () => {
      node.removeEventListener('wheel', onWheel);
      cancelAnimationFrame(frame);
    };
  }, [side]);

  useLayoutEffect(() => {
    const node = scrollRef.current;
    if (node !== null && pinchAnchor.current !== null) restoreAnchor(node, pinchAnchor.current);
    pinchAnchor.current = null;
  }, [zoom]);

  useEffect(
    () =>
      subscribeScroll((ratio, origin) => {
        const node = scrollRef.current;
        if (origin === side || node === null || !useWorkbench.getState().sync) return;
        applyScrollRatio(node, ratio);
      }),
    [side],
  );

  // 跳转：优先滚到段落框，其次页面
  useEffect(() => {
    const node = scrollRef.current;
    if (reveal === null || node === null) return;
    const target =
      (reveal.paragraphId === null ? null : node.querySelector(`[data-pid="${reveal.paragraphId}"]`)) ??
      node.querySelector(`[data-page="${reveal.page}"]`);
    target?.scrollIntoView({ block: reveal.paragraphId === null ? 'start' : 'center' });
  }, [reveal]);

  const onScroll = (): void => {
    const node = scrollRef.current;
    if (node === null) return;
    if (side === 'source') useLibrary.getState().setCurrentPage(currentPageOf(node));
    if (gate.isUserScroll() && useWorkbench.getState().sync) publishScroll(scrollRatioOf(node), side);
  };

  const onSelect = useCallback((id: ParagraphId) => useLibrary.getState().select(id), []);

  const renderOverlay = useCallback(
    (pageNumber: number) =>
      function Overlay(viewport: PageViewport) {
        const items = itemsByPage.get(pageNumber);
        if (!showBoxes || items === undefined) return null;
        return <BoxOverlay items={items} viewport={viewport} selected={selected} onSelect={onSelect} />;
      },
    [itemsByPage, showBoxes, selected, onSelect],
  );

  return (
    <div
      ref={scrollRef}
      className="sp-pdf-scroll"
      data-side={side}
      tabIndex={-1}
      onScroll={onScroll}
      onWheel={() => gate.noteInput()}
      onKeyDown={() => gate.noteInput()}
      onTouchMove={() => gate.noteInput()}
      onPointerDown={() => gate.setPointerDown(true)}
      onPointerUp={() => gate.setPointerDown(false)}
      onPointerCancel={() => gate.setPointerDown(false)}
    >
      {doc === null && (
        <div className="sp-pane-message">{error !== null ? `无法打开 PDF：${error}` : loading ? '加载中…' : ''}</div>
      )}
      {doc !== null &&
        width > 0 &&
        Array.from({ length: doc.numPages }, (_, i) => i + 1).map((pageNumber) => (
          <PageCanvas
            key={pageNumber}
            doc={doc}
            pageNumber={pageNumber}
            zoom={zoom}
            containerWidth={width}
            revision={revision}
            renderOverlay={renderOverlay(pageNumber)}
          />
        ))}
    </div>
  );
}

/** 视口上三分之一处所在的页。 */
function currentPageOf(node: HTMLElement): number {
  const probe = node.scrollTop + node.clientHeight / 3;
  let page = 1;
  for (const child of node.querySelectorAll<HTMLElement>('[data-page]')) {
    if (child.offsetTop > probe) break;
    page = Number(child.dataset.page);
  }
  return page;
}
