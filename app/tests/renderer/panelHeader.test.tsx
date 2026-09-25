/**
 * @vitest-environment jsdom
 *
 * 面板头（VSCode 侧栏式）：多视图 = 图标切换排 + 标题行；单视图只有标题行；inline 并成一行。
 */
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { IconButton, PanelHeader } from '@/layout/Panel';

afterEach(cleanup);

const VIEWS = [
  { id: 'a' as const, title: '块详情', icon: 'inspect' },
  { id: 'b' as const, title: '问题', icon: 'warning', badge: 3 },
];

describe('PanelHeader', () => {
  it('视图切换是只有图标的按钮，标题行显示当前视图名，点击切换', () => {
    const onSelect = vi.fn();
    const { container } = render(<PanelHeader views={VIEWS} active="a" onSelect={onSelect} />);
    const tabs = screen.getAllByRole('tab');
    expect(tabs.map((tab) => tab.getAttribute('aria-label'))).toEqual(['块详情', '问题']);
    // 按钮里没有文字标签，只有 codicon（徽标数字除外）
    expect(tabs[0].textContent).toBe('');
    expect(tabs[0].querySelector('.codicon-inspect')).not.toBeNull();
    expect(tabs[1].textContent).toBe('3');
    expect(tabs[0].getAttribute('aria-selected')).toBe('true');
    expect(container.querySelector('.sp-panel-title')?.textContent).toBe('块详情');
    fireEvent.click(tabs[1]);
    expect(onSelect).toHaveBeenCalledWith('b');
    expect(container.querySelectorAll('header')).toHaveLength(2);
  });

  it('单视图不渲染切换排；inline 把切换排和标题并成一行', () => {
    const single = render(<PanelHeader views={[VIEWS[0]]} active="a" actions={<span data-testid="act" />} />);
    expect(single.queryAllByRole('tab')).toHaveLength(0);
    expect(single.getByTestId('act')).toBeTruthy();
    single.unmount();

    const inline = render(<PanelHeader inline views={VIEWS} active="b" />);
    expect(inline.container.querySelectorAll('header')).toHaveLength(1);
    expect(inline.container.querySelector('.sp-panel-title')?.textContent).toBe('问题');
  });
});

describe('IconButton', () => {
  it('开关用 aria-pressed，单选用 aria-checked', () => {
    render(
      <>
        <IconButton icon="eye" title="显示版面框" active onClick={() => {}} />
        <IconButton icon="globe" title="译文" role="radio" active={false} onClick={() => {}} />
      </>,
    );
    expect(screen.getByLabelText('显示版面框').getAttribute('aria-pressed')).toBe('true');
    const radio = screen.getByRole('radio');
    expect(radio.getAttribute('aria-checked')).toBe('false');
    expect(radio.hasAttribute('aria-pressed')).toBe(false);
  });
});
