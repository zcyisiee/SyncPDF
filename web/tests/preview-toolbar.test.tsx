/** `PreviewToolbar`：模式切换、原文模式禁用态与 tooltip、翻页按钮 + 页码提交、缩放控件、
 * bbox 三态（原文框 / 译文框 / 关）。导出/下载不在工具条上（在右栏归档 tab）。 */
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

  it('bbox 图层三态（原文框 / 译文框 / 关）回调父级', () => {
    const onBboxModeChange = vi.fn();
    renderToolbar({ onBboxModeChange });
    expect(screen.getByRole('button', { name: '原文框' })).toHaveAttribute('aria-pressed', 'true');
    fireEvent.click(screen.getByRole('button', { name: '译文框' }));
    fireEvent.click(screen.getByRole('button', { name: '关' }));
    expect(onBboxModeChange.mock.calls).toEqual([['layout'], ['off']]);
  });

  it('翻页按钮：首/末页禁用，点击翻页走 clampPage 后的页码', () => {
    const onPageChange = vi.fn();
    renderToolbar({ page: 3, onPageChange });
    fireEvent.click(screen.getByRole('button', { name: '上一页' }));
    expect(onPageChange).toHaveBeenLastCalledWith(2);
    fireEvent.click(screen.getByRole('button', { name: '下一页' }));
    expect(onPageChange).toHaveBeenLastCalledWith(4);
  });

  it('翻页按钮的禁用条件：第 1 页禁上一页、末页禁下一页、无产物两个都禁、无产物页码也禁', () => {
    const first = renderToolbar({ page: 1 });
    expect(screen.getByRole('button', { name: '上一页' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '下一页' })).toBeEnabled();
    first.unmount();

    const last = renderToolbar({ page: 21 });
    expect(screen.getByRole('button', { name: '上一页' })).toBeEnabled();
    expect(screen.getByRole('button', { name: '下一页' })).toBeDisabled();
    last.unmount();

    renderToolbar({ paged: false, page: 1 });
    expect(screen.getByRole('button', { name: '上一页' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '下一页' })).toBeDisabled();
    expect(screen.getByRole('spinbutton', { name: '页码' })).toBeDisabled();
  });

  it('页码输入：合法值提交、越界 clamp、非法值回退', () => {
    const onPageChange = vi.fn();
    renderToolbar({ onPageChange });
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

    expect(screen.getByText('/ 21 页')).toBeInTheDocument();
  });

  it('缩放：默认适宽；点 ＋ 从 1 起步 ×1.25 → 125%；点 − 回 100%；适宽按钮回 null', () => {
    renderToolbar();
    expect(screen.getByText('适宽')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '适合宽度' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );

    fireEvent.click(screen.getByRole('button', { name: '放大' }));
    expect(uiStore.getState().previewZoom).toBe(1.25);
    expect(screen.getByText('125%')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '适合宽度' })).toHaveAttribute(
      'aria-pressed',
      'false',
    );

    fireEvent.click(screen.getByRole('button', { name: '缩小' }));
    expect(uiStore.getState().previewZoom).toBe(1);
    expect(screen.getByText('100%')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '适合宽度' }));
    expect(uiStore.getState().previewZoom).toBeNull();
    expect(screen.getByText('适宽')).toBeInTheDocument();
  });

  it('缩放步进基于当前值（当前 200%：两次 ＋ 后是 313%）', () => {
    uiStore.setState({ previewZoom: 2 });
    renderToolbar();
    fireEvent.click(screen.getByRole('button', { name: '放大' }));
    expect(uiStore.getState().previewZoom).toBe(2.5);
    fireEvent.click(screen.getByRole('button', { name: '放大' }));
    expect(uiStore.getState().previewZoom).toBe(3.125);
    expect(screen.getByText('313%')).toBeInTheDocument();
  });

  it('bbox 三态直接平铺（不再藏在《更多》里）；工具条单行不换行、没有下载槽', () => {
    renderToolbar();
    expect(screen.getByRole('group', { name: 'bbox 图层' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '原文框' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.queryByText('更多')).toBeNull();
    // 导出/下载入口在右栏归档 tab（用户决策 E），工具条上不再有下载槽
    expect(screen.queryByRole('link', { name: /下载/ })).toBeNull();
    expect(document.querySelector('[data-od-id="download-group"]')).toBeNull();
    expect(document.querySelector('[data-od-id="download-button"]')).toBeNull();
    // 悬停说明（title）在：简洁标签 + 完整解释
    expect(screen.getByRole('button', { name: '译文框' })).toHaveAttribute(
      'title',
      expect.stringContaining('拖拽'),
    );
    const toolbar = screen.getByRole('toolbar', { name: '预览工具条' });
    expect(toolbar.className).toContain('flex-nowrap');
    expect(toolbar.className).not.toContain('flex-wrap');
  });

  it('工具条带 data-od-id（§7.9）与三组 od-id', () => {
    renderToolbar();
    const toolbar = screen.getByRole('toolbar', { name: '预览工具条' });
    expect(toolbar).toHaveAttribute('data-od-id', 'preview-toolbar');
    expect(document.querySelector('[data-od-id="page-nav"]')).not.toBeNull();
    expect(document.querySelector('[data-od-id="zoom-controls"]')).not.toBeNull();
    expect(document.querySelector('[data-od-id="zoom-level"]')).not.toBeNull();
    expect(document.querySelector('[data-od-id="page-count"]')).not.toBeNull();
    expect(window.localStorage.getItem(STORAGE_KEYS.bboxMode)).toBeNull();
  });

  it('文档名与状态徽标住翻页组左侧；两者都不给就不渲染占位', () => {
    const { unmount } = renderToolbar({
      title: 'Attention Is All You Need',
      status: <span>已完成</span>,
    });
    const title = document.querySelector('[data-od-id="toolbar-title"]') as HTMLElement;
    expect(title.textContent).toBe('Attention Is All You Need');
    // brief 指定的排印：可收缩/截断 + 衬线 + md + ink
    expect(title.className).toBe('min-w-0 max-w-[24ch] truncate font-serif text-md text-ink');
    expect(document.querySelector('[data-od-id="toolbar-status"]')?.textContent).toBe('已完成');
    // 顺序：文档名 / 状态 / 翻页组 / bbox 图层 ……（无右侧内容时不给占位元素）
    const toolbar = screen.getByRole('toolbar', { name: '预览工具条' });
    const order = Array.from(toolbar.children).map((node) =>
      node.getAttribute('data-od-id') ?? node.getAttribute('role') ?? node.className,
    );
    expect(order[0]).toBe('toolbar-title');
    expect(order[1]).toBe('toolbar-status');
    expect(order.indexOf('toolbar-status')).toBeLessThan(order.indexOf('group'));
    unmount();

    // 缺省：标题与徽标都不渲染（不留空壳）
    renderToolbar();
    expect(document.querySelector('[data-od-id="toolbar-title"]')).toBeNull();
    expect(document.querySelector('[data-od-id="toolbar-status"]')).toBeNull();
  });
});

describe('reader controls', () => {
  it('compare link toggle updates session state (trackpad/ctrl-wheel zoom still applies)', () => {
    renderToolbar();
    fireEvent.click(screen.getByRole('button', { name: '对照' }));
    fireEvent.click(screen.getByRole('button', { name: '解除联动' }));
    expect(uiStore.getState().compareLinked).toBe(false);
  });
});
