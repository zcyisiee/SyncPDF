import { act, fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { BboxLegend } from '../src/components/preview/BboxLegend';
import { BboxLayer } from '../src/components/preview/BboxLayer';
import { categoryColor, categoryKey, categoryVisible } from '../src/lib/bbox';
import type { BboxItem } from '../src/lib/preview';
import { readVisibility, useBboxStore } from '../src/stores/bbox';
import { makeViewport } from './helpers';

const boxes: BboxItem[] = [
  { id: 'a', label: 'text', box: [1, 2, 100, 50] },
  { id: 'b', label: 'inline_equation', kind: 'span', paragraphId: null, box: [20, 12, 30, 20] },
];
beforeEach(() => {
  localStorage.clear();
  useBboxStore.setState({ documents: {}, strokeWidth: 1.5, fillOpacity: 0.08 });
});
describe('bbox categories', () => {
  it('lists only actual raw labels from the document, including labels on other pages', () => {
    render(<BboxLegend did="one" boxes={boxes.slice(0, 1)} labels={[{ label: 'text' }, { label: 'inline_equation' }, { label: 'custom_label' }]} />);
    expect(screen.getAllByRole('checkbox')).toHaveLength(3);
    expect(screen.getByLabelText('inline_equation')).toBeChecked();
    expect(screen.getByLabelText('custom_label')).toBeChecked();
    expect(screen.queryByRole('slider')).not.toBeInTheDocument();
    expect(screen.queryByRole('button')).not.toBeInTheDocument();
    expect(screen.queryByText(/仅控制预览|当前页|其他类别/)).not.toBeInTheDocument();
    expect(categoryColor('custom_label')).not.toBe(categoryColor('another_label'));
    expect(categoryKey(null)).not.toBe(categoryKey('null'));
  });
  it('toggles independent span frames and persists across pages, reloads and document switches', () => {
    const select = vi.fn();
    function Preview({ did, rows }: { did: string; rows: BboxItem[] }) {
      const state = useBboxStore();
      return <><BboxLegend did={did} boxes={rows} labels={[{ label: 'text' }, { label: 'inline_equation' }]} />
        <BboxLayer boxes={rows} viewport={makeViewport({ scale: 1 })} mode="parse" onSelect={select}
          visibility={state.documents[did] ?? readVisibility(did)} /></>;
    }
    const { container, rerender } = render(<Preview did="one" rows={boxes} />);
    const span = container.querySelector('[data-bbox-id="b"]')!;
    expect(span).toHaveAttribute('x', '20');
    expect(span).toHaveAttribute('y', '12');
    expect(span).toHaveAttribute('rx', '3');
    expect(span).not.toHaveAttribute('tabindex');
    fireEvent.click(span);
    expect(select).not.toHaveBeenCalled();
    fireEvent.click(screen.getByLabelText('inline_equation'));
    expect(container.querySelector('[data-bbox-id="b"]')).toBeNull();
    expect(container.querySelector('[data-bbox-id="a"]')).not.toBeNull();
    rerender(<Preview did="one" rows={[]} />);
    expect(screen.getByLabelText('inline_equation')).not.toBeChecked();
    act(() => useBboxStore.setState({ documents: {} }));
    rerender(<Preview did="one" rows={boxes} />);
    expect(container.querySelector('[data-bbox-id="b"]')).toBeNull();
    rerender(<Preview did="two" rows={boxes} />);
    expect(screen.getByLabelText('inline_equation')).toBeChecked();
    rerender(<Preview did="one" rows={boxes} />);
    fireEvent.click(screen.getByLabelText('inline_equation'));
    expect(container.querySelector('[data-bbox-id="b"]')).not.toBeNull();
    fireEvent.click(screen.getByLabelText('text'));
    expect(container.querySelector('[data-bbox-id="a"]')).toBeNull();
    expect(container.querySelector('[data-bbox-id="b"]')).not.toBeNull();
  });
  it('avoids duplicate text outlines while keeping formula spans independent', () => {
    const rows: BboxItem[] = [
      { id: 'title', label: 'title', kind: 'block', paragraphId: null, box: [0, 0, 100, 50] },
      { id: 'words', label: 'text', kind: 'span', parentId: 'title', paragraphId: null, box: [5, 5, 40, 20] },
      { ...boxes[1], parentId: 'title' },
    ];
    const { container, rerender } = render(<BboxLayer boxes={rows} viewport={makeViewport({ scale: 1 })} mode="parse" />);
    expect(container.querySelector('[data-bbox-id="words"]')).toBeNull();
    expect(container.querySelector('[data-bbox-id="b"]')).not.toBeNull();
    rerender(<BboxLayer boxes={rows} viewport={makeViewport({ scale: 1 })} mode="parse"
      visibility={{ defaultVisible: true, overrides: { [categoryKey('title')]: false } }} />);
    expect(container.querySelector('[data-bbox-id="title"]')).toBeNull();
    expect(container.querySelector('[data-bbox-id="words"]')).not.toBeNull();
    expect(container.querySelector('[data-bbox-id="b"]')).not.toBeNull();
  });
  it('provider blocks select only their mapped paragraph, while malformed storage is harmless', () => {
    const select = vi.fn();
    render(<BboxLayer boxes={[{ ...boxes[0], kind: 'block', paragraphId: 'P01-001' }]}
      viewport={makeViewport({ scale: 1 })} mode="parse" onSelect={select} />);
    fireEvent.keyDown(screen.getByRole('button'), { key: 'Enter' });
    expect(select).toHaveBeenCalledWith('P01-001');
    localStorage.setItem('ieet.bboxVisibility.broken', '{');
    expect(categoryVisible(readVisibility('broken'), null)).toBe(true);
  });
});
