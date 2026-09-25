/**
 * 源栏 / 译文栏滚动同步（可开关，见 workbench store 的 `sync`）。
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

/** 用户输入后这段时间内的滚动算用户滚动（惯性滚动会持续一小会儿）。 */
export const USER_SCROLL_WINDOW_MS = 600;

/**
 * 判定一次 scroll 事件是否由用户发起。只有用户滚动才广播给对侧；
 * 布局变化（译文重载、页面尺寸变化、scrollTop 被夹紧）、对侧同步、跳转
 * 引起的滚动都不广播，否则两栏会互相拖动。
 */
export class UserScrollGate {
  private until = 0;
  private pointerDown = false;

  constructor(private readonly now: () => number = () => performance.now()) {}

  /** wheel / 键盘 / 触控输入。 */
  noteInput(): void {
    this.until = this.now() + USER_SCROLL_WINDOW_MS;
  }

  /** 按住滚动条拖动期间都算用户滚动。 */
  setPointerDown(down: boolean): void {
    this.pointerDown = down;
    if (!down) this.noteInput();
  }

  isUserScroll(): boolean {
    return this.pointerDown || this.now() < this.until;
  }
}
