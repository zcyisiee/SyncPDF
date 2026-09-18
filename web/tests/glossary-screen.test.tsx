/**
 * W13 词表屏（`#/glossary`）：表格编辑 / 整表保存 / 放弃 / 删行 / CSV 导入导出 / 清空 /
 * 不回溯提示。
 *
 * 红线的界面证据：
 * - 「保存」只在真有改动且无行级错误时可用，发的是**整表** `PUT /glossary`；
 * - 顶部常驻「对已翻译段落无追溯效果」提示条（`data-od-id="glossary-no-retro"`）；
 * - CSV 的解析在浏览器里做（后端只收 JSON）。
 */
import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { GlossaryScreen } from '../src/screens/GlossaryScreen';
import { jsonResponse, mockApiFetch, renderWithQuery, resetUiStore } from './helpers';

const ENTRIES = [
  { source: 'attention', target: '注意力', note: null },
  { source: 'LTO', target: '链接时优化', note: '厂商写法' },
];

const GLOSSARY = { entries: ENTRIES, count: ENTRIES.length };

interface RecordedCall {
  url: string;
  method: string;
  body: unknown;
}

/** mock：GET /glossary 返回给定表；记录所有方法的调用（PUT/DELETE 都能断言）。 */
function mockGlossary(routes: Record<string, () => Response | Promise<Response>> = {}) {
  const calls: RecordedCall[] = [];
  const fetchMock = mockApiFetch({ '/api/v1/glossary': () => jsonResponse(GLOSSARY), ...routes });
  fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? 'GET').toUpperCase();
    calls.push({ url, method, body: init?.body === undefined ? undefined : JSON.parse(String(init.body)) });
    const table: Record<string, () => Response | Promise<Response>> = {
      '/api/v1/glossary': () => jsonResponse(GLOSSARY),
      ...routes,
    };
    const route = table[url];
    return route ? await route() : jsonResponse({ error: { code: 'not_found', message: url } }, 404);
  });
  return { fetchMock, calls };
}

/** jsdom 没有 `File#text`：给这一个文件实例补上（屏幕只用 `file.text()`）。 */
function csvFile(text: string, name = 'glossary.csv'): File {
  const file = new File([text], name, { type: 'text/csv' });
  Object.defineProperty(file, 'text', { value: async () => text });
  return file;
}

/** 导出用的 blob 内容（jsdom 没有 `Blob#text`，用 FileReader 读）。 */
async function blobText(blob: Blob): Promise<string> {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(new Error('读不到导出的 blob'));
    reader.readAsText(blob);
  });
}

beforeEach(() => {
  resetUiStore();
});

