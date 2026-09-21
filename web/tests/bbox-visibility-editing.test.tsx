import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import { beforeEach, expect, it, vi } from 'vitest';
import type { ComponentProps } from 'react';
import type { ContinuousPdfPane } from '../src/components/preview/ContinuousPdfPane';
import { PreviewArea } from '../src/components/preview/PreviewArea';
import { useBboxStore } from '../src/stores/bbox';
import { uiStore } from '../src/stores/ui';
import { jsonResponse, makeViewport, mockApiFetch, renderWithQuery, resetUiStore } from './helpers';

// Exercise the real PreviewArea editing gate with local geometry, without PDF loading.
vi.mock('../src/components/preview/ContinuousPdfPane', () => ({
  ContinuousPdfPane: (props: ComponentProps<typeof ContinuousPdfPane>) =>
    <div>{props.overlay?.(makeViewport({ scale: 1 }))}</div>,
}));
vi.mock('../src/components/edit/BboxEditor', () => ({
  BboxEditor: () => <div data-testid="editing-overlay" />,
}));
beforeEach(() => {
  localStorage.clear();
  resetUiStore();
  useBboxStore.setState({ documents: {}, strokeWidth: 1, fillOpacity: 0.08 });
});
it('hiding a selected category removes its editor even when selected from elsewhere; no data writes', async () => {
  const fetch = mockApiFetch({
    '/api/v1/documents/local': () => jsonResponse({ did: 'local', pages: 1, compile: null }),
    '/api/v1/documents/local/artifacts': () => jsonResponse([{ name: 'output/test.mono.pdf', kind: 'pdf' }]),
    '/api/v1/documents/local/geometry?kind=layout&page=1': () => jsonResponse({
      coord_system: 'pdf_native', paragraphs: [{ id: 'p1', layout_label: 'title', layout_box: [1, 2, 10, 20] }],
    }),
  });
  // 译文框图层（几何 mock 是 pdf_native）：合并视图后没有按视图切默认值，显式选 layout
  act(() => uiStore.getState().setBboxMode('layout'));
  renderWithQuery(<PreviewArea did="local" />);
  await screen.findByLabelText('title');
  act(() => uiStore.getState().setSelectedParagraph('p1'));
  expect(screen.getByTestId('editing-overlay')).toBeInTheDocument();
  fireEvent.click(screen.getByLabelText('title'));
  expect(screen.queryByTestId('editing-overlay')).toBeNull();
  act(() => { uiStore.getState().setSelectedParagraph(null); uiStore.getState().setSelectedParagraph('p1'); });
  expect(screen.queryByTestId('editing-overlay')).toBeNull();
  fireEvent.click(screen.getByLabelText('title'));
  expect(screen.getByTestId('editing-overlay')).toBeInTheDocument();
  act(() => uiStore.getState().setBboxMode('off'));
  await waitFor(() => expect(screen.queryByTestId('editing-overlay')).toBeNull());
  expect(fetch.mock.calls.every(([, init]) => !init?.method || init.method === 'GET')).toBe(true);
});
/**
 * 「译文框」(target) 是**只读**叠加层：拖拽编辑层的准入只看 layerMode === 'layout'。
 *
 * 两档的坐标系不同（target 是 pdf_topleft、layout 是 pdf_native）。若把 target 的框
 * 接了拖拽，写回的坐标就会按 layout 语义解释，「我拖的框」和「我看到的框」会分家，
 * 所以这里守的是「翻译侧重新识别的框不可编辑」这条边界。
 *
 * 本文件把 ContinuousPdfPane 换成了只渲染 overlay 的替身，所以这里只验编辑层的有无；
 * 叠加层本身的渲染（data-bbox-mode）在 bbox-layer.test.tsx 里验。
 */
it('译文框档位不给拖拽 overlay；排版框档位仍给（同一段）', async () => {
  mockApiFetch({
    '/api/v1/documents/local': () => jsonResponse({ did: 'local', pages: 1, compile: null }),
    '/api/v1/documents/local/artifacts': () => jsonResponse([{ name: 'output/test.mono.pdf', kind: 'pdf' }]),
    '/api/v1/documents/local/geometry?kind=target&page=1': () => jsonResponse({
      did: 'local', kind: 'target', coord_system: 'pdf_topleft', page: 1,
      recognition_entities: [{ id: 'provider:block:p0-b0', kind: 'block', label: 'text',
        page: 1, box: { x0: 70.5, y0: 90.25, x1: 500.75, y1: 130 }, parent_id: null, paragraph_id: null }],
      recognition: { status: 'ok', reason: null }, entities: [], relations: [],
      labels: [{ label: 'text', count: 1 }],
    }),
    '/api/v1/documents/local/geometry?kind=layout&page=1': () => jsonResponse({
      coord_system: 'pdf_native', labels: [{ label: 'text', count: 1 }],
      paragraphs: [{ id: 'p1', layout_label: 'text', layout_box: [1, 2, 10, 20] }],
    }),
  });

  // ---- 译文框（target）：叠加层档位开着，但没有可拖拽的编辑层 ---------------------- #
  act(() => uiStore.getState().setBboxMode('target'));
  const targetView = renderWithQuery(<PreviewArea did="local" />);
  await screen.findByLabelText('text');
  act(() => uiStore.getState().setSelectedParagraph('p1'));
  expect(screen.queryByTestId('editing-overlay')).toBeNull();
  targetView.unmount();

  // ---- 排版框（layout）：同一段可拖拽（行为与改动前一致） ----------------------- #
  act(() => uiStore.getState().setBboxMode('layout'));
  renderWithQuery(<PreviewArea did="local" />);
  await screen.findByLabelText('text');
  act(() => uiStore.getState().setSelectedParagraph('p1'));
  expect(screen.getByTestId('editing-overlay')).toBeInTheDocument();
});
