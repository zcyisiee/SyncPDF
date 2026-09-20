import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { DocumentListItem } from '../src/api/types';
import { PaperNav } from '../src/components/shell/PaperNav';
import { parseHash } from '../src/lib/routing';
import {
  jsonResponse,
  makePdfFile,
  makeTextFile,
  mockApiFetch,
  renderWithQuery,
  resetUiStore,
} from './helpers';

const ALL_OK = {
  parse: 'ok',
  translate: 'ok',
  apply: 'ok',
  build: 'ok',
  check: 'ok',
  review: 'ok',
  report: 'ok',
};

const ALL_NOT_RUN = {
  parse: 'not_run',
  translate: 'not_run',
  apply: 'not_run',
  build: 'not_run',
  check: 'not_run',
  review: 'not_run',
  report: 'not_run',
};

const THREE_DAYS_AGO = new Date(Date.now() - 3 * 24 * 60 * 60 * 1000).toISOString();

const DOCUMENTS: DocumentListItem[] = [
  {
    did: 'ccs3764-dyn',
    title: 'Attention Is All You Need',
    pages: 21,
    paragraph_count: 420,
    translated_count: 206,
    stage_summary: ALL_OK,
    updated_at: THREE_DAYS_AGO,
  },
  {
    did: 'W01-entry-smoke-20260918-001933',
    title: 'Transformer 综述',
    pages: null,
    paragraph_count: null,
    translated_count: null,
    stage_summary: { ...ALL_NOT_RUN, check: 'error' },
    updated_at: null,
  },
];

function renderNav(activeDid: string | null = null, hash = '#/library') {
  return renderWithQuery(<PaperNav activeDid={activeDid} route={parseHash(hash)} />);
}

function cardOf(did: string): HTMLElement {
  return document.querySelector(`[data-od-id="paper-${did}"]`) as HTMLElement;
}

beforeEach(() => {
  resetUiStore();
});

