import { act, fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { BboxLegend } from '../src/components/preview/BboxLegend';
import { BboxLayer } from '../src/components/preview/BboxLayer';
import { categoryColor, categoryKey, categoryVisible, pageCategories } from '../src/lib/bbox';
import type { BboxItem } from '../src/lib/preview';
import { readVisibility, useBboxStore } from '../src/stores/bbox';
import { makeViewport } from './helpers';

const boxes: BboxItem[] = [
  { id: 'a', label: 'title', box: [1, 2, 10, 20] },
  { id: 'b', label: 'custom_label', box: [20, 2, 30, 20] },
  { id: 'c', label: 'title', box: [1, 30, 10, 40] },
  { id: 'd', label: null, box: [20, 30, 30, 40] },
];
beforeEach(() => {
  localStorage.clear();
  useBboxStore.setState({ documents: {}, strokeWidth: 1, fillOpacity: 0.08 });
});
describe('bbox categories', () => {
  it('uses actual labels, Chinese/raw names and current-page counts without mutating input', () => {
    const original = JSON.stringify(boxes);
    render(<BboxLegend did="one" page={3} boxes={boxes} />);
    expect(screen.getByText('框类别 · 当前页 3（4 个）')).toBeInTheDocument();
    expect(screen.getByLabelText('标题 · title（2）')).toBeChecked();
    expect(screen.getByLabelText('其他类别 · custom_label（1）')).toBeChecked();
    expect(screen.getByLabelText('未标注（null）（1）')).toBeChecked();
    expect(pageCategories([...boxes].reverse())).toEqual(pageCategories(boxes));
    expect(categoryColor('custom_label')).toBe(categoryColor('custom_label'));
    expect(categoryColor('custom_label')).not.toBe(categoryColor('another_label'));
    expect(categoryKey(null)).not.toBe(categoryKey('null'));
    expect(JSON.stringify(boxes)).toBe(original);
  });
  it('persists document isolation, all/none including later categories, and individual overrides', () => {
    const { rerender } = render(<BboxLegend did="one" page={1} boxes={boxes} />);
    fireEvent.click(screen.getByText('全不选类别'));
    expect(categoryVisible(readVisibility('one'), 'future')).toBe(false);
    fireEvent.click(screen.getByLabelText('标题 · title（2）'));
    expect(categoryVisible(readVisibility('one'), 'title')).toBe(true);
    act(() => useBboxStore.setState({ documents: {} }));
    expect(screen.getByLabelText('其他类别 · custom_label（1）')).not.toBeChecked();
    rerender(<BboxLegend did="two" page={1} boxes={boxes} />);
    expect(screen.getByLabelText('其他类别 · custom_label（1）')).toBeChecked();
    rerender(<BboxLegend did="one" page={2} boxes={boxes.slice(1, 2)} />);
    fireEvent.click(screen.getByText('全选类别'));
    expect(categoryVisible(readVisibility('one'), 'future')).toBe(true);
    expect(categoryVisible(readVisibility('one'), 'title')).toBe(true);
  });
  it('hidden categories have no click or keyboard targets, visible boxes retain coordinates and global styles', () => {
    const onSelect = vi.fn();
    const { container } = render(<BboxLayer boxes={boxes} viewport={makeViewport({ scale: 1 })}
      mode="parse" visibility={{ defaultVisible: false, overrides: { [categoryKey('custom_label')]: true } }}
      strokeWidth={3} fillOpacity={0.2} onSelect={onSelect} />);
    expect(screen.getAllByRole('button')).toHaveLength(1);
    expect(container.querySelector('[data-bbox-id="a"]')).toBeNull();
    const box = screen.getByRole('button');
    expect(box).toHaveAttribute('x', '20');
    expect(box).toHaveAttribute('stroke-width', '3');
    expect(box).toHaveAttribute('fill-opacity', '0.2');
    expect(box).toHaveAttribute('stroke', categoryColor('custom_label'));
    fireEvent.keyDown(box, { key: 'Enter' });
    expect(onSelect).toHaveBeenCalledWith('b');
  });
  it('global style controls persist, clamp invalid settings and tolerate malformed document storage', () => {
    render(<BboxLegend did="one" page={1} boxes={boxes} />);
    fireEvent.change(screen.getByLabelText('描边粗细'), { target: { value: '2.5' } });
    fireEvent.change(screen.getByLabelText('填充透明度'), { target: { value: '0.2' } });
    expect(JSON.parse(localStorage.getItem('ieet.bboxStyle')!)).toEqual({ strokeWidth: 2.5, fillOpacity: 0.2 });
    act(() => useBboxStore.getState().setStyle({ strokeWidth: Infinity, fillOpacity: 4 }));
    expect(useBboxStore.getState().strokeWidth).toBe(2.5);
    expect(useBboxStore.getState().fillOpacity).toBe(0.4);
    localStorage.setItem('ieet.bboxVisibility.broken', '{');
    expect(categoryVisible(readVisibility('broken'), null)).toBe(true);
  });
});
