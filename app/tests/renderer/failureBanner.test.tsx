/**
 * @vitest-environment jsdom
 *
 * 失败原因必须在文档区直接可见（即使已有部分译文 PDF），并可一键重跑。
 */
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { LibraryDoc } from '@shared/library';
import { FailureBanner } from '@/pdf/FailureBanner';

afterEach(cleanup);

const doc = (patch: Partial<LibraryDoc>): LibraryDoc => ({ id: 'd1', status: 'failed', error: null, ...patch }) as LibraryDoc;

describe('FailureBanner', () => {
  it('失败时显示引擎原因并可重新翻译', () => {
    const enqueue = vi.fn(() => Promise.resolve());
    Object.assign(window, { syncpdf: { engine: { enqueue } } });
    render(<FailureBanner doc={doc({ error: '公式源绘制保存失败' })} />);
    expect(screen.getByRole('alert').textContent).toContain('翻译失败：公式源绘制保存失败');
    fireEvent.click(screen.getByText('重新翻译'));
    expect(enqueue).toHaveBeenCalledWith('d1');
  });

  it('没有原因也提示失败；非失败状态不显示', () => {
    const { rerender } = render(<FailureBanner doc={doc({})} />);
    expect(screen.getByRole('alert').textContent).toContain('引擎未给出原因');
    for (const status of ['done', 'running', 'queued'] as const) {
      rerender(<FailureBanner doc={doc({ status, error: 'x' })} />);
      expect(screen.queryByRole('alert')).toBeNull();
    }
  });
});
