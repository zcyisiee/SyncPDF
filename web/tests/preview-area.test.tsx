/**
 * `PreviewArea` 的占位/降级/模式分支（pdf.js 本体渲染不在 jsdom 单测范围，这里 mock
 * `src/lib/pdf`；真渲染由 `e2e/preview.spec.ts` 在 Chromium 里覆盖）。
 */
import { act, fireEvent, screen, within } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { PreviewArea } from '../src/components/preview/PreviewArea';
import { InspectorPanel } from '../src/components/shell/InspectorPanel';
import { STORAGE_KEYS, uiStore } from '../src/stores/ui';
import { jsonResponse, mockApiFetch, renderWithQuery, resetUiStore } from './helpers';

vi.mock('../src/lib/pdf', () => ({
  PDF_CMAP_URL: '/pdfjs/cmaps/',
  PDF_STANDARD_FONT_DATA_URL: '/pdfjs/standard_fonts/',
  // 永不 resolve：组件停在“正在加载 PDF…”，单测不碰真 pdf.js / canvas
  loadPdfDocument: () => ({ promise: new Promise(() => {}), destroy: async () => {} }),
}));

const DID = 'ccs3764-dyn';

const DETAIL = {
  did: DID,
  title: null,
  pages: 21,
  paragraph_count: 420,
  translated_count: 206,
  stage_summary: {},
  updated_at: '2026-09-16T13:28:29.000Z',
  pdf: { source: null, outputs: [] },
  config: null,
  quality: {},
  compile: {},
  available: {},
};

const MONO = {
  name: 'output/ccs2026b-paper3764.no_watermark.zh.mono.pdf',
  path: 'output/ccs2026b-paper3764.no_watermark.zh.mono.pdf',
  kind: 'pdf',
  size: 6257602,
  mtime: '2026-09-15T17:03:14.105Z',
};

const PARSE_GEOMETRY = {
  did: DID,
  kind: 'parse',
  coord_system: 'pdf_topleft',
  page: 1,
  run_id: 'run-1',
  entities: [
    {
      id: 'P01-001',
      kind: 'paragraph',
      label: 'title',
      page: 1,
      box: { x0: 66.585, y0: 78.41, x1: 544.64, y1: 119.647 },
    },
  ],
  relations: [],
};

function mockPreview(options: {
  artifacts?: unknown;
  artifactsStatus?: number;
  geometry?: () => Response;
} = {}) {
  const artifacts = options.artifacts ?? [MONO];
  return mockApiFetch({
    [`/api/v1/documents/${DID}`]: () => jsonResponse(DETAIL),
    [`/api/v1/documents/${DID}/artifacts`]: () =>
      options.artifactsStatus === undefined
        ? jsonResponse(artifacts)
        : jsonResponse(artifacts, options.artifactsStatus),
    [`/api/v1/documents/${DID}/geometry?kind=parse&page=1`]: () =>
      options.geometry?.() ?? jsonResponse(PARSE_GEOMETRY),
  });
}

beforeEach(() => {
  resetUiStore();
});

describe('PreviewArea 产物与空态', () => {
  it('清单里没有产物 PDF → 明确占位卡（数据来自 /artifacts，不造假）', async () => {
    mockPreview({ artifacts: [{ ...MONO, name: 'agent/translated.json', kind: 'json' }] });
    renderWithQuery(<PreviewArea did={DID} view="progress" />);
    expect(await screen.findByText('无产物 PDF')).toBeInTheDocument();
    expect(document.querySelector('[data-od-id="preview-no-pdf"]')).not.toBeNull();
    expect(screen.getByRole('toolbar', { name: '预览工具条' })).toBeInTheDocument();
  });

  it('有产物 PDF → 画布容器带 data-od-id（jsdom 里停在加载态）', async () => {
    mockPreview();
    renderWithQuery(<PreviewArea did={DID} view="progress" />);
    expect(await screen.findByText('正在加载 PDF…')).toBeInTheDocument();
    expect(document.querySelector('[data-od-id="preview-canvas"]')).not.toBeNull();
    expect(document.querySelector('[data-od-id="preview-loading"]')).not.toBeNull();
    expect(document.querySelector('[data-od-id="pdf-canvas"]')).toBeNull();
  });

  it('产物清单 500 → 错误卡 + 重试', async () => {
    mockPreview({
      artifacts: { error: { code: 'internal_error', message: '炸了' } },
      artifactsStatus: 500,
    });
    renderWithQuery(<PreviewArea did={DID} view="progress" />);
    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText('读取产物清单失败')).toBeInTheDocument();
    expect(within(alert).getByRole('button', { name: '重试' })).toBeInTheDocument();
  });
});

