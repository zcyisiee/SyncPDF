/**
 * 状态栏：左侧引擎 / 队列状态，右侧当前页与缩放。
 */
import { useLibrary } from '@/store/library';
import { useWorkbench } from '@/store/workbench';

export function StatusBar(): JSX.Element {
  const docs = useLibrary((s) => s.docs);
  const openId = useLibrary((s) => s.openId);
  const currentPage = useLibrary((s) => s.currentPage);
  const pageCount = useLibrary((s) => s.pageCount);
  const zoom = useWorkbench((s) => s.zoom.source);
  const all = Object.values(docs);
  const running = all.find((doc) => doc.status === 'running');
  const queued = all.filter((doc) => doc.status === 'queued').length;

  return (
    <footer className="sp-statusbar">
      <span className="sp-status-item">
        {running === undefined ? (
          <>
            <i className="codicon codicon-check" /> 引擎空闲
          </>
        ) : (
          <>
            <i className="codicon codicon-sync codicon-modifier-spin" /> 翻译中 {Math.round(running.progress * 100)}% ·{' '}
            {running.title}
          </>
        )}
      </span>
      {queued > 0 && <span className="sp-status-item">排队 {queued}</span>}
      <span className="sp-spacer" />
      {openId !== null && pageCount > 0 && (
        <span className="sp-status-item">
          第 {currentPage} / {pageCount} 页
        </span>
      )}
      {openId !== null && (
        <span className="sp-status-item">{zoom === 'fit-width' ? '适合宽度' : `${Math.round(zoom * 100)}%`}</span>
      )}
    </footer>
  );
}
