/**
 * 欢迎页：大号添加区 + 最近打开的论文。
 */
import { useLibrary } from '@/store/library';
import { pickFiles } from './actions';
import { StatusBadge } from './StatusBadge';

const RECENT_LIMIT = 6;

export function Welcome(): JSX.Element {
  const docs = useLibrary((s) => s.docs);
  const recent = Object.values(docs)
    .sort((a, b) => (b.openedAt ?? b.addedAt) - (a.openedAt ?? a.addedAt))
    .slice(0, RECENT_LIMIT);
  return (
    <div className="sp-welcome">
      <h1>SyncPDF</h1>
      <p className="sp-muted">保留原版式的论文翻译</p>
      <button type="button" className="sp-dropzone" onClick={() => void pickFiles()}>
        <i className="codicon codicon-file-pdf" />
        <strong>拖入 PDF，或点击选择</strong>
        <small>加入后自动排队翻译</small>
      </button>
      {recent.length > 0 && (
        <section className="sp-recent">
          <h2>最近</h2>
          <ul>
            {recent.map((doc) => (
              <li key={doc.id}>
                <button type="button" onClick={() => useLibrary.getState().openDoc(doc.id)}>
                  <span className="sp-recent-title">{doc.title}</span>
                  <StatusBadge doc={doc} />
                </button>
              </li>
            ))}
          </ul>
        </section>
      )}
    </div>
  );
}
