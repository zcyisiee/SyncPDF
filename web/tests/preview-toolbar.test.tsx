/** `PreviewToolbar`：模式切换、原文模式禁用态与 tooltip、页码提交、bbox 三态、只读 zoom。 */
import { fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { PreviewToolbar } from '../src/components/preview/PreviewToolbar';
import { STORAGE_KEYS, uiStore } from '../src/stores/ui';
import { resetUiStore } from './helpers';

function renderToolbar(props: Partial<Parameters<typeof PreviewToolbar>[0]> = {}) {
  return render(
    <PreviewToolbar
      page={1}
      pageCount={21}
      scale={1}
      sourceAvailable
      paged
      onPageChange={() => {}}
      onBboxModeChange={() => {}}
      {...props}
    />,
  );
}

beforeEach(() => {
  resetUiStore();
});

describe('PreviewToolbar', () => {
  it('三模式切换写回 store.previewMode（原文 / 译文 / 对照）', () => {
    renderToolbar();
    const group = screen.getByRole('group', { name: '预览模式' });
    expect(group).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '译文' })).toHaveAttribute('aria-pressed', 'true');

    fireEvent.click(screen.getByRole('button', { name: '对照' }));
    expect(uiStore.getState().previewMode).toBe('compare');
    expect(screen.getByRole('button', { name: '对照' })).toHaveAttribute('aria-pressed', 'true');

    fireEvent.click(screen.getByRole('button', { name: '原文' }));
    expect(uiStore.getState().previewMode).toBe('source');
  });

  it('没有 source.pdf：原文模式禁用 + tooltip 说明，点击不改变 store', () => {
    renderToolbar({ sourceAvailable: false });
    const source = screen.getByRole('button', { name: '原文' });
    expect(source).toBeDisabled();
    expect(source.closest('[data-tip]')).toHaveAttribute(
      'data-tip',
      expect.stringContaining('source.pdf'),
    );
    fireEvent.click(source);
    expect(uiStore.getState().previewMode).toBe('target');
  });

  it('bbox 图层三态（段落框 / 版面框 / 关）回调父级', () => {
    const onBboxModeChange = vi.fn();
    renderToolbar({ onBboxModeChange });
    expect(screen.getByRole('button', { name: '段落框' })).toHaveAttribute('aria-pressed', 'true');
    fireEvent.click(screen.getByRole('button', { name: '版面框' }));
    fireEvent.click(screen.getByRole('button', { name: '关' }));
    expect(onBboxModeChange.mock.calls).toEqual([['layout'], ['off']]);
  });

  it('页码输入：合法值提交、越界 clamp、非法值回退；上下页在两端禁用', () => {
    const onPageChange = vi.fn();
    const { rerender } = renderToolbar({ onPageChange });
    const input = screen.getByRole('spinbutton', { name: '页码' });

    fireEvent.change(input, { target: { value: '5' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(onPageChange).toHaveBeenLastCalledWith(5);

    fireEvent.change(input, { target: { value: '99' } });
    fireEvent.blur(input);
    expect(onPageChange).toHaveBeenLastCalledWith(21);

    fireEvent.change(input, { target: { value: '' } });
    fireEvent.blur(input);
    expect(onPageChange).toHaveBeenCalledTimes(2);
    expect((input as HTMLInputElement).value).toBe('1');

    expect(screen.getByRole('button', { name: '上一页' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '下一页' })).toBeEnabled();
    rerender(
      <PreviewToolbar
        page={21}
        pageCount={21}
        scale={1}
        sourceAvailable
        paged
        onPageChange={onPageChange}
        onBboxModeChange={() => {}}
      />,
    );
    expect(screen.getByRole('button', { name: '下一页' })).toBeDisabled();
    expect(screen.getByText('/ 21 页')).toBeInTheDocument();
  });

  it('无产物 PDF 时翻页与缩放控件禁用', () => {
    renderToolbar({ paged: false, scale: 1.25 });
    expect(screen.getByRole('button', { name: '下一页' })).toBeDisabled();
    expect(screen.getByRole('spinbutton', { name: '页码' })).toBeDisabled();
    expect(screen.getByRole('spinbutton', { name: '缩放百分比' })).toHaveValue(125);
    expect(screen.getByRole('button', { name: '放大' })).toBeDisabled();
  });

  it('工具条带 data-od-id（§7.9）', () => {
    renderToolbar();
    const toolbar = screen.getByRole('toolbar', { name: '预览工具条' });
    expect(toolbar).toHaveAttribute('data-od-id', 'preview-toolbar');
    expect(window.localStorage.getItem(STORAGE_KEYS.bboxMode)).toBeNull();
  });
});

describe('reader controls', () => {
  it('zoom buttons, numeric zoom, fit width and link toggle update session state', () => {
    renderToolbar();
    fireEvent.click(screen.getByRole('button', { name: '放大' }));
    expect(uiStore.getState().previewZoom).toBe(1.2);
    const input = screen.getByRole('spinbutton', { name: '缩放百分比' });
    fireEvent.change(input, { target: { value: '175' } });
    fireEvent.blur(input);
    expect(uiStore.getState().previewZoom).toBe(1.75);
    fireEvent.click(screen.getByRole('button', { name: '适宽' }));
    expect(uiStore.getState().previewZoom).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: '对照' }));
    fireEvent.click(screen.getByRole('button', { name: '解除联动' }));
    expect(uiStore.getState().compareLinked).toBe(false);
  });
});
