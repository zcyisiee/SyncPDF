import { screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';

import { App } from '../src/app/App';
import { IconRail } from '../src/components/shell/IconRail';
import { jsonResponse, mockApiFetch, renderWithQuery, resetUiStore } from './helpers';

beforeEach(() => {
  resetUiStore();
});

describe('App 路由（hash）', () => {
  it('#/library 渲染文件库屏', async () => {
    window.location.hash = '#/library';
    mockApiFetch({
      '/api/v1/documents': () => jsonResponse([]),
      '/api/v1/glossary': () => jsonResponse({ entries: [], count: 0 }),
    });
    renderWithQuery(<App />);
    expect(await screen.findByRole('heading', { name: '文件库' })).toBeInTheDocument();
  });

  it('#/glossary 渲染真词表屏（W13），#/settings 渲染翻译配置屏', async () => {
    window.location.hash = '#/glossary';
    mockApiFetch({ '/api/v1/glossary': () => jsonResponse({ entries: [], count: 0 }) });
    const { unmount } = renderWithQuery(<App />);
    expect(await screen.findByRole('heading', { name: '词表' })).toBeInTheDocument();
    expect(await screen.findByText(/对已翻译段落无追溯效果/)).toBeInTheDocument();
    unmount();

    window.location.hash = '#/settings';
    renderWithQuery(<App />);
    expect(await screen.findByRole('heading', { name: '设置' })).toBeInTheDocument();
    expect(screen.getByText('翻译偏好')).toBeInTheDocument();
  });

  it('未知 hash 给出明确出口', async () => {
    window.location.hash = '#/nope';
    renderWithQuery(<App />);
    expect(await screen.findByRole('heading', { name: '页面不存在' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '返回文件库' })).toHaveAttribute('href', '#/library');
  });

  it('首屏没有 hash 时回落到 ieet.screen 记的屏', async () => {
    window.location.hash = '';
    window.localStorage.setItem('ieet.screen', 'settings');
    mockApiFetch({ '/api/v1/glossary': () => jsonResponse({ entries: [], count: 0 }) });
    renderWithQuery(<App />);
    expect(await screen.findByRole('heading', { name: '设置' })).toBeInTheDocument();
  });

  it('同步 ieet.screen（屏状态的唯一写入点）', async () => {
    window.location.hash = '#/glossary';
    mockApiFetch({ '/api/v1/glossary': () => jsonResponse({ entries: [], count: 0 }) });
    renderWithQuery(<App />);
    await screen.findByRole('heading', { name: '词表' });
    expect(window.localStorage.getItem('ieet.screen')).toBe('glossary');
  });

  it('图标栏一级导航含文件库/词表/设置三个入口', async () => {
    window.location.hash = '#/library';
    mockApiFetch({
      '/api/v1/documents': () => jsonResponse([]),
      '/api/v1/glossary': () => jsonResponse({ entries: [], count: 0 }),
    });
    renderWithQuery(<App />);
    const rail = await screen.findByRole('navigation', { name: '全局导航' });
    expect(rail.querySelectorAll('[data-rail-item]')).toHaveLength(3);
    expect(screen.getByRole('link', { name: '词表' })).toHaveAttribute('href', '#/glossary');
  });

  it('词表条数徐标（W13）：有词表时显示条数，空表时不显示', async () => {
    // 直接渲染 IconRail：`<App/>` 用的是**模块级** queryClient（见 App.tsx），
    // 跨用例缓存会让数据驱动的断言变成顺序依赖（本文件前一个用例已经把它置成空表）。
    mockApiFetch({
      '/api/v1/glossary': () =>
        jsonResponse({ entries: [{ source: 'a', target: '甲', note: null }], count: 3 }),
    });
    const first = renderWithQuery(<IconRail />);
    const badge = await screen.findByText('3');
    expect(badge).toHaveAttribute('data-od-id', 'rail-glossary-badge');
    expect(screen.getByRole('link', { name: '词表' })).toHaveAttribute('title', '词表（3 条）');
    first.unmount();

    mockApiFetch({ '/api/v1/glossary': () => jsonResponse({ entries: [], count: 0 }) });
    renderWithQuery(<IconRail />);
    await screen.findByRole('navigation', { name: '全局导航' });
    await waitFor(() =>
      expect(document.querySelector('[data-od-id="rail-glossary-badge"]')).toBeNull(),
    );
  });
});
