/** `PreviewToolbar`：模式切换、原文模式禁用态与 tooltip、页码提交、bbox 三态与下载槽。
 * 缩放控件已删除（触控板捏合 / Ctrl+滚轮直接缩放），bbox 与下载从《更多》移出平铺。 */
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

  it('bbox 图层三态（段落框 / 版面框 / 关）回调父级', () => {
    const onBboxModeChange = vi.fn();
    renderToolbar({ onBboxModeChange });
    expect(screen.getByRole('button', { name: '段落框' })).toHaveAttribute('aria-pressed', 'true');
    fireEvent.click(screen.getByRole('button', { name: '版面框' }));
    fireEvent.click(screen.getByRole('button', { name: '关' }));
    expect(onBboxModeChange.mock.calls).toEqual([['layout'], ['off']]);
  });

  it('页码输入：合法值提交、越界 clamp、非法值回退（没有上一页/下一页按钮，滚轮/触控板翻页）', () => {
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

    // 上一页/下一页按钮已删除（触控板滚动即可连续翻页；Windows 按住右键拖滚轮同理）
    expect(screen.queryByRole('button', { name: '上一页' })).toBeNull();
    expect(screen.queryByRole('button', { name: '下一页' })).toBeNull();
    expect(screen.getByText('/ 21 页')).toBeInTheDocument();
  });

  it('没有缩放控件（触控板捏合 / Ctrl+滚轮缩放）；无产物时页码禁用', () => {
    renderToolbar({ paged: false });
    expect(screen.queryByRole('button', { name: '放大' })).toBeNull();
    expect(screen.queryByRole('button', { name: '缩小' })).toBeNull();
    expect(screen.queryByRole('button', { name: '适宽' })).toBeNull();
    expect(screen.queryByRole('spinbutton', { name: '缩放百分比' })).toBeNull();
    expect(screen.getByRole('spinbutton', { name: '页码' })).toBeDisabled();
  });

  it('bbox 三态与下载槽直接平铺（不再藏在《更多》里）', () => {
    renderToolbar({ download: <a href="#dl">下载 PDF</a> });
    expect(screen.getByRole('group', { name: 'bbox 图层' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '段落框' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('link', { name: '下载 PDF' })).toBeInTheDocument();
    expect(screen.queryByText('更多')).toBeNull();
    // 悬停说明（title）在：简洁标签 + 完整解释
    expect(screen.getByRole('button', { name: '版面框' })).toHaveAttribute(
      'title',
      expect.stringContaining('拖拽'),
    );
  });

  it('工具条带 data-od-id（§7.9）', () => {
    renderToolbar();
    const toolbar = screen.getByRole('toolbar', { name: '预览工具条' });
    expect(toolbar).toHaveAttribute('data-od-id', 'preview-toolbar');
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
    // 顺序：文档名 / 状态 / 翻页组 / bbox 图层 …… 下载槽在最右
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
  it('compare link toggle updates session state (zoom lives on the trackpad/ctrl-wheel)', () => {
    renderToolbar();
    fireEvent.click(screen.getByRole('button', { name: '对照' }));
    fireEvent.click(screen.getByRole('button', { name: '解除联动' }));
    expect(uiStore.getState().compareLinked).toBe(false);
  });
});
