/**
 * `BboxEditor`：8 个手柄 + 拖拽数学（纯函数 `resizeBox`）+ 松手回调（viewport 逆变换后的 box）
 * + 拖拽期间屏蔽底层点击 + ESC 取消 + 只读态（编译中）。
 *
 * jsdom 没有 PointerEvent / 指针捕获，用例用 `helpers.stubPointerCapture` + `makePointerEvent`
 * 造出与浏览器同形的指针事件（`pointerId`/`clientX`/`clientY`/`button`）。
 */
import { act, fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { BboxEditor, MIN_BOX_PX, resizeBox } from '../src/components/edit/BboxEditor';
import type { ScreenRect } from '../src/components/preview/BboxLayer';
import { makePointerEvent, makeViewport, stubPointerCapture } from './helpers';

const LETTER_CROPBOX = { x0: 0, y0: 0, x1: 612, y1: 792 };
/** 真样本（tmp/ccs3764-dyn P01-001）的屏幕矩形与对应 box（`pdf_native`、y 向上）。 */
const RECT: ScreenRect = { x: 66.585, y: 78.41, width: 478.055, height: 41.237 };
const LAYOUT_BOX = [66.585, 672.353, 544.64, 713.59];

function renderEditor(props: Partial<Parameters<typeof BboxEditor>[0]> = {}) {
  const onCommit = vi.fn();
  const view = render(
    <BboxEditor
      id="P01-001"
      rect={RECT}
      viewport={makeViewport({ scale: 1 })}
      cropbox={LETTER_CROPBOX}
      onCommit={onCommit}
      {...props}
    />,
  );
  return { onCommit, view };
}

beforeEach(() => {
  stubPointerCapture();
});

describe('resizeBox（拖拽数学，纯函数）', () => {
  it('8 个手柄各自只动对应的边', () => {
    const rect: ScreenRect = { x: 100, y: 200, width: 120, height: 40 };
    expect(resizeBox(rect, 'nw', -10, -5)).toEqual({ x: 90, y: 195, width: 130, height: 45 });
    expect(resizeBox(rect, 'n', 0, -5)).toEqual({ x: 100, y: 195, width: 120, height: 45 });
    expect(resizeBox(rect, 'ne', 10, -5)).toEqual({ x: 100, y: 195, width: 130, height: 45 });
    expect(resizeBox(rect, 'e', 10, 0)).toEqual({ x: 100, y: 200, width: 130, height: 40 });
    expect(resizeBox(rect, 'se', 10, 5)).toEqual({ x: 100, y: 200, width: 130, height: 45 });
    expect(resizeBox(rect, 's', 0, 5)).toEqual({ x: 100, y: 200, width: 120, height: 45 });
    expect(resizeBox(rect, 'sw', -10, 5)).toEqual({ x: 90, y: 200, width: 130, height: 45 });
    expect(resizeBox(rect, 'w', -10, 0)).toEqual({ x: 90, y: 200, width: 130, height: 40 });
  });

  it('body = 整体平移（宽高不变）', () => {
    expect(resizeBox({ x: 100, y: 200, width: 120, height: 40 }, 'body', 7, -3)).toEqual({
      x: 107,
      y: 197,
      width: 120,
      height: 40,
    });
  });

  it('越过对边时按最小边长夹住（不翻转、不留 0 宽高）', () => {
    const rect: ScreenRect = { x: 100, y: 200, width: 120, height: 40 };
    const west = resizeBox(rect, 'w', 500, 0);
    expect(west.width).toBe(MIN_BOX_PX);
    expect(west.x).toBe(220 - MIN_BOX_PX);
    const north = resizeBox(rect, 'n', 0, 500);
    expect(north.height).toBe(MIN_BOX_PX);
    expect(north.y).toBe(240 - MIN_BOX_PX);
    const east = resizeBox(rect, 'e', -500, 0);
    expect(east.width).toBe(MIN_BOX_PX);
    expect(east.x).toBe(100);
  });
});

describe('BboxEditor 渲染', () => {
  it('选中段渲染 8 个手柄 + 框，并按屏幕矩形定位', () => {
    renderEditor();
    const handles = document.querySelectorAll('[data-od-id^="bbox-handle-"]');
    expect(handles).toHaveLength(8);
    expect(document.querySelector('[data-od-id="bbox-editor"]')).toHaveAttribute(
      'data-editable',
      'true',
    );
    const box = document.querySelector('[data-od-id="bbox-editor-box-P01-001"]');
    expect(Number(box?.getAttribute('x'))).toBeCloseTo(66.585, 6);
    expect(Number(box?.getAttribute('width'))).toBeCloseTo(478.055, 6);
    // 手柄锚在框上：nw 在左上角，se 在右下角（6px 手柄以中心对齐）
    const nw = document.querySelector('[data-od-id="bbox-handle-nw"]');
    expect(Number(nw?.getAttribute('x'))).toBeCloseTo(66.585 - 3, 6);
    expect(Number(nw?.getAttribute('y'))).toBeCloseTo(78.41 - 3, 6);
  });

  it('根 svg 不抢命中（pointer-events-none），命中只落在框体/手柄上', () => {
    // 真浏览器冒烟发现的回归：svg 根的 fill 默认黑色 = painted，会盖住**其它**框的点击
    const { view } = renderEditor();
    const svg = view.container.querySelector('svg') as SVGSVGElement;
    expect(svg.getAttribute('class')).toContain('pointer-events-none');
    expect(
      document.querySelector('[data-od-id="bbox-editor-box-P01-001"]')?.getAttribute('class'),
    ).toContain('pointer-events-auto');
    expect(
      document.querySelector('[data-od-id="bbox-handle-nw"]')?.getAttribute('class'),
    ).toContain('pointer-events-auto');
  });

  it('只读态（编译中）：没有手柄，框是虚线且带不可编辑原因', () => {
    renderEditor({ disabled: true, disabledReason: '编译中…：编译结束后可继续编辑' });
    expect(document.querySelectorAll('[data-od-id^="bbox-handle-"]')).toHaveLength(0);
    const root = document.querySelector('[data-od-id="bbox-editor"]');
    expect(root).toHaveAttribute('data-editable', 'false');
    expect(root?.getAttribute('aria-label')).toContain('编译中');
  });
});

describe('BboxEditor 拖拽', () => {
  it('拖 e 手柄 +20px → 实时更新屏幕矩形，松手回调逆变换后的 box', () => {
    const { onCommit } = renderEditor();
    const handle = document.querySelector('[data-od-id="bbox-handle-e"]') as SVGRectElement;

    act(() => {
      handle.dispatchEvent(makePointerEvent('pointerdown', { clientX: 544.64, clientY: 99.03 }));
    });
    // 拖拽中：整块视口被盖住（底层 BboxLayer 的 rect 拿不到点击）
    expect(document.querySelector('[data-od-id="bbox-editor-shield"]')).not.toBeNull();
    expect(document.querySelector('[data-od-id="bbox-editor"]')).toHaveAttribute(
      'data-dragging',
      'true',
    );

    act(() => {
      handle.dispatchEvent(makePointerEvent('pointermove', { clientX: 564.64, clientY: 99.03 }));
    });
    const box = document.querySelector('[data-od-id="bbox-editor-box-P01-001"]');
    expect(Number(box?.getAttribute('width'))).toBeCloseTo(498.055, 6);

    act(() => {
      handle.dispatchEvent(makePointerEvent('pointerup', { clientX: 564.64, clientY: 99.03 }));
    });
    expect(onCommit).toHaveBeenCalledTimes(1);
    const committed = onCommit.mock.calls[0][0] as number[];
    expect(committed[0]).toBeCloseTo(LAYOUT_BOX[0], 6);
    expect(committed[1]).toBeCloseTo(LAYOUT_BOX[1], 6);
    expect(committed[2]).toBeCloseTo(LAYOUT_BOX[2] + 20, 6);
    expect(committed[3]).toBeCloseTo(LAYOUT_BOX[3], 6);
    // 松手后回到非拖拽态
    expect(document.querySelector('[data-od-id="bbox-editor-shield"]')).toBeNull();
  });

  it('拖框体 = 平移：松手后 box 的 y 与 y2 同减（屏幕向下 = y 向上变小）', () => {
    const { onCommit } = renderEditor();
    const box = document.querySelector('[data-od-id="bbox-editor-box-P01-001"]') as SVGRectElement;
    act(() => {
      box.dispatchEvent(makePointerEvent('pointerdown', { clientX: 300, clientY: 90 }));
    });
    act(() => {
      box.dispatchEvent(makePointerEvent('pointermove', { clientX: 300, clientY: 120 }));
    });
    act(() => {
      box.dispatchEvent(makePointerEvent('pointerup', { clientX: 300, clientY: 120 }));
    });
    const committed = onCommit.mock.calls[0][0] as number[];
    expect(committed[1]).toBeCloseTo(LAYOUT_BOX[1] - 30, 6);
    expect(committed[3]).toBeCloseTo(LAYOUT_BOX[3] - 30, 6);
  });

  it('ESC 取消本次拖拽：不回调，矩形回到拖拽前', () => {
    const { onCommit } = renderEditor();
    const handle = document.querySelector('[data-od-id="bbox-handle-se"]') as SVGRectElement;
    act(() => {
      handle.dispatchEvent(makePointerEvent('pointerdown', { clientX: 544.64, clientY: 119.647 }));
    });
    act(() => {
      handle.dispatchEvent(makePointerEvent('pointermove', { clientX: 600, clientY: 200 }));
    });
    act(() => {
      fireEvent.keyDown(window, { key: 'Escape' });
    });
    expect(onCommit).not.toHaveBeenCalled();
    const box = document.querySelector('[data-od-id="bbox-editor-box-P01-001"]');
    expect(Number(box?.getAttribute('height'))).toBeCloseTo(41.237, 6);
    expect(document.querySelector('[data-od-id="bbox-editor-shield"]')).toBeNull();
  });

  it('只读态下拖拽无效果（不回调、无拖拽态）', () => {
    const { onCommit } = renderEditor({ disabled: true });
    const box = document.querySelector('[data-od-id="bbox-editor-box-P01-001"]') as SVGRectElement;
    act(() => {
      box.dispatchEvent(makePointerEvent('pointerdown', { clientX: 300, clientY: 90 }));
    });
    act(() => {
      box.dispatchEvent(makePointerEvent('pointerup', { clientX: 300, clientY: 190 }));
    });
    expect(onCommit).not.toHaveBeenCalled();
    expect(document.querySelector('[data-od-id="bbox-editor"]')).toHaveAttribute(
      'data-editable',
      'false',
    );
    expect(document.querySelector('[data-od-id="bbox-editor-shield"]')).toBeNull();
  });

  it('手柄可聚焦语义：role/aria-label 带段落 id 与手柄名（键盘用户能定位）', () => {
    renderEditor();
    expect(screen.getByRole('button', { name: /P01-001.*nw/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /P01-001.*se/ })).toBeInTheDocument();
  });
});
