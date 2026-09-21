/**
 * 面板（§12.1）：底部 allotment 分栏——问题（issue 列表）/ 输出（引擎日志）。
 */
import { useUiStore } from '../store/uiStore';
import type { PanelTab } from '../store/uiStore';
import { IssuesView } from '../views/IssuesView';
import { OutputView } from '../views/OutputView';

const TABS: ReadonlyArray<{ id: PanelTab; label: string }> = [
  { id: 'issues', label: '问题' },
  { id: 'output', label: '输出' },
];

export function Panel(): JSX.Element {
  const panelTab = useUiStore((state) => state.panelTab);
  const setPanelTab = useUiStore((state) => state.setPanelTab);
  const setLayout = useUiStore((state) => state.setLayout);

  return (
    <section
      style={{
        display: 'flex',
        flexDirection: 'column',
        height: '100%',
        minWidth: 0,
        background: 'var(--vscode-panel-background)',
        borderTop: '1px solid var(--vscode-panel-border)',
      }}
    >
      <div
        role="tablist"
        style={{
          flex: '0 0 28px',
          display: 'flex',
          alignItems: 'center',
          gap: 4,
          paddingLeft: 8,
          fontSize: 11,
          textTransform: 'uppercase',
          letterSpacing: 0.5,
        }}
      >
        {TABS.map((tab) => {
          const active = tab.id === panelTab;
          return (
            <button
              key={tab.id}
              type="button"
              role="tab"
              aria-selected={active}
              onClick={() => setPanelTab(tab.id)}
              style={{
                border: 'none',
                background: active ? 'var(--vscode-list-activeSelectionBackground)' : 'transparent',
                color: active
                  ? 'var(--vscode-list-activeSelectionForeground)'
                  : 'inherit',
                padding: '3px 10px',
                cursor: 'pointer',
              }}
            >
              {tab.label}
            </button>
          );
        })}
        <button
          type="button"
          title="关闭面板"
          aria-label="关闭面板"
          onClick={() => setLayout({ panelVisible: false })}
          style={{
            marginLeft: 'auto',
            border: 'none',
            background: 'transparent',
            color: 'inherit',
            cursor: 'pointer',
            padding: '2px 8px',
          }}
        >
          <span className="codicon codicon-chrome-close" />
        </button>
      </div>
      <div style={{ flex: 1, minHeight: 0 }}>
        {panelTab === 'issues' ? <IssuesView /> : <OutputView />}
      </div>
    </section>
  );
}
