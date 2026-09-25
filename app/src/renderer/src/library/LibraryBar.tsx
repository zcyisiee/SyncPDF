/**
 * 左栏：「开始」区（添加 PDF）+ 论文卡片列表。单击打开，右键原生菜单。
 */
import type { LibraryDoc } from '@shared/library';
import { useLibrary } from '@/store/library';
import { Panel } from '@/layout/Panel';
import { pickFiles } from './actions';
import { StatusBadge } from './StatusBadge';

export function LibraryBar(): JSX.Element {
  const docs = useLibrary((s) => s.docs);
  const openId = useLibrary((s) => s.openId);
  const list = Object.values(docs).sort((a, b) => b.addedAt - a.addedAt);
  return (
    <Panel className="sp-library">
      <div className="sp-section-title">开始</div>
      <button type="button" className="sp-start" onClick={() => void pickFiles()}>
        <i className="codicon codicon-add" />
        <span>
          <strong>添加论文</strong>
          <small>选择或拖入 PDF</small>
        </span>
      </button>
      <div className="sp-section-title">
        论文 <span className="sp-count">{list.length}</span>
      </div>
      <ul className="sp-cards" aria-label="论文列表">
        {list.map((doc) => (
          <DocCard key={doc.id} doc={doc} active={doc.id === openId} />
        ))}
        {list.length === 0 && <li className="sp-empty">还没有论文</li>}
      </ul>
    </Panel>
  );
}

function DocCard({ doc, active }: { doc: LibraryDoc; active: boolean }): JSX.Element {
  return (
    <li>
      <button
        type="button"
        className={`sp-card ${active ? 'is-active' : ''}`}
        onClick={() => useLibrary.getState().openDoc(doc.id)}
        onContextMenu={(event) => {
          event.preventDefault();
          void window.syncpdf.library.contextMenu(doc.id);
        }}
      >
        <span className="sp-card-title">{doc.title}</span>
        {doc.authors !== null && <span className="sp-card-authors">{doc.authors}</span>}
        <span className="sp-card-meta">
          <StatusBadge doc={doc} />
          {doc.pages !== null && <span>{doc.pages} 页</span>}
        </span>
        {doc.status === 'running' && (
          <span className="sp-progress" style={{ width: `${Math.round(doc.progress * 100)}%` }} />
        )}
      </button>
    </li>
  );
}
