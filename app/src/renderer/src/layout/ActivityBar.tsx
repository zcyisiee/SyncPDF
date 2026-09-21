/**
 * 活动栏（§12.1）：codicons + --vscode-activityBar-* token。
 * 视图：文档 / 段落 / 术语 / 设置。
 */
import { useUiStore } from '../store/uiStore';
import type { ViewId } from '../store/uiStore';

interface ActivityItem {
  id: ViewId;
  icon: string;
  title: string;
}

const ITEMS: readonly ActivityItem[] = [
  { id: 'documents', icon: 'codicon-files', title: '文档' },
  { id: 'paragraphs', icon: 'codicon-list-tree', title: '段落' },
  { id: 'terminology', icon: 'codicon-book', title: '术语' },
  { id: 'settings', icon: 'codicon-settings-gear', title: '设置' },
];

export function ActivityBar(): JSX.Element {
  const view = useUiStore((state) => state.view);
  const setView = useUiStore((state) => state.setView);

  return (
    <nav
      aria-label="活动栏"
      style={{
        width: 48,
        flex: '0 0 48px',
        display: 'flex',
        flexDirection: 'column',
        alignItems: 'center',
        paddingTop: 4,
        gap: 2,
        background: 'var(--vscode-activityBar-background)',
      }}
    >
      {ITEMS.map((item) => {
        const active = item.id === view;
        return (
          <button
            key={item.id}
            type="button"
            title={item.title}
            aria-label={item.title}
            aria-pressed={active}
            onClick={() => setView(item.id)}
            style={{
              width: 48,
              height: 48,
              display: 'grid',
              placeItems: 'center',
              border: 'none',
              background: 'transparent',
              color: active
                ? 'var(--vscode-activityBar-foreground)'
                : 'var(--vscode-activityBar-inactiveForeground)',
              borderBottom: active
                ? '2px solid var(--vscode-activityBar-activeBorder)'
                : '2px solid transparent',
              cursor: 'pointer',
              padding: 0,
            }}
          >
            <span className={`codicon ${item.icon}`} style={{ fontSize: 22 }} />
          </button>
        );
      })}
    </nav>
  );
}
