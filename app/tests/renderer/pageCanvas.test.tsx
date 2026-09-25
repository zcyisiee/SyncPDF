/**
 * @vitest-environment jsdom
 *
 * PageCanvas：渲染任务被取消后必须重新渲染，不能停在空白页。
 */
import { act, render, waitFor } from '@testing-library/react';
import { StrictMode } from 'react';
import { afterEach, beforeAll, describe, expect, it, vi } from 'vitest';
import { PageCanvas } from '@/pdf/PageCanvas';
import type { PDFDocumentProxy, PDFPageProxy } from '@/pdf/pdfjs';

beforeAll(() => {
  // jsdom 没有 2d 上下文；渲染由假 page 完成，只要非 null
  vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({} as never);
});
afterEach(() => vi.clearAllMocks());

/** 假页面：render 返回可取消的任务；取消时以 RenderingCancelledException 拒绝，`finish()` 完成全部在途任务。 */
function fakeDoc(): { doc: PDFDocumentProxy; renders: () => number; finish: () => void } {
  let count = 0;
  const pending = new Set<() => void>();
  const page = {
    getViewport: ({ scale }: { scale: number }) => ({ width: 600 * scale, height: 800 * scale, scale }),
    render: () => {
      count += 1;
      let cancel = (): void => {};
      const promise = new Promise<void>((resolve, reject) => {
        pending.add(resolve);
        cancel = () => {
          if (!pending.delete(resolve)) return;
          reject(Object.assign(new Error('cancelled'), { name: 'RenderingCancelledException' }));
        };
      });
      return { promise, cancel: () => cancel() };
    },
    cleanup: () => {},
  } as unknown as PDFPageProxy;
  const doc = { getPage: () => Promise.resolve(page) } as unknown as PDFDocumentProxy;
  const finish = (): void => {
    for (const resolve of pending) resolve();
    pending.clear();
  };
  return { doc, renders: () => count, finish };
}

const rendered = (container: HTMLElement): string | null | undefined =>
  container.querySelector('[data-page]')?.getAttribute('data-rendered');

describe('PageCanvas', () => {
  // 回归：StrictMode / 依赖换身份时 cleanup 取消了任务，重跑却因同一 renderKey 直接返回 → 页面永远空白
  it('StrictMode 下 effect 重放后仍完成渲染', async () => {
    const { doc, finish } = fakeDoc();
    const { container } = render(
      <StrictMode>
        <PageCanvas doc={doc} pageNumber={1} zoom={1} containerWidth={800} />
      </StrictMode>,
    );
    await waitFor(() => expect(container.querySelector('canvas')?.width).toBeGreaterThan(0));
    await act(async () => finish());
    expect(rendered(container)).toBe('true');
  });

  it('尺寸不变、视口对象换新时，被取消的渲染会重做', async () => {
    const { doc, renders, finish } = fakeDoc();
    const { container, rerender } = render(<PageCanvas doc={doc} pageNumber={1} zoom="fit-width" containerWidth={800} />);
    await waitFor(() => expect(renders()).toBe(1));
    // 首次渲染还在途时宽度变化小于 renderKey 的精度：视口对象换新，尺寸（到 0.01）不变
    await act(async () => {
      rerender(<PageCanvas doc={doc} pageNumber={1} zoom="fit-width" containerWidth={800.001} />);
    });
    await act(async () => finish());
    expect(rendered(container)).toBe('true');
  });

  // 回归：任一页回写都会重载译文文件（doc 换新），其它页不能跟着整篇重画
  it('换新 doc 但本页修订号不变：不重画；本页修订号变了且新 doc 就位才重画', async () => {
    const first = fakeDoc();
    const { rerender } = render(<PageCanvas doc={first.doc} pageNumber={1} zoom={1} containerWidth={800} />);
    await waitFor(() => expect(first.renders()).toBe(1));
    await act(async () => first.finish());

    const second = fakeDoc();
    await act(async () => {
      rerender(<PageCanvas doc={second.doc} pageNumber={1} zoom={1} containerWidth={800} docRevision={1} />);
    });
    expect(second.renders()).toBe(0);

    // 本页回写：新文件还没加载（docRevision 落后）时不用旧文件抢画
    await act(async () => {
      rerender(<PageCanvas doc={second.doc} pageNumber={1} zoom={1} containerWidth={800} revision={2} docRevision={1} />);
    });
    expect(second.renders()).toBe(0);
    const third = fakeDoc();
    await act(async () => {
      rerender(<PageCanvas doc={third.doc} pageNumber={1} zoom={1} containerWidth={800} revision={2} docRevision={2} />);
    });
    await waitFor(() => expect(third.renders()).toBe(1));
  });

  it('尺寸与修订号都没变时不重复渲染', async () => {
    const { doc, renders, finish } = fakeDoc();
    const { container, rerender } = render(<PageCanvas doc={doc} pageNumber={1} zoom={1} containerWidth={800} />);
    await waitFor(() => expect(renders()).toBe(1));
    await act(async () => finish());
    expect(rendered(container)).toBe('true');
    await act(async () => {
      rerender(<PageCanvas doc={doc} pageNumber={1} zoom={1} containerWidth={900} />);
    });
    expect(renders()).toBe(1);
  });
});
