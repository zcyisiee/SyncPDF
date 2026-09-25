/**
 * 圆角面板外框 + 可选的 tab 头。所有区域（左栏 / 编辑区 / 右栏 / 底部面板）共用。
 */
import type { ReactNode } from 'react';

export interface PanelTab<T extends string> {
  id: T;
  label: string;
  badge?: number;
}

export function Panel({ children, className }: { children: ReactNode; className?: string }): JSX.Element {
  return (
    <div className="sp-slot">
      <section className={`sp-panel ${className ?? ''}`}>{children}</section>
    </div>
  );
}

export function PanelTabs<T extends string>({
  tabs,
  active,
  onSelect,
  actions,
}: {
  tabs: PanelTab<T>[];
  active: T;
  onSelect: (id: T) => void;
  actions?: ReactNode;
}): JSX.Element {
  return (
    <header className="sp-panel-header" role="tablist">
      {tabs.map((tab) => (
        <button
          key={tab.id}
          type="button"
          role="tab"
          aria-selected={tab.id === active}
          className={`sp-tab ${tab.id === active ? 'is-active' : ''}`}
          onClick={() => onSelect(tab.id)}
        >
          {tab.label}
          {tab.badge !== undefined && tab.badge > 0 && <span className="sp-count">{tab.badge}</span>}
        </button>
      ))}
      <span className="sp-spacer" />
      {actions}
    </header>
  );
}

/** 只有图标的按钮（工具栏 / 标题栏）。 */
export function IconButton({
  icon,
  title,
  onClick,
  active = false,
  disabled = false,
}: {
  icon: string;
  title: string;
  onClick: () => void;
  active?: boolean;
  disabled?: boolean;
}): JSX.Element {
  return (
    <button
      type="button"
      className={`sp-icon-button ${active ? 'is-active' : ''}`}
      title={title}
      aria-label={title}
      aria-pressed={active}
      disabled={disabled}
      onClick={onClick}
    >
      <i className={`codicon codicon-${icon}`} />
    </button>
  );
}
