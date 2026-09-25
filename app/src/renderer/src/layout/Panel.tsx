/**
 * 圆角面板外框 + 面板头。所有区域（左栏 / 编辑区 / 右栏 / 底部面板）共用。
 *
 * 面板头照 VSCode 侧栏：多个视图时先是一排图标切换（选中 = 圆角底色，不画下划线），
 * 再是加粗的视图标题行，右侧放该视图的图标操作。`inline` 把两行并成一行（底部面板用）。
 */
import type { ReactNode } from 'react';

export interface PanelView<T extends string> {
  id: T;
  title: string;
  icon: string;
  badge?: number;
}

export function Panel({ children, className }: { children: ReactNode; className?: string }): JSX.Element {
  return (
    <div className="sp-slot">
      <section className={`sp-panel ${className ?? ''}`}>{children}</section>
    </div>
  );
}

export function PanelHeader<T extends string>({
  views,
  active,
  onSelect,
  actions,
  inline = false,
}: {
  views: PanelView<T>[];
  active: T;
  onSelect?: (id: T) => void;
  actions?: ReactNode;
  inline?: boolean;
}): JSX.Element {
  const current = views.find((view) => view.id === active) ?? views[0];
  const switcher =
    views.length > 1 ? (
      <div className="sp-viewbar" role="tablist">
        {views.map((view) => (
          <button
            key={view.id}
            type="button"
            role="tab"
            title={view.title}
            aria-label={view.title}
            aria-selected={view.id === active}
            className={`sp-view-tab ${view.id === active ? 'is-active' : ''}`}
            onClick={() => onSelect?.(view.id)}
          >
            <i className={`codicon codicon-${view.icon}`} />
            {view.badge !== undefined && view.badge > 0 && <span className="sp-view-badge">{view.badge}</span>}
          </button>
        ))}
      </div>
    ) : null;
  const title = (
    <>
      <h2 className="sp-panel-title">{current.title}</h2>
      <span className="sp-spacer" />
      {actions !== undefined && <div className="sp-actionbar">{actions}</div>}
    </>
  );
  if (inline) {
    return (
      <header className="sp-panel-header">
        {switcher}
        {title}
      </header>
    );
  }
  return (
    <>
      {switcher !== null && <header className="sp-panel-header">{switcher}</header>}
      <header className="sp-panel-header">{title}</header>
    </>
  );
}

/**
 * 只有图标的按钮（面板头 / 工具栏 / 标题栏）。`active` 表示开关或单选的选中态。
 * `glyph`：codicon 里没有贴切图标的概念（原文 / 译文）直接用一个字作图标。
 */
export function IconButton({
  icon,
  glyph,
  title,
  onClick,
  active,
  disabled = false,
  role,
}: {
  icon: string;
  glyph?: string;
  title: string;
  onClick: () => void;
  active?: boolean;
  disabled?: boolean;
  role?: 'radio';
}): JSX.Element {
  return (
    <button
      type="button"
      className={`sp-icon-button ${active === true ? 'is-active' : ''}`}
      title={title}
      aria-label={title}
      role={role}
      aria-pressed={role === undefined ? active : undefined}
      aria-checked={role === 'radio' ? active : undefined}
      disabled={disabled}
      onClick={onClick}
    >
      {glyph === undefined ? <i className={`codicon codicon-${icon}`} /> : <span className="sp-glyph-icon">{glyph}</span>}
    </button>
  );
}
