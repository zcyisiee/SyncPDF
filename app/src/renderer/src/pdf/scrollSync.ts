/**
 * 源栏 / 译文栏滚动同步（可开关，见 uiStore.scrollSyncEnabled）。
 *
 * 用"滚动比例"而不是绝对像素：两栏缩放倍率可能不同（译文页面尺寸一致，
 * 但栏宽不同 → fit-width 的 scale 不同），比例同步才对得上。
 * 发布者自己不会被自己的事件回灌（按 origin 过滤）。
 */

export type ScrollOrigin = 'source' | 'target';

type Listener = (ratio: number, origin: ScrollOrigin) => void;

const listeners = new Set<Listener>();

/** 订阅滚动比例；返回取消订阅。 */
export function subscribeScroll(listener: Listener): () => void {
  listeners.add(listener);
  return () => {
    listeners.delete(listener);
  };
}

/** 广播滚动比例（0..1）。 */
export function publishScroll(ratio: number, origin: ScrollOrigin): void {
  if (!Number.isFinite(ratio)) return;
  const clamped = Math.min(1, Math.max(0, ratio));
  for (const listener of [...listeners]) {
    listener(clamped, origin);
  }
}

/** 滚动容器 → 比例（内容不足一屏时为 0）。 */
export function scrollRatioOf(element: HTMLElement): number {
  const max = element.scrollHeight - element.clientHeight;
  if (max <= 0) return 0;
  return element.scrollTop / max;
}

/** 比例 → 滚动容器位置。 */
export function applyScrollRatio(element: HTMLElement, ratio: number): void {
  const max = element.scrollHeight - element.clientHeight;
  if (max <= 0) return;
  element.scrollTop = ratio * max;
}
