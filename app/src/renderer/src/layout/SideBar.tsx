/**
 * 侧栏（§12.1）：按活动栏视图切换内容——段落树 / 文档列表 / 术语 / 设置。
 * 本任务先放占位视图（M2-08 深化）。
 */
import { useUiStore } from '../store/uiStore';
import { ParagraphTreeView } from '../views/ParagraphTreeView';
import { PlaceholderView } from '../views/PlaceholderView';

export function SideBar(): JSX.Element {
  const view = useUiStore((state) => state.view);

  return (
    <aside
      style={{
        display: 'flex',
        flexDirection: 'column',
        minWidth: 0,
        height: '100%',
        background: 'var(--vscode-sideBar-background)',
        color: 'var(--vscode-sideBar-foreground)',
      }}
    >
      <div
        style={{
          padding: '8px 16px',
          fontSize: 11,
          textTransform: 'uppercase',
          letterSpacing: 0.5,
          opacity: 0.8,
        }}
      >
        {viewTitle(view)}
      </div>
      <div className="syncpdf-scroll" style={{ flex: 1, minHeight: 0 }}>
        {view === 'paragraphs' ? <ParagraphTreeView /> : <PlaceholderView view={view} />}
      </div>
    </aside>
  );
}

function viewTitle(view: string): string {
  switch (view) {
    case 'documents':
      return '文档';
    case 'paragraphs':
      return '段落';
    case 'terminology':
      return '术语';
    case 'settings':
      return '设置';
    default:
      return view;
  }
}
