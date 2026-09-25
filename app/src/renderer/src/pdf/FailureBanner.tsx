/**
 * 翻译失败横幅：失败前可能已回写了部分译文页，译文栏看起来仍像原文，
 * 所以失败原因不能只藏在「论文信息」里。
 */
import type { LibraryDoc } from '@shared/library';

export function FailureBanner({ doc }: { doc: LibraryDoc }): JSX.Element | null {
  if (doc.status !== 'failed') return null;
  return (
    <div className="sp-failure-banner" role="alert">
      <span className="codicon codicon-error" aria-hidden />
      <span className="sp-failure-text sp-selectable">翻译失败：{doc.error ?? '引擎未给出原因'}</span>
      <button type="button" className="sp-button" onClick={() => void window.syncpdf.engine.enqueue(doc.id)}>
        重新翻译
      </button>
    </div>
  );
}
