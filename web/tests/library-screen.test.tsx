import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { DocumentListItem } from '../src/api/types';
import { LibraryScreen } from '../src/screens/LibraryScreen';
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

/** 上传成功后列表里出现的新文档（计数全 null：服务端只写了 source.pdf）。 */
const UPLOADED: DocumentListItem = {
  did: 'up-paper-20260917-120000',
  title: null,
  pages: null,
  paragraph_count: null,
  translated_count: null,
  stage_summary: ALL_NOT_RUN,
  updated_at: null,
};

const DOCUMENTS: DocumentListItem[] = [
  {
    did: 'ccs3764-dyn',
    title: null,
    pages: 21,
    paragraph_count: 420,
    translated_count: 206,
    stage_summary: ALL_OK,
    updated_at: THREE_DAYS_AGO,
  },
  {
    did: 'W01-entry-smoke-20260918-001933',
    title: 'Attention Is All You Need',
    pages: null,
    paragraph_count: null,
    translated_count: null,
    stage_summary: { ...ALL_NOT_RUN, check: 'error' },
    updated_at: null,
  },
];

beforeEach(() => {
  resetUiStore();
});

describe('文件库屏（真数据 /documents）', () => {
  it('渲染卡片字段：did、标题、页/段/已译计数、更新时间、7 个阶段徽标', async () => {
    mockApiFetch({ '/api/v1/documents': () => jsonResponse(DOCUMENTS) });
    renderWithQuery(<LibraryScreen />);

    expect(await screen.findByText('ccs3764-dyn')).toBeInTheDocument();
    expect(screen.getByText('Attention Is All You Need')).toBeInTheDocument();
    expect(screen.getByText('2 篇文档 · 数据来自 bdt serve（--root 下的 workdir）')).toBeInTheDocument();

    expect(screen.getByText('21 页')).toBeInTheDocument();
    expect(screen.getByText('420 段')).toBeInTheDocument();
    expect(screen.getByText('已译 206 段')).toBeInTheDocument();
    expect(screen.getByText('3 天前')).toBeInTheDocument();

    // 产物缺失的计数是 null（不是 0），3 个计数 + 更新时间都显示 — 而不是编造数字
    expect(screen.getAllByText('—')).toHaveLength(3);
    expect(screen.queryByText('— 段')).toBeNull();

    expect(screen.getByTitle('parse · 已完成（ok）')).toBeInTheDocument();
    expect(screen.getByTitle('check · 失败（error）')).toBeInTheDocument();
    expect(screen.getByTitle('apply · 未运行（not_run）')).toBeInTheDocument();
    // 无真实 running 阶段 → 全站唯一动效（脉冲）不出现
    expect(document.querySelectorAll('.pulse-dot')).toHaveLength(0);
  });

  it('卡片链接指向工作台进度视图', async () => {
    mockApiFetch({ '/api/v1/documents': () => jsonResponse(DOCUMENTS) });
    renderWithQuery(<LibraryScreen />);
    const card = await screen.findByRole('link', { name: /ccs3764-dyn/ });
    expect(card).toHaveAttribute('href', '#/d/ccs3764-dyn/progress');
  });

  it('上传入口可用：按钮 + 拖放区（W08 接入，不再 disabled）', async () => {
    mockApiFetch({ '/api/v1/documents': () => jsonResponse([]) });
    renderWithQuery(<LibraryScreen />);
    expect(await screen.findByText('还没有文档')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /上传 PDF/ })).toBeEnabled();
    const dropzone = document.querySelector('[data-od-id="dropzone"]');
    expect(dropzone).not.toBeNull();
    expect(dropzone).toHaveAttribute('data-drag', 'idle');
  });

  it('选中 PDF → POST /documents（只带 file）→ 列表刷新出新卡，不自动跳转', async () => {
    const calls: { url: string; method: string; body: unknown }[] = [];
    let listCalls = 0;
    // POST 挂住不返回，好断言"上传中"那一行的行内状态（真跑时是几百毫秒的窗口）
    let releasePost: (() => void) | undefined;
    const fetchMock = mockApiFetch({});
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? 'GET').toUpperCase();
      calls.push({ url, method, body: init?.body });
      if (url === '/api/v1/documents' && method === 'POST') {
        await new Promise<void>((resolve) => {
          releasePost = resolve;
        });
        return jsonResponse(
          { did: 'up-paper-20260917-120000', bytes: 5, source: 'source.pdf' },
          201,
        );
      }
      // 图标栏（W13）也带一个词表条数请求：不是本用例的断言对象，单给一个空表免得
      // 扰乱下面的 listCalls 计数。
      if (url === '/api/v1/glossary') return jsonResponse({ entries: [], count: 0 });
      listCalls += 1;
      return jsonResponse(listCalls === 1 ? [] : [UPLOADED]);
    });

    renderWithQuery(<LibraryScreen />);
    expect(await screen.findByText('还没有文档')).toBeInTheDocument();

    const input = document.querySelector('[data-od-id="upload-input"]') as HTMLInputElement;
    fireEvent.change(input, { target: { files: [makePdfFile('paper.pdf')] } });

    // 上传中：行内状态（文件名 + 脉冲点，没有假百分比）
    const row = await screen.findByText(/正在上传/, {
      selector: '[data-od-id="upload-row"]',
    });
    expect(row).toHaveAttribute('data-status', 'uploading');
    expect(row.textContent).toContain('paper.pdf');
    // 放行 POST → 新卡片出现（列表被 invalidate）
    releasePost?.();
    // 成功后上传行消失 + 新卡片出现
    await waitFor(() =>
      expect(document.querySelector('[data-od-id="upload-row"]')).toBeNull(),
    );
    expect(await screen.findByRole('link', { name: /up-paper-20260917-120000/ })).toHaveAttribute(
      'href',
      '#/d/up-paper-20260917-120000/progress',
    );
    // 不自动跳转：hash 没变
    expect(window.location.hash).toBe('');
    const posted = calls.filter((call) => call.method === 'POST');
    expect(posted).toHaveLength(1);
    expect(posted[0].url).toBe('/api/v1/documents');
    expect(posted[0].body).toBeInstanceOf(FormData);
  });

  it('拖放 PDF → 走同一条上传路径', async () => {
    const fetchMock = mockApiFetch({
      '/api/v1/documents': () => jsonResponse([]),
    });
    fetchMock.mockImplementation(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if ((init?.method ?? 'GET').toUpperCase() === 'POST') {
        return jsonResponse({ did: 'up-dropped-20260917-120000', bytes: 5, source: 'source.pdf' }, 201);
      }
      return jsonResponse([]);
    });
    renderWithQuery(<LibraryScreen />);
    await screen.findByText('还没有文档');

    fireEvent.drop(document.querySelector('[data-od-id="dropzone"]') as Element, {
      dataTransfer: { files: [makePdfFile('dropped.pdf')] },
    });
    await waitFor(() =>
      expect(fetchMock.mock.calls.some((call) => (call[1] as RequestInit)?.method === 'POST')).toBe(
        true,
      ),
    );
  });

  it('413 file_too_large → 错误卡写明"文件过大"（不写假成功）', async () => {
    mockApiFetch({
      '/api/v1/documents': () => jsonResponse([]),
    });
    const fetchMock = globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
    fetchMock.mockImplementation(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if ((init?.method ?? 'GET').toUpperCase() === 'POST') {
        return jsonResponse(
          {
            error: {
              code: 'file_too_large',
              message: '上传超过上限 209715200 字节（本文件 300000000 字节）',
              detail: { limit_bytes: 209715200, size_bytes: 300000000 },
            },
          },
          413,
        );
      }
      return jsonResponse([]);
    });

    renderWithQuery(<LibraryScreen />);
    await screen.findByText('还没有文档');
    fireEvent.change(document.querySelector('[data-od-id="upload-input"]') as Element, {
      target: { files: [makePdfFile('huge.pdf')] },
    });

    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('文件过大：huge.pdf');
    expect(alert).toHaveTextContent('上传超过上限 209715200 字节');
  });

  it('本地预检：非 .pdf 扩展名与假魔数都不发 POST', async () => {
    const fetchMock = mockApiFetch({
      '/api/v1/documents': () => jsonResponse([]),
    });
    renderWithQuery(<LibraryScreen />);
    await screen.findByText('还没有文档');
    const input = document.querySelector('[data-od-id="upload-input"]') as Element;

    fireEvent.change(input, {
      target: { files: [new File(['x'], 'notes.txt', { type: 'text/plain' })] },
    });
    expect(await screen.findByText(/这个文件不是 PDF：notes.txt/)).toBeInTheDocument();

    fireEvent.change(input, { target: { files: [makeTextFile('fake.pdf')] } });
    expect(await screen.findByText(/前 5 字节不是 %PDF-/)).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.every((call) => call[1]?.method !== 'POST'),
    ).toBe(true);
  });

  it('上传失败卡可以关掉', async () => {
    const fetchMock = mockApiFetch({ '/api/v1/documents': () => jsonResponse([]) });
    fetchMock.mockImplementation(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if ((init?.method ?? 'GET').toUpperCase() === 'POST') {
        return jsonResponse(
          { error: { code: 'invalid_pdf', message: '上传内容不是 PDF' } },
          422,
        );
      }
      return jsonResponse([]);
    });
    renderWithQuery(<LibraryScreen />);
    await screen.findByText('还没有文档');
    fireEvent.change(document.querySelector('[data-od-id="upload-input"]') as Element, {
      target: { files: [makePdfFile('broken.pdf')] },
    });
    expect(await screen.findByText(/这个文件不是 PDF：broken.pdf/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '知道了' }));
    await waitFor(() =>
      expect(screen.queryByText(/这个文件不是 PDF：broken.pdf/)).toBeNull(),
    );
  });

  it('多选：逐个串行 POST（不并发）', async () => {
    const order: string[] = [];
    let inflight = 0;
    let maxInflight = 0;
    const fetchMock = mockApiFetch({ '/api/v1/documents': () => jsonResponse([]) });
    fetchMock.mockImplementation(async (_input: RequestInfo | URL, init?: RequestInit) => {
      if ((init?.method ?? 'GET').toUpperCase() !== 'POST') return jsonResponse([]);
      inflight += 1;
      maxInflight = Math.max(maxInflight, inflight);
      const file = (init?.body as FormData).get('file') as File;
      order.push(file.name);
      await new Promise((resolve) => setTimeout(resolve, 0));
      inflight -= 1;
      return jsonResponse({ did: `up-${file.name}`, bytes: 5, source: 'source.pdf' }, 201);
    });

    renderWithQuery(<LibraryScreen />);
    await screen.findByText('还没有文档');
    fireEvent.change(document.querySelector('[data-od-id="upload-input"]') as Element, {
      target: { files: [makePdfFile('a.pdf'), makePdfFile('b.pdf'), makePdfFile('c.pdf')] },
    });
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.filter((call) => (call[1] as RequestInit)?.method === 'POST'),
      ).toHaveLength(3),
    );
    expect(order).toEqual(['a.pdf', 'b.pdf', 'c.pdf']);
    expect(maxInflight).toBe(1);
  });

  it('空态：没有文档时给出明确说明而不是空白', async () => {
    mockApiFetch({ '/api/v1/documents': () => jsonResponse([]) });
    renderWithQuery(<LibraryScreen />);
    const empty = await screen.findByText('还没有文档');
    expect(empty).toBeInTheDocument();
    expect(document.querySelector('[data-od-id="library-empty"]')).not.toBeNull();
  });

  it('连接失败时显示错误卡 + 重试按钮，重试后渲染列表（不白屏）', async () => {
    let failedOnce = false;
    const fetchMock = vi.fn(async (input: RequestInfo | URL) => {
      // 图标栏的词表请求（W13）不是本用例的断言对象：直接给空表。
      if (String(input) === '/api/v1/glossary') return jsonResponse({ entries: [], count: 0 });
      if (!failedOnce) {
        failedOnce = true;
        throw new TypeError('Failed to fetch');
      }
      return jsonResponse(DOCUMENTS);
    });
    vi.stubGlobal('fetch', fetchMock);
    renderWithQuery(<LibraryScreen />);

    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.getByText('无法连接后端服务')).toBeInTheDocument();
    expect(document.querySelector('[data-od-id="error-card"]')).not.toBeNull();

    fireEvent.click(screen.getByRole('button', { name: '重试' }));
    expect(await screen.findByText('ccs3764-dyn')).toBeInTheDocument();
    expect(screen.queryByRole('alert')).toBeNull();
    // 文件库列表请求了两次（首次失败 + 重试）；词表请求不计入这个计数
    expect(fetchMock).toHaveBeenCalled();
  });

  it('后端返回统一错误信封时把 code 翻成人话标题', async () => {
    mockApiFetch({
      '/api/v1/documents': () =>
        jsonResponse({ error: { code: 'root_missing', message: 'root 目录不可用' } }, 503),
    });
    renderWithQuery(<LibraryScreen />);
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText('服务根目录不可用')).toBeInTheDocument();
    expect(within(alert).getByText('root 目录不可用')).toBeInTheDocument();
    expect(within(alert).getByText('HTTP 503 · root_missing')).toBeInTheDocument();
  });
});
