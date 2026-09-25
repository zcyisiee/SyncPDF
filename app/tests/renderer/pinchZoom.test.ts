/**
 * @vitest-environment jsdom
 *
 * 双指缩放：倍率按 delta 指数变化且有上下限；缩放后光标下的页面点保持不动。
 */
import { describe, expect, it } from 'vitest';
import { captureAnchor, pinchScale, renderedScale, restoreAnchor } from '@/pdf/pinchZoom';
import { ZOOM_STEPS } from '@/store/workbench';

describe('pinchScale', () => {
  it('张开（deltaY<0）放大、捏合缩小，来回同量回到原倍率', () => {
    const bigger = pinchScale(1, -20);
    expect(bigger).toBeGreaterThan(1);
    expect(pinchScale(1, 20)).toBeLessThan(1);
    expect(pinchScale(bigger, 20)).toBeCloseTo(1, 10);
  });

  it('夹在缩放档位上下限内', () => {
    expect(pinchScale(1, -10_000)).toBe(ZOOM_STEPS.at(-1));
    expect(pinchScale(1, 10_000)).toBe(ZOOM_STEPS[0]);
  });
});

/** jsdom 不排版：按给定几何伪造页面元素。 */
function layout(pageBoxes: Array<{ top: number; left: number; width: number; height: number; scale?: number }>): HTMLElement {
  const container = document.createElement('div');
  container.getBoundingClientRect = () => ({ left: 100, top: 50 }) as DOMRect;
  pageBoxes.forEach((box, index) => {
    const page = document.createElement('div');
    page.dataset.page = String(index + 1);
    if (box.scale !== undefined) page.dataset.scale = String(box.scale);
    Object.defineProperties(page, {
      offsetTop: { value: box.top },
      offsetLeft: { value: box.left },
      offsetWidth: { value: box.width },
      offsetHeight: { value: box.height },
    });
    container.append(page);
  });
  return container;
}

describe('缩放锚点', () => {
  it('放大一倍后光标下仍是同一页面点', () => {
    const before = layout([
      { top: 12, left: 10, width: 600, height: 800 },
      { top: 824, left: 10, width: 600, height: 800 },
    ]);
    before.scrollTop = 700;
    // 视口内 (50, 200) → 内容 y = 900，落在第 2 页 76px 处
    const anchor = captureAnchor(before, 150, 250)!;
    expect(anchor).toMatchObject({ page: '2', x: 50, y: 200 });

    const after = layout([
      { top: 12, left: 0, width: 1200, height: 1600 },
      { top: 1624, left: 0, width: 1200, height: 1600 },
    ]);
    restoreAnchor(after, anchor);
    const page2 = after.children[1] as HTMLElement;
    expect(after.scrollTop + anchor.y).toBeCloseTo(page2.offsetTop + 76 * 2);
    expect(after.scrollLeft + anchor.x).toBeCloseTo((50 - 10) * 2);
  });

  it('光标落在页间空隙时取上方的页；没有页面时不锚定', () => {
    const container = layout([
      { top: 12, left: 0, width: 600, height: 800 },
      { top: 824, left: 0, width: 600, height: 800 },
    ]);
    container.scrollTop = 0;
    expect(captureAnchor(container, 100, 50 + 818)?.page).toBe('1');
    expect(captureAnchor(layout([]), 0, 0)).toBeNull();
  });

  it('实际倍率取自页面 data-scale（fit-width 也是具体数字）', () => {
    expect(renderedScale(layout([{ top: 0, left: 0, width: 1, height: 1, scale: 1.37 }]))).toBe(1.37);
    expect(renderedScale(layout([{ top: 0, left: 0, width: 1, height: 1 }]))).toBeNull();
  });
});
