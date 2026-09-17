import { fireEvent, screen, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { DocumentListItem } from '../src/api/types';
import { LibraryScreen } from '../src/screens/LibraryScreen';
import { jsonResponse, mockApiFetch, renderWithQuery, resetUiStore } from './helpers';

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

  it('上传按钮与拖放区已渲染但禁用（W08 接入）', async () => {
    mockApiFetch({ '/api/v1/documents': () => jsonResponse([]) });
    renderWithQuery(<LibraryScreen />);
    expect(await screen.findByText('还没有文档')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /上传 PDF/ })).toBeDisabled();
    expect(document.querySelector('[data-od-id="dropzone"]')).toHaveAttribute(
      'aria-disabled',
      'true',
    );
    expect(document.querySelector('[data-tip="W08 接入"]')).not.toBeNull();
  });

  it('空态：没有文档时给出明确说明而不是空白', async () => {
    mockApiFetch({ '/api/v1/documents': () => jsonResponse([]) });
    renderWithQuery(<LibraryScreen />);
    const empty = await screen.findByText('还没有文档');
    expect(empty).toBeInTheDocument();
    expect(document.querySelector('[data-od-id="library-empty"]')).not.toBeNull();
  });

  it('连接失败时显示错误卡 + 重试按钮，重试后渲染列表（不白屏）', async () => {
    let calls = 0;
    const fetchMock = vi.fn(async () => {
      calls += 1;
      if (calls === 1) throw new TypeError('Failed to fetch');
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
    expect(calls).toBe(2);
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