describe('PaperNav 论文卡（真数据 /documents）', () => {
  it('渲染卡片字段：标题、页/段/已译计数、更新时间、状态 chip、迷你进度条', async () => {
    mockApiFetch({ '/api/v1/documents': () => jsonResponse(DOCUMENTS) });
    renderNav();

    expect(await screen.findByText('Attention Is All You Need')).toBeInTheDocument();
    expect(screen.getByText('Transformer 综述')).toBeInTheDocument();
    // 节头数量 = 过滤后篇数
    expect(screen.getByText('论文 · 2')).toBeInTheDocument();

    const first = cardOf('ccs3764-dyn');
    expect(within(first).getByText(/21 页/)).toBeInTheDocument();
    expect(within(first).getByText(/420 段/)).toBeInTheDocument();
    expect(within(first).getByText(/已译 206 段/)).toBeInTheDocument();
    expect(within(first).getByText('已完成')).toBeInTheDocument();
    // 206/420 段已译 → 进度条存在且宽度按比例
    const bar = first.querySelector('[data-od-id="paper-progress"]') as HTMLElement;
    expect(bar).not.toBeNull();
    expect(bar.style.width).toBe('49%');

    // 产物缺失的计数是 null（不是 0）：逐项显示 — ；分母缺失 → 不画进度条
    const second = cardOf('W01-entry-smoke-20260918-001933');
    const meta = within(second).getByText((text) => text.includes('已译 —'));
    expect(meta.textContent).toContain('— · —');
    expect(second.querySelector('[data-od-id="paper-progress"]')).toBeNull();
    expect(within(second).getByText('失败')).toBeInTheDocument();
    // 没有真实 running 阶段 → 全站唯一动效（脉冲）不出现
    expect(document.querySelectorAll('.pulse-dot')).toHaveLength(0);
  });

  it('未开始 / 部分完成的文档分别用 idle（未开始）与 warn（未完成）chip', async () => {
    mockApiFetch({
      '/api/v1/documents': () =>
        jsonResponse([
          { ...DOCUMENTS[1], did: 'fresh', title: '全新文档', stage_summary: ALL_NOT_RUN, paragraph_count: 12, translated_count: 0, pages: 3 },
          { ...DOCUMENTS[1], did: 'partial', title: '半成品', stage_summary: { ...ALL_NOT_RUN, parse: 'ok' }, paragraph_count: 12, translated_count: 0, pages: 3 },
        ]),
    });
    renderNav();

    expect(await screen.findByText('未开始')).toBeInTheDocument();
    const fresh = cardOf('fresh');
    // 分母 12、已译 0：进度条存在但宽度为 0（不编造进度）
    expect((fresh.querySelector('[data-od-id="paper-progress"]') as HTMLElement).style.width).toBe(
      '0%',
    );
    expect(within(cardOf('partial')).getByText('未完成')).toBeInTheDocument();
    // 没有真实 running 阶段 → 不出现脉冲
    expect(document.querySelectorAll('.pulse-dot')).toHaveLength(0);
  });

  it('点击卡片跳转到工作台进度视图', async () => {
    mockApiFetch({ '/api/v1/documents': () => jsonResponse(DOCUMENTS) });
    renderNav();
    fireEvent.click(await screen.findByText('Attention Is All You Need'));
    expect(window.location.hash).toBe('#/d/ccs3764-dyn/progress');
  });

  it('当前文档卡高亮并标 aria-current', async () => {
    mockApiFetch({ '/api/v1/documents': () => jsonResponse(DOCUMENTS) });
    renderNav('ccs3764-dyn', '#/d/ccs3764-dyn/progress');

    const active = await screen.findByRole('button', { name: /Attention Is All You Need/ });
    expect(active).toHaveAttribute('aria-current', 'true');
    expect(active.className).toContain('bg-accent-soft');
    const other = cardOf('W01-entry-smoke-20260918-001933');
    expect(other).not.toHaveAttribute('aria-current');
  });

  it('搜索框按标题 / did 客户端过滤（不区分大小写），空串不过滤', async () => {
    mockApiFetch({ '/api/v1/documents': () => jsonResponse(DOCUMENTS) });
    renderNav();
    await screen.findByText('Attention Is All You Need');
    const search = screen.getByLabelText('搜索论文（标题 / did）');

    fireEvent.change(search, { target: { value: 'attention' } });
    expect(screen.getByText('Attention Is All You Need')).toBeInTheDocument();
    expect(screen.queryByText('Transformer 综述')).toBeNull();
    expect(screen.getByText('论文 · 1')).toBeInTheDocument();

    fireEvent.change(search, { target: { value: 'W01-ENTRY' } });
    expect(await screen.findByText('Transformer 综述')).toBeInTheDocument();
    expect(screen.queryByText('Attention Is All You Need')).toBeNull();

    fireEvent.change(search, { target: { value: 'zzz' } });
    expect(screen.getByText('没有匹配的论文')).toBeInTheDocument();

    fireEvent.change(search, { target: { value: '' } });
    expect(screen.getByText('论文 · 2')).toBeInTheDocument();
  });

  it('列表末尾的上传按钮触发左栏唯一的文件选择器', async () => {
    mockApiFetch({ '/api/v1/documents': () => jsonResponse([]) });
    const click = vi.spyOn(HTMLInputElement.prototype, 'click');
    renderNav();
    await screen.findByText('还没有文档');

    fireEvent.click(screen.getByRole('button', { name: '上传论文 PDF' }));
    expect(click).toHaveBeenCalledTimes(1);
    // 节头的 icon 按钮走同一条路径
    fireEvent.click(screen.getByRole('button', { name: '上传论文' }));
    expect(click).toHaveBeenCalledTimes(2);
  });

  it('空列表给出说明；加载失败显示错误卡 + 重试', async () => {
    mockApiFetch({ '/api/v1/documents': () => jsonResponse([]) });
    const { unmount } = renderNav();
    expect(await screen.findByText('还没有文档')).toBeInTheDocument();
    expect(screen.getByText('拖一个 PDF 进来，或点上面的上传按钮')).toBeInTheDocument();
    unmount();

    let failedOnce = false;
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        if (!failedOnce) {
          failedOnce = true;
          throw new TypeError('Failed to fetch');
        }
        return jsonResponse(DOCUMENTS);
      }),
    );
    renderNav();
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText('无法连接后端服务')).toBeInTheDocument();
    fireEvent.click(within(alert).getByRole('button', { name: '重试' }));
    expect(await screen.findByText('Attention Is All You Need')).toBeInTheDocument();
  });
});

