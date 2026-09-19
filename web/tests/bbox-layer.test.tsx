/** `BboxLayer`：rect 数量、命中点击、选中/悬停态、键盘可达、空数据不渲染。 */
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';

import { BboxLayer } from '../src/components/preview/BboxLayer';
import type { BboxItem } from '../src/lib/preview';
import { makeViewport } from './helpers';

const LETTER_CROPBOX = { x0: 0, y0: 0, x1: 612, y1: 792 };

const PARSE_BOXES: BboxItem[] = [
  { id: 'P01-001', box: [66.585, 78.41, 544.64, 119.647], label: 'title' },
  { id: 'P01-002', box: [66.585, 128.41, 300, 160], label: 'text' },
];

function renderLayer(props: Partial<Parameters<typeof BboxLayer>[0]> = {}) {  return render(
    <BboxLayer
      boxes={PARSE_BOXES}
      viewport={makeViewport({ scale: 1 })}
      mode="parse"
      cropbox={LETTER_CROPBOX}
      onSelect={() => {}}
      {...props}
    />,
  );
}

describe('BboxLayer', () => {
  it('按 bbox 数量渲染 rect，并把屏幕坐标写进 x/y/width/height', () => {
    const { container } = renderLayer();
    const rects = container.querySelectorAll('rect');
    expect(rects).toHaveLength(2);
    const first = screen.getByRole('button', { name: /P01-001/ });
    expect(Number(first.getAttribute('x'))).toBeCloseTo(66.585, 6);
    expect(Number(first.getAttribute('y'))).toBeCloseTo(78.41, 6);
    expect(Number(first.getAttribute('width'))).toBeCloseTo(478.055, 6);
    expect(Number(first.getAttribute('height'))).toBeCloseTo(41.237, 6);
    expect(first).toHaveAttribute('data-od-id', 'bbox-P01-001');
    // overlay 自身不挡画布点击，只有 rect 命中
    const svg = container.querySelector('svg') as SVGSVGElement;
    expect(svg.getAttribute('class')).toContain('pointer-events-none');
    expect(first.getAttribute('class')).toContain('pointer-events-auto');
  });

  it('点击 rect 回调段落 id；svg 空白处点击不触发', () => {
    const onSelect = vi.fn();
    const { container } = renderLayer({ onSelect });
    fireEvent.click(screen.getByRole('button', { name: /P01-002/ }));
    expect(onSelect).toHaveBeenCalledWith('P01-002');
    fireEvent.click(container.querySelector('svg') as SVGSVGElement);
    expect(onSelect).toHaveBeenCalledTimes(1);
  });

  it('选中态：加粗虚线 + aria-pressed；类别颜色不变', () => {
    const { rerender } = renderLayer({ selectedId: null });
    const before = screen.getByRole('button', { name: /P01-001/ });
    expect(before).toHaveAttribute('aria-pressed', 'false');
    expect(before.getAttribute('stroke-width')).toBe('1.5');
    const categoryStroke = before.getAttribute('stroke');

    rerender(
      <BboxLayer
        boxes={PARSE_BOXES}
        viewport={makeViewport({ scale: 1 })}
        mode="parse"
        cropbox={LETTER_CROPBOX}
        selectedId="P01-001"
        onSelect={() => {}}
      />,
    );
    const after = screen.getByRole('button', { name: /P01-001/ });
    expect(after).toHaveAttribute('aria-pressed', 'true');
    expect(after.getAttribute('stroke-width')).toBe('2.5');
    expect(after).toHaveAttribute('stroke-dasharray', '4 2');
    expect(after.getAttribute('stroke')).toBe(categoryStroke);
    expect(after).toHaveAttribute('fill-opacity', '0.08');
  });

  it('键盘可达：Enter / Space 等同点击', () => {
    const onSelect = vi.fn();
    renderLayer({ onSelect });
    const rect = screen.getByRole('button', { name: /P01-001/ });
    fireEvent.keyDown(rect, { key: 'Enter' });
    fireEvent.keyDown(rect, { key: ' ' });
    fireEvent.keyDown(rect, { key: 'Tab' });
    expect(onSelect).toHaveBeenCalledTimes(2);
    expect(onSelect).toHaveBeenCalledWith('P01-001');
  });

  it('无 bbox 时整层不渲染（不产生空 svg）', () => {
    const { container } = renderLayer({ boxes: [] });
    expect(container.querySelector('svg')).toBeNull();
  });

  it('layout 模式用 pdf_native 换算（同一段落框与 parse 重合）', () => {
    const { container } = renderLayer({
      mode: 'layout',
      boxes: [{ id: 'P01-001', box: [66.585, 672.353, 544.64, 713.59], label: 'title' }],
    });
    const rect = container.querySelector('rect');
    expect(Number(rect?.getAttribute('x'))).toBeCloseTo(66.585, 6);
    expect(Number(rect?.getAttribute('y'))).toBeCloseTo(78.41, 6);
    expect(container.querySelector('svg')?.getAttribute('data-bbox-mode')).toBe('layout');
  });
});
