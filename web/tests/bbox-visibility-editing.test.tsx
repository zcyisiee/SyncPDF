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
  renderWithQuery(<PreviewArea did="local" view="translate" />);
  await screen.findByLabelText('标题 · title（1）');
  act(() => uiStore.getState().setSelectedParagraph('p1'));
  expect(screen.getByTestId('editing-overlay')).toBeInTheDocument();
  fireEvent.click(screen.getByLabelText('标题 · title（1）'));
  expect(screen.queryByTestId('editing-overlay')).toBeNull();
  act(() => { uiStore.getState().setSelectedParagraph(null); uiStore.getState().setSelectedParagraph('p1'); });
  expect(screen.queryByTestId('editing-overlay')).toBeNull();
  fireEvent.click(screen.getByText('全选类别'));
  expect(screen.getByTestId('editing-overlay')).toBeInTheDocument();
  act(() => uiStore.getState().setBboxMode('off'));
  await waitFor(() => expect(screen.queryByTestId('editing-overlay')).toBeNull());
  expect(fetch.mock.calls.every(([, init]) => !init?.method || init.method === 'GET')).toBe(true);
});