describe('PaperNav 上传（POST /documents，串行 + 预检）', () => {
  it('选中 PDF → 行内「正在上传」→ 成功后行消失并刷新列表', async () => {
    const calls: (RequestInit | undefined)[] = [];
    let releasePost: (() => void) | undefined;
    let listCalls = 0;
    const fetchMock = mockApiFetch({});
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url === '/api/v1/documents' && (init?.method ?? 'GET').toUpperCase() === 'POST') {
        calls.push(init);
        await new Promise<void>((resolve) => {
          releasePost = resolve;
        });
        return jsonResponse({ did: 'up-paper-20260917-120000', bytes: 5, source: 'source.pdf' }, 201);
      }
      listCalls += 1;
      return jsonResponse(listCalls === 1 ? [] : [DOCUMENTS[0]]);
    });

    renderNav();
    await screen.findByText('还没有文档');
    fireEvent.change(document.querySelector('[data-od-id="upload-input"]') as Element, {
      target: { files: [makePdfFile('paper.pdf')] },
    });

    const row = await screen.findByText(/正在上传/, {
      selector: '[data-od-id="upload-row"]',
    });
    expect(row).toHaveAttribute('data-status', 'uploading');
    expect(row.textContent).toContain('paper.pdf');

    releasePost?.();
    await waitFor(() => expect(document.querySelector('[data-od-id="upload-row"]')).toBeNull());
    expect(await screen.findByText('Attention Is All You Need')).toBeInTheDocument();
    expect(calls).toHaveLength(1);
    expect(calls[0]?.body).toBeInstanceOf(FormData);
  });

  it('上传失败：行内错误卡 + 「知道了」关掉（POST 500）', async () => {
    const fetchMock = mockApiFetch({ '/api/v1/documents': () => jsonResponse([]) });
    fetchMock.mockImplementation(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if ((init?.method ?? 'GET').toUpperCase() === 'POST') {
        return jsonResponse(
          { error: { code: 'internal_error', message: '磁盘写入失败' } },
          500,
        );
      }
      return jsonResponse([]);
    });

    renderNav();
    await screen.findByText('还没有文档');
    fireEvent.change(document.querySelector('[data-od-id="upload-input"]') as Element, {
      target: { files: [makePdfFile('broken.pdf')] },
    });

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('broken.pdf');
    expect(alert).toHaveTextContent('磁盘写入失败');
    fireEvent.click(screen.getByRole('button', { name: '知道了' }));
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
  });

  it('本地预检：非 PDF 扩展名与假魔数都不发 POST', async () => {
    const fetchMock = mockApiFetch({ '/api/v1/documents': () => jsonResponse([]) });
    renderNav();
    await screen.findByText('还没有文档');
    const input = document.querySelector('[data-od-id="upload-input"]') as Element;

    fireEvent.change(input, {
      target: { files: [new File(['x'], 'notes.txt', { type: 'text/plain' })] },
    });
    expect(await screen.findByText(/这个文件不是 PDF：notes.txt/)).toBeInTheDocument();

    fireEvent.change(input, { target: { files: [makeTextFile('fake.pdf')] } });
    expect(await screen.findByText(/前 5 字节不是 %PDF-/)).toBeInTheDocument();
    expect(fetchMock.mock.calls.every((call) => call[1]?.method !== 'POST')).toBe(true);
  });

  it('拖到左栏任意位置 → 走同一条上传路径', async () => {
    const fetchMock = mockApiFetch({ '/api/v1/documents': () => jsonResponse([]) });
    fetchMock.mockImplementation(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if ((init?.method ?? 'GET').toUpperCase() === 'POST') {
        return jsonResponse({ did: 'up-dropped', bytes: 5, source: 'source.pdf' }, 201);
      }
      return jsonResponse([]);
    });
    renderNav();
    await screen.findByText('还没有文档');

    const navRail = document.querySelector('[data-od-id="nav-rail"]') as Element;
    fireEvent.drop(navRail, { dataTransfer: { files: [makePdfFile('dropped.pdf')] } });
    await waitFor(() =>
      expect(fetchMock.mock.calls.some((call) => call[1]?.method === 'POST')).toBe(true),
    );
  });
});

