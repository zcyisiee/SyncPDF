import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { App } from '../src/app/App';
import { PaperNav } from '../src/components/shell/PaperNav';
import { parseHash } from '../src/lib/routing';
import { jsonResponse, mockApiFetch, renderWithQuery, resetUiStore } from './helpers';

/** 左栏（PaperNav）在测试里要的 `route` 参数：按 hash 解析，与 App 的用法一致。 */
function nav(hash: string, activeDid: string | null = null) {
  return <PaperNav activeDid={activeDid} route={parseHash(hash)} />;
}

beforeEach(() => {
  resetUiStore();
});

describe('App 三栏外壳（hash 路由）', () => {
  it('#/library 中栏渲染空态卡（选一篇或上传）', async () => {
    window.location.hash = '#/library';
    mockApiFetch({
      '/api/v1/documents': () => jsonResponse([]),
      '/api/v1/glossary': () => jsonResponse({ entries: [], count: 0 }),
    });
    const click = vi.spyOn(HTMLInputElement.prototype, 'click');
    renderWithQuery(<App />);
    expect(await screen.findByRole('heading', { name: '选择或上传一篇论文' })).toBeInTheDocument();
    expect(document.querySelector('[data-od-id="library-empty"]')).not.toBeNull();

    // 中栏的「上传 PDF」与左栏共用同一个文件选择器（左栏那个隐藏 input）
    const cta = screen.getByRole('button', { name: /上传 PDF/ });
    fireEvent.click(cta);
    await waitFor(() => expect(click).toHaveBeenCalledTimes(1));
    expect(click.mock.instances[0]).toBe(document.querySelector('[data-od-id="upload-input"]'));
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
    mockApiFetch({ '/api/v1/documents': () => jsonResponse([]) });
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

  it('旧外壳已删除：任何路由都没有顶栏 / 图标栏 / 底部时间线', async () => {
    mockApiFetch({
      '/api/v1/documents': () => jsonResponse([]),
      '/api/v1/glossary': () => jsonResponse({ entries: [], count: 0 }),
    });
    window.location.hash = '#/library';
    const { unmount } = renderWithQuery(<App />);
    await screen.findByRole('heading', { name: '选择或上传一篇论文' });
    for (const odId of ['app-topbar', 'icon-rail', 'timeline']) {
      expect(document.querySelector(`[data-od-id="${odId}"]`)).toBeNull();
    }
    unmount();

    // 工作台也没有旧外壳：只有右栏（检查器）+ 左栏 + 中栏
    window.location.hash = '#/d/alpha/progress';
    mockApiFetch({
      '/api/v1/documents': () => jsonResponse([]),
      '/api/v1/documents/alpha': () =>
        jsonResponse({ error: { code: 'document_not_found', message: '文档不存在' } }, 404),
    });
    renderWithQuery(<App />);
    await screen.findByRole('alert');
    for (const odId of ['app-topbar', 'icon-rail', 'timeline']) {
      expect(document.querySelector(`[data-od-id="${odId}"]`)).toBeNull();
    }
  });

  it('左栏论文导航在所有路由常驻（含底部两个全局链接）', async () => {
    const routes: { hash: string; mock: Record<string, () => Response> }[] = [
      {
        hash: '#/library',
        mock: { '/api/v1/documents': () => jsonResponse([]) },
      },
      { hash: '#/glossary', mock: { '/api/v1/glossary': () => jsonResponse({ entries: [], count: 0 }) } },
      {
        hash: '#/settings',
        mock: {
          '/api/v1/glossary': () => jsonResponse({ entries: [], count: 0 }),
          '/api/v1/profiles': () => jsonResponse([]),
        },
      },
      {
        hash: '#/d/nope/progress',
        mock: {
          '/api/v1/documents/nope': () =>
            jsonResponse({ error: { code: 'document_not_found', message: '文档不存在' } }, 404),
        },
      },
      { hash: '#/nope', mock: {} },
    ];

    for (const { hash, mock } of routes) {
      window.location.hash = hash;
      mockApiFetch({ '/api/v1/documents': () => jsonResponse([]), ...mock });
      const { unmount } = renderWithQuery(<App />);
      const navRail = (await screen.findByRole('complementary', {
        name: '论文导航',
      })) as HTMLElement;
      expect(navRail).toHaveAttribute('data-od-id', 'nav-rail');
      expect(within(navRail).getByRole('link', { name: '词表' })).toHaveAttribute(
        'href',
        '#/glossary',
      );
      expect(within(navRail).getByRole('link', { name: '设置' })).toHaveAttribute(
        'href',
        '#/settings',
      );
      unmount();
    }
  });

  it('工作台路由套在三栏骨架里（左栏常驻 + 中栏 main + 右栏检查器）', async () => {
    window.location.hash = '#/d/alpha/progress';
    mockApiFetch({
      '/api/v1/documents': () => jsonResponse([]),
      '/api/v1/documents/alpha': () =>
        jsonResponse({
          did: 'alpha',
          title: 'Attention Is All You Need',
          pages: 21,
          paragraph_count: 420,
          translated_count: 206,
          stage_summary: { parse: 'ok', translate: 'ok', apply: 'ok', build: 'ok', check: 'ok', review: 'ok', report: 'ok' },
          updated_at: '2026-09-16T13:28:29.000Z',
          pdf: { source: null, outputs: [] },
          config: null,
          quality: {
            check: { verdict: 'pass', blockers: [], warnings: [], reasons: [], at: null },
            reviewer: { status: 'pass', fix_rounds: {}, at: null },
            pipeline_ok: true,
          },
          compile: { status: 'none', revision: 0, stale: false, artifact: null },
          available: {
            run_state: true,
            anchors: true,
            translated: true,
            geometry: true,
            parse_snapshot: true,
            review_verdict: true,
            layout_lint: true,
            link_audit: true,
          },
        }),
      '/api/v1/glossary': () => jsonResponse({ entries: [], count: 0 }),
      '/api/v1/profiles': () => jsonResponse([]),
    });
    renderWithQuery(<App />);

    // 工作台 = 中栏 doc-header（文档名 + 徽标 + 任务控制）+ 右栏检查器（三 tab）
    const header = (await screen.findByText('已完成')).closest(
      '[data-od-id="doc-header"]',
    ) as HTMLElement;
    expect(header).not.toBeNull();
    expect(within(header).getByText('Attention Is All You Need')).toBeInTheDocument();
    const inspector = document.querySelector('[data-od-id="inspector"]') as HTMLElement;
    expect(inspector).not.toBeNull();
    const tablist = within(inspector).getByRole('tablist', { name: '右侧面板' });
    for (const label of ['段落', '事件流', '归档']) {
      expect(within(tablist).getByRole('tab', { name: label })).toBeInTheDocument();
    }
    // 骨架三层齐全，旧外壳仍不存在
    expect(document.querySelector('.app-grid')).not.toBeNull();
    expect(document.querySelector('[data-od-id="nav-rail"]')).not.toBeNull();
    expect(document.querySelector('[data-od-id="app-main"]')).not.toBeNull();
    expect(document.querySelector('[data-od-id="app-topbar"]')).toBeNull();
    expect(document.querySelector('[data-od-id="icon-rail"]')).toBeNull();
    expect(document.querySelector('[data-od-id="timeline"]')).toBeNull();
  });

  it('did 不存在时中栏只给错误卡（左栏与骨架仍在）', async () => {
    window.location.hash = '#/d/nope/progress';
    mockApiFetch({
      '/api/v1/documents': () => jsonResponse([]),
      '/api/v1/documents/nope': () =>
        jsonResponse({ error: { code: 'document_not_found', message: '文档不存在' } }, 404),
      '/api/v1/glossary': () => jsonResponse({ entries: [], count: 0 }),
    });
    renderWithQuery(<App />);
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText('文档不存在')).toBeInTheDocument();
    expect(document.querySelector('.app-grid')).not.toBeNull();
    expect(document.querySelector('[data-od-id="nav-rail"]')).not.toBeNull();
    expect(document.querySelector('[data-od-id="app-main"]')).not.toBeNull();
  });
});

describe('PaperNav 直接渲染（底部链接高亮按路由）', () => {
  it('词表路由时词表链接标记 aria-current，设置路由时反之', async () => {
    mockApiFetch({ '/api/v1/documents': () => jsonResponse([]) });
    const first = renderWithQuery(nav('#/glossary'));
    const glossaryLink = await screen.findByRole('link', { name: '词表' });
    expect(glossaryLink).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('link', { name: '设置' })).not.toHaveAttribute('aria-current');
    first.unmount();

    renderWithQuery(nav('#/settings'));
    expect(await screen.findByRole('link', { name: '设置' })).toHaveAttribute(
      'aria-current',
      'page',
    );
  });
});
