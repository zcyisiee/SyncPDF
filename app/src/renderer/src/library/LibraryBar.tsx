/**
 * 左栏：论文库标题行（添加论文）+ 论文卡片列表。单击打开，右键原生菜单；PDF 可拖入整个窗口。
 */
import type { LibraryDoc } from '@shared/library';
import { useLibrary } from '@/store/library';
import { IconButton, Panel, PanelHeader } from '@/layout/Panel';
import { pickFiles } from './actions';
import { StatusBadge } from './StatusBadge';

export function LibraryBar(): JSX.Element {
  const docs = useLibrary((s) => s.docs);
  const openId = useLibrary((s) => s.openId);
  const list = Object.values(docs).sort((a, b) => b.addedAt - a.addedAt);
  return (
    <Panel className="sp-library">
      <PanelHeader
        views={[{ id: 'library', title: `论文库 · ${list.length}`, icon: 'library' }]}
        active="library"
        actions={<IconButton icon="add" title="添加论文" onClick={() => void pickFiles()} />}
      />
      <ul className="sp-cards" aria-label="论文列表">
        {list.map((doc) => (
          <DocCard key={doc.id} doc={doc} active={doc.id === openId} />
        ))}
        {list.length === 0 && <li className="sp-hint">还没有论文。点击 + 选择 PDF，或把 PDF 拖进窗口。</li>}
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