describe('PaperNav 右键删除（DELETE /documents/{did}）', () => {
  it('右键弹菜单；确认后发 DELETE 并刷新列表', async () => {
    let documents = DOCUMENTS;
    const fetchMock = mockApiFetch({
      '/api/v1/documents': () => jsonResponse(documents),
      'DELETE /api/v1/documents/ccs3764-dyn': () => {
        documents = DOCUMENTS.filter((doc) => doc.did !== 'ccs3764-dyn');
        return jsonResponse({ did: 'ccs3764-dyn', deleted: true, rows: { documents: 1 } });
      },
    });
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderNav();
    await screen.findByText('Attention Is All You Need');

    fireEvent.contextMenu(cardOf('ccs3764-dyn'));
    const menu = await screen.findByRole('menu');
    expect(menu).toHaveAttribute('data-did', 'ccs3764-dyn');
    fireEvent.click(within(menu).getByRole('menuitem', { name: /删除此文档/ }));

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith(
        expect.stringContaining('/api/v1/documents/ccs3764-dyn'),
        expect.objectContaining({ method: 'DELETE' }),
      ),
    );
    await waitFor(() => expect(screen.queryByText('Attention Is All You Need')).toBeNull());
    expect(screen.getByText('Transformer 综述')).toBeInTheDocument();
  });

  it('取消确认时不发请求', async () => {
    const fetchMock = mockApiFetch({ '/api/v1/documents': () => jsonResponse(DOCUMENTS) });
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    renderNav();
    await screen.findByText('Attention Is All You Need');

    fireEvent.contextMenu(cardOf('ccs3764-dyn'));
    fireEvent.click(await screen.findByRole('menuitem', { name: /删除此文档/ }));
    expect(
      fetchMock.mock.calls.filter(
        ([, init]) => (init as RequestInit | undefined)?.method === 'DELETE',
      ),
    ).toHaveLength(0);
    expect(screen.getByText('Attention Is All You Need')).toBeInTheDocument();
  });

  it('有活动任务时展示服务端原文（document_busy），卡片不被清掉', async () => {
    mockApiFetch({
      '/api/v1/documents': () => jsonResponse(DOCUMENTS),
      'DELETE /api/v1/documents/ccs3764-dyn': () =>
        jsonResponse(
          {
            error: {
              code: 'document_busy',
              message: '该文档有活动任务（running），先取消或等它结束再删',
            },
          },
          409,
        ),
    });
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderNav();
    await screen.findByText('Attention Is All You Need');

    fireEvent.contextMenu(cardOf('ccs3764-dyn'));
    fireEvent.click(await screen.findByRole('menuitem', { name: /删除此文档/ }));

    const error = await screen.findByText(/该文档有活动任务/);
    expect(error).toHaveAttribute('data-od-id', 'doc-card-delete-error');
    expect(screen.getByText('Attention Is All You Need')).toBeInTheDocument();
  });

  it('菜单是单一实例，Esc 关闭', async () => {
    mockApiFetch({ '/api/v1/documents': () => jsonResponse(DOCUMENTS) });
    renderNav();
    await screen.findByText('Attention Is All You Need');

    fireEvent.contextMenu(cardOf('ccs3764-dyn'));
    expect(await screen.findAllByRole('menu')).toHaveLength(1);
    fireEvent.keyDown(window, { key: 'Escape' });
    await waitFor(() => expect(screen.queryByRole('menu')).toBeNull());

    fireEvent.contextMenu(cardOf('W01-entry-smoke-20260918-001933'));
    const menus = await screen.findAllByRole('menu');
    expect(menus).toHaveLength(1);
    expect(menus[0]).toHaveAttribute('data-did', 'W01-entry-smoke-20260918-001933');
  });
});