describe('GlossaryScreen（全局词表）', () => {
  it('渲染服务端的表 + 常驻「不回溯」提示条', async () => {
    mockGlossary();
    renderWithQuery(<GlossaryScreen />);

    const table = await screen.findByRole('table');
    expect(within(table).getAllByRole('row')).toHaveLength(3); // 表头 + 2 行
    expect(screen.getByDisplayValue('attention')).toBeInTheDocument();
    expect(screen.getByDisplayValue('链接时优化')).toBeInTheDocument();
    expect(screen.getByDisplayValue('厂商写法')).toBeInTheDocument();

    const notice = document.querySelector('[data-od-id="glossary-no-retro"]');
    expect(notice).not.toBeNull();
    expect(notice?.textContent).toContain('对已翻译段落无追溯效果');
  });

  it('没有保存/清空失败时不得渲染「请求失败 / null」假错误卡（TanStack error=null 回归）', async () => {
    // TanStack v5 里 mutation 无错时 error 是 null（不是 undefined）：
    // 旧代码 `failure === undefined` 判空把 null 当成真错误，屏上常驻「请求失败 / null」。
    mockGlossary();
    renderWithQuery(<GlossaryScreen />);

    await screen.findByRole('table');
    expect(screen.queryByRole('alert')).toBeNull();
    expect(document.querySelector('[data-od-id="glossary-save-error"]')).toBeNull();
  });

  it('没有词表时给空态引导，不渲染空表格', async () => {
    mockGlossary({ '/api/v1/glossary': () => jsonResponse({ entries: [], count: 0 }) });
    renderWithQuery(<GlossaryScreen />);
    expect(await screen.findByText(/还没有词表/)).toBeInTheDocument();
    expect(screen.queryByRole('table')).toBeNull();
  });

  it('添加行 → 编辑 → 保存：发整表 PUT，成功后回落到服务端那一份', async () => {
    const saved = {
      entries: [
        { source: 'attention', target: '注意力', note: null },
        { source: 'LTO', target: '链接时优化', note: '厂商写法' },
        { source: 'beam search', target: '束搜索', note: null },
      ],
      count: 3,
    };
    const { calls } = mockGlossary({
      '/api/v1/glossary': () => jsonResponse(GLOSSARY),
    });
    // PUT 返回服务端规范化后的表（这里就是三条）
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? 'GET').toUpperCase();
      calls.push({
        url,
        method,
        body: init?.body === undefined ? undefined : JSON.parse(String(init.body)),
      });
      if (method === 'PUT') return jsonResponse(saved);
      if (url === '/api/v1/glossary') return jsonResponse(GLOSSARY);
      return jsonResponse({ error: { code: 'not_found', message: url } }, 404);
    });

    renderWithQuery(<GlossaryScreen />);
    await screen.findByRole('table');
    // 没有改动时不能保存（整表编辑：没改就没必要写）
    expect(screen.getByRole('button', { name: '保存' })).toBeDisabled();

    fireEvent.click(screen.getByRole('button', { name: '添加行' }));
    const cells = document.querySelectorAll('[data-od-id="glossary-cell-source"]');
    fireEvent.change(cells[cells.length - 1], { target: { value: 'beam search' } });
    const targets = document.querySelectorAll('[data-od-id="glossary-cell-target"]');
    fireEvent.change(targets[targets.length - 1], { target: { value: '束搜索' } });

    expect(await screen.findByText(/有未保存的修改/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '保存' }));

    await waitFor(() => expect(calls.filter((call) => call.method === 'PUT')).toHaveLength(1));
    const put = calls.find((call) => call.method === 'PUT');
    expect(put?.url).toBe('/api/v1/glossary');
    expect(put?.body).toEqual({
      entries: [
        { source: 'attention', target: '注意力', note: null },
        { source: 'LTO', target: '链接时优化', note: '厂商写法' },
        { source: 'beam search', target: '束搜索', note: null },
      ],
    });
    // 保存成功 → 本地草稿清空（`有未保存的修改` 消失，保存按钮回到禁用）
    await waitFor(() => expect(screen.queryByText(/有未保存的修改/)).toBeNull());
    expect(screen.getByRole('button', { name: '保存' })).toBeDisabled();
  });

  it('行级错误（只填一边）挡住保存', async () => {
    mockGlossary();
    renderWithQuery(<GlossaryScreen />);
    await screen.findByRole('table');

    const sources = document.querySelectorAll('[data-od-id="glossary-cell-source"]');
    fireEvent.change(sources[0], { target: { value: '' } }); // attention 行的 source 清空

    expect(await screen.findByText(/行不合法/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '保存' })).toBeDisabled();
  });

  it('放弃：把本地改动丢掉，回到服务端那一份', async () => {
    mockGlossary();
    renderWithQuery(<GlossaryScreen />);
    await screen.findByRole('table');

    const sources = document.querySelectorAll('[data-od-id="glossary-cell-source"]');
    fireEvent.change(sources[0], { target: { value: 'attn' } });
    expect(screen.getByDisplayValue('attn')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: '放弃' }));
    expect(screen.getByDisplayValue('attention')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '放弃' })).toBeDisabled();
  });

  it('删行：只改本地草稿，保存时才整表替换', async () => {
    const { calls } = mockGlossary();
    renderWithQuery(<GlossaryScreen />);
    await screen.findByRole('table');

    // 删行的可访问名是「删除第 N 行」——用 data-od-id 取出来更直接
    const deleteButtons = document.querySelectorAll('[data-od-id="glossary-delete-row"]');
    expect(deleteButtons).toHaveLength(2);
    fireEvent.click(deleteButtons[0]);
    expect(within(screen.getByRole('table')).getAllByRole('row')).toHaveLength(2);
    expect(calls.filter((call) => call.method === 'PUT')).toHaveLength(0);

    fireEvent.click(screen.getByRole('button', { name: '保存' }));
    await waitFor(() => expect(calls.filter((call) => call.method === 'PUT')).toHaveLength(1));
    expect(calls.find((call) => call.method === 'PUT')?.body).toEqual({
      entries: [{ source: 'LTO', target: '链接时优化', note: '厂商写法' }],
    });
  });

  it('导入 CSV：浏览器解析后进本地草稿（不直接发请求）', async () => {
    const { calls } = mockGlossary();
    renderWithQuery(<GlossaryScreen />);
    await screen.findByRole('table');

    const input = document.querySelector('[data-od-id="glossary-import-input"]') as HTMLInputElement;
    fireEvent.change(input, {
      target: { files: [csvFile('source,target,note\nbeam search,束搜索,beam search 的译名\n')] },
    });

    expect(await screen.findByDisplayValue('beam search')).toBeInTheDocument();
    expect(screen.getByDisplayValue('束搜索')).toBeInTheDocument();
    expect(screen.queryByDisplayValue('attention')).toBeNull(); // 整表替换成导入的内容
    expect(calls.filter((call) => call.method === 'PUT')).toHaveLength(0); // 还没保存
  });

  it('导入坏 CSV：给出明确报错，不动现有表', async () => {
    mockGlossary();
    renderWithQuery(<GlossaryScreen />);
    await screen.findByRole('table');

    const input = document.querySelector('[data-od-id="glossary-import-input"]') as HTMLInputElement;
    fireEvent.change(input, { target: { files: [csvFile('term,translation\na,b\n')] } });

    expect(await screen.findByText('CSV 导入失败')).toBeInTheDocument();
    expect(screen.getByText(/必须含 source 与 target/)).toBeInTheDocument();
    expect(screen.getByDisplayValue('attention')).toBeInTheDocument();
  });

  it('导出 CSV：本地生成（含未保存的编辑），列顺序 source,target,note', async () => {
    const blobs: Blob[] = [];
    const createObjectURL = vi.fn((blob: Blob | MediaSource) => {
      blobs.push(blob as Blob);
      return 'blob:mock';
    });
    vi.stubGlobal('URL', { ...URL, createObjectURL, revokeObjectURL: vi.fn() });

    mockGlossary();
    renderWithQuery(<GlossaryScreen />);
    await screen.findByRole('table');

    const sources = document.querySelectorAll('[data-od-id="glossary-cell-source"]');
    fireEvent.change(sources[0], { target: { value: 'attn' } });
    fireEvent.click(screen.getByRole('button', { name: '导出 CSV' }));

    await waitFor(() => expect(blobs).toHaveLength(1));
    const text = await blobText(blobs[0]);
    expect(text.split('\n')[0]).toBe('source,target,note');
    expect(text).toContain('attn,注意力,'); // 导出的是屏幕上的表（含未保存的编辑）
    expect(text).toContain('LTO,链接时优化,厂商写法');
  });

  it('清空：confirm 后 DELETE，表变空态', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    const { calls } = mockGlossary({
      '/api/v1/glossary': () => jsonResponse(GLOSSARY),
    });
    const fetchMock = vi.mocked(fetch);
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      const method = (init?.method ?? 'GET').toUpperCase();
      calls.push({ url, method, body: undefined });
      if (method === 'DELETE') return jsonResponse({ entries: [], count: 0 });
      return jsonResponse(GLOSSARY);
    });

    renderWithQuery(<GlossaryScreen />);
    await screen.findByRole('table');
    fireEvent.click(screen.getByRole('button', { name: '清空' }));

    await waitFor(() => expect(calls.filter((call) => call.method === 'DELETE')).toHaveLength(1));
    expect(await screen.findByText(/还没有词表/)).toBeInTheDocument();
  });
});