describe('PreviewArea bbox 降级与模式', () => {
  it('geometry 404 → 小条提示「该页无解析数据」，预览仍可用', async () => {
    mockPreview({
      geometry: () =>
        jsonResponse({ error: { code: 'snapshot_unavailable', message: '没有快照' } }, 404),
    });
    renderWithQuery(<PreviewArea did={DID} view="progress" />);
    expect(await screen.findByText(/该页无解析数据/)).toBeInTheDocument();
    // 产物到达后画布容器仍在（预览不受 bbox 缺失影响），但没有 bbox 层
    expect(await screen.findByText('正在加载 PDF…')).toBeInTheDocument();
    expect(document.querySelector('[data-od-id="preview-canvas"]')).not.toBeNull();
    expect(document.querySelector('[data-od-id="bbox-layer"]')).toBeNull();
  });

  it('bbox 图层关（持久化 ieet.bboxMode=off）→ 不发 geometry 请求、无提示条', async () => {
    window.localStorage.setItem(STORAGE_KEYS.bboxMode, 'off');
    uiStore.setState({ bboxMode: 'off' });
    const fetchMock = mockPreview();
    renderWithQuery(<PreviewArea did={DID} view="progress" />);
    await screen.findByText('正在加载 PDF…');
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes('/geometry'))).toBe(false);
    expect(document.querySelector('[data-od-id="preview-bbox-unavailable"]')).toBeNull();
  });

  it('原文模式缺 source.pdf → 明确出口按钮可切回译文', async () => {
    uiStore.setState({ previewMode: 'source' });
    mockPreview();
    renderWithQuery(<PreviewArea did={DID} view="progress" />);
    expect(await screen.findByText('该文档没有 source.pdf')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: '切换到译文' }));
    expect(uiStore.getState().previewMode).toBe('target');
  });

  it('对照模式缺 source.pdf → 右侧译侧仍有画布，左侧给出说明', async () => {
    uiStore.setState({ previewMode: 'compare' });
    mockPreview();
    renderWithQuery(<PreviewArea did={DID} view="progress" />);
    expect(await screen.findByText(/对照模式的左侧不可用/)).toBeInTheDocument();
    expect(await screen.findByText('正在加载 PDF…')).toBeInTheDocument();
    expect(document.querySelector('[data-od-id="preview-canvas"]')).not.toBeNull();
    expect(document.querySelector('[data-od-id="preview-canvas-source"]')).toBeNull();
  });

  it('选中的段落 id 反映在右侧面板（点框选中的联动出口）', async () => {
    mockPreview();
    renderWithQuery(
      <>
        <PreviewArea did={DID} view="progress" />
        <InspectorPanel />
      </>,
    );
    await screen.findByText('正在加载 PDF…');
    expect(screen.getByText(/点击预览里的段落框查看该段/)).toBeInTheDocument();

    act(() => uiStore.getState().setSelectedParagraph('P01-001'));
    expect(await screen.findByText('已选中段落')).toBeInTheDocument();
    expect(document.querySelector('[data-od-id="selected-paragraph-id"]')?.textContent).toBe(
      'P01-001',
    );
  });
});
