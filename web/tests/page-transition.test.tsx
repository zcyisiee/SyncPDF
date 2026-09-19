import { fireEvent, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { TransitioningPdfPage } from '../src/components/preview/TransitioningPdfPage';

vi.mock('../src/components/preview/PdfCanvas', () => ({
  PdfCanvas: ({ url, onRenderComplete, onError }: { url: string; onRenderComplete: () => void; onError: () => void }) =>
    <div data-testid={url}><button onClick={onRenderComplete}>render {url}</button><button onClick={onError}>fail {url}</button></div>,
}));
afterEach(() => vi.unstubAllGlobals());

it('retains the old layer until the rendered replacement finishes revealing', () => {
  const view = render(<TransitioningPdfPage url="old" pageNumber={1} scale={1} />);
  fireEvent.click(screen.getByText('render old'));
  const old = screen.getByTestId('old');
  view.rerender(<TransitioningPdfPage url="new" pageNumber={1} scale={1} />);
  expect(screen.getByTestId('old')).toBe(old);
  expect(screen.getByTestId('new').parentElement).toHaveStyle({ opacity: '0' });
  fireEvent.click(screen.getByText('render new'));
  expect(screen.getByTestId('new').parentElement).toHaveClass('preview-page-reveal');
  const next = screen.getByTestId('new');
  fireEvent.animationEnd(next.parentElement!);
  expect(screen.queryByTestId('old')).toBeNull();
  expect(screen.getByTestId('new')).toBe(next);
});

it('keeps the current page on failure and discards superseded incoming pages', () => {
  const view = render(<TransitioningPdfPage url="old" pageNumber={1} scale={1} />);
  fireEvent.click(screen.getByText('render old'));
  view.rerender(<TransitioningPdfPage url="bad" pageNumber={1} scale={1} />);
  fireEvent.click(screen.getByText('fail bad'));
  expect(screen.getByTestId('old')).toBeInTheDocument();
  expect(screen.queryByTestId('bad')).toBeNull();
  view.rerender(<TransitioningPdfPage url="latest" pageNumber={1} scale={1} />);
  expect(screen.getByTestId('latest')).toBeInTheDocument();
  expect(screen.queryByTestId('bad')).toBeNull();
});

it('switches only after rendering with reduced motion', () => {
  vi.stubGlobal('matchMedia', () => ({ matches: true }));
  const view = render(<TransitioningPdfPage url="old" pageNumber={1} scale={1} />);
  fireEvent.click(screen.getByText('render old'));
  view.rerender(<TransitioningPdfPage url="new" pageNumber={1} scale={1} />);
  expect(screen.getByTestId('old')).toBeInTheDocument();
  fireEvent.click(screen.getByText('render new'));
  expect(screen.queryByTestId('old')).toBeNull();
  expect(screen.getByTestId('new').parentElement).not.toHaveClass('preview-page-reveal');
});
