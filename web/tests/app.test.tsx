import { screen } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';

import { App } from '../src/app/App';
import { jsonResponse, mockApiFetch, renderWithQuery, resetUiStore } from './helpers';

beforeEach(() => {
  resetUiStore();
});

describe('App 路由（hash）', () => {
  it('#/library 渲染文件库屏', async () => {
    window.location.hash = '#/library';
    mockApiFetch({ '/api/v1/documents': () => jsonResponse([]) });
    renderWithQuery(<App />);
    expect(await screen.findByRole('heading', { name: '文件库' })).toBeInTheDocument();
  });

  it('#/glossary 与 #/settings 渲染占位屏（标题 + 后续版本提供）', async () => {
    window.location.hash = '#/glossary';
    renderWithQuery(<App />);
    expect(await screen.findByRole('heading', { name: '词表' })).toBeInTheDocument();
    expect(screen.getAllByText('后续版本提供')).toHaveLength(1);
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
    renderWithQuery(<App />);
    expect(await screen.findByRole('heading', { name: '设置' })).toBeInTheDocument();
  });

  it('同步 ieet.screen（屏状态的唯一写入点）', async () => {
    window.location.hash = '#/glossary';
    renderWithQuery(<App />);
    await screen.findByRole('heading', { name: '词表' });
    expect(window.localStorage.getItem('ieet.screen')).toBe('glossary');
  });

  it('图标栏一级导航含文件库/词表/设置三个入口', async () => {
    window.location.hash = '#/library';
    mockApiFetch({ '/api/v1/documents': () => jsonResponse([]) });
    renderWithQuery(<App />);
    const rail = await screen.findByRole('navigation', { name: '全局导航' });
    expect(rail.querySelectorAll('[data-rail-item]')).toHaveLength(3);
    expect(screen.getByRole('link', { name: '词表' })).toHaveAttribute('href', '#/glossary');
  });
});
