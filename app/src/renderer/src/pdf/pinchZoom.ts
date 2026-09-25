/**
 * 触控板双指缩放：Chromium 把 pinch 报成 `ctrlKey=true` 的 wheel 事件，deltaY 是缩放量。
 * 倍率按指数变化（每单位 delta 等比缩放，放大缩小对称），缩放后让光标下的页面点保持不动。
 */
import { ZOOM_STEPS } from '@/store/workbench';

/** 每单位 deltaY 的对数缩放量。 */
const PINCH_SENSITIVITY = 0.01;

/** 按 pinch 的 deltaY 算新倍率，夹在缩放档位的上下限内。 */
export function pinchScale(scale: number, deltaY: number): number {
  const next = scale * Math.exp(-deltaY * PINCH_SENSITIVITY);
  return Math.min(ZOOM_STEPS.at(-1)!, Math.max(ZOOM_STEPS[0], next));
}

/** 缩放锚点：光标所在页及页内相对位置，和光标在滚动容器视口内的偏移。 */
export interface ZoomAnchor {
  page: string;
  fx: number;
  fy: number;
  x: number;
  y: number;
}

function pages(container: HTMLElement): HTMLElement[] {
  return [...container.querySelectorAll<HTMLElement>('[data-page]')];
}

/** 当前实际渲染倍率（fit-width 时也是具体数字）；页面未就绪为 null。 */
export function renderedScale(container: HTMLElement): number | null {
  const scale = Number(pages(container)[0]?.dataset.scale);
  return Number.isFinite(scale) && scale > 0 ? scale : null;
}

/** 记下光标（视口坐标）下的页面点；落在页间空隙时取其上方最近的页。 */
export function captureAnchor(container: HTMLElement, clientX: number, clientY: number): ZoomAnchor | null {
  const rect = container.getBoundingClientRect();
  const x = clientX - rect.left;
  const y = clientY - rect.top;
  const contentY = container.scrollTop + y;
  let hit: HTMLElement | null = null;
  for (const page of pages(container)) {
    if (page.offsetTop > contentY) break;
    hit = page;
  }
  if (hit === null || hit.offsetWidth === 0 || hit.offsetHeight === 0) return null;
  return {
    page: hit.dataset.page!,
    fx: (container.scrollLeft + x - hit.offsetLeft) / hit.offsetWidth,
    fy: (contentY - hit.offsetTop) / hit.offsetHeight,
    x,
    y,
  };
}

/** 页面按新倍率排好后，滚动到让锚点回到光标下。 */
export function restoreAnchor(container: HTMLElement, anchor: ZoomAnchor): void {
  const page = container.querySelector<HTMLElement>(`[data-page="${anchor.page}"]`);
  if (page === null) return;
  container.scrollLeft = page.offsetLeft + anchor.fx * page.offsetWidth - anchor.x;
  container.scrollTop = page.offsetTop + anchor.fy * page.offsetHeight - anchor.y;
}
