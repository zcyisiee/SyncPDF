/**
 * 问题面板（M2-08）：`issue` 事件列表，按等级过滤，点击定位。
 *
 * 定位规则：带 `paragraph_id` 的选中该段（三栏联动滚动到它所在页）；
 * 只带 `page` 的退化为"选中该页第一个段落"，再没有就只切到段落视图。
 */
import { useMemo, useState } from 'react';
import type { IssueSeverity } from '@shared/protocol';
import { documentStore } from '../store/documentStore';
import { useDocumentStore } from '../store/documentStoreStoreHooks';
import { useUiStore } from '../store/uiStore';

const SEVERITY_ORDER: readonly IssueSeverity[] = ['error', 'warning', 'info'];

function severityIcon(severity: IssueSeverity): { icon: string; color: string } {
  switch (severity) {
    case 'error':
      return { icon: 'codicon-error', color: 'var(--vscode-errorForeground)' };
    case 'warning':
      return { icon: 'codicon-warning', color: 'var(--vscode-editorWarning-foreground)' };
    case 'info':
      return { icon: 'codicon-info', color: 'var(--vscode-editorInfo-foreground)' };
  }
}

export function IssuesView(): JSX.Element {
  const issues = useDocumentStore((state) => state.issues);
  const paragraphs = useDocumentStore((state) => state.paragraphs);
  const order = useDocumentStore((state) => state.paragraphOrder);
  const selectedId = useDocumentStore((state) => state.selectedParagraphId);
  const setView = useUiStore((state) => state.setView);
  const [filter, setFilter] = useState<IssueSeverity | 'all'>('all');

  const counts = useMemo(() => {
    const result: Record<IssueSeverity, number> = { error: 0, warning: 0, info: 0 };
    for (const issue of issues) result[issue.severity] += 1;
    return result;
  }, [issues]);

  const visible = useMemo(
    () => (filter === 'all' ? issues : issues.filter((issue) => issue.severity === filter)),
    [issues, filter],
  );

  /** 问题 → 要选中的段落 id。 */
  const locate = (paragraphId: string | undefined, page: number | undefined): void => {
    if (paragraphId !== undefined && paragraphs[paragraphId] !== undefined) {
      documentStore.getState().selectParagraph(paragraphId);
      setView('paragraphs');
      return;
    }
    if (page !== undefined) {
      const first = order.find((id) => paragraphs[id]?.page === page);
      if (first !== undefined) {
        documentStore.getState().selectParagraph(first);
        setView('paragraphs');
      }
    }
  };

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div
        style={{
          flex: '0 0 24px',
          display: 'flex',
          alignItems: 'center',
          gap: 4,
          padding: '0 8px',
          fontSize: 11,
          borderBottom: '1px solid var(--vscode-panel-border)',
        }}
      >
        <FilterChip
          label={`全部 ${issues.length}`}
          active={filter === 'all'}
          onClick={() => setFilter('all')}
        />
        {SEVERITY_ORDER.map((severity) => (
          <FilterChip
            key={severity}
            label={`${severityLabel(severity)} ${counts[severity]}`}
            active={filter === severity}
            onClick={() => setFilter(severity)}
          />
        ))}
      </div>
      <div className="syncpdf-scroll" style={{ flex: 1, minHeight: 0, overflow: 'auto' }}>
        {visible.length === 0 ? (
          <div style={{ padding: 12, opacity: 0.6, fontSize: 12 }}>无问题</div>
        ) : (
          visible.map((issue) => {
            const icon = severityIcon(issue.severity);
            const active = issue.paragraphId !== undefined && issue.paragraphId === selectedId;
            return (
              <button
                key={issue.seq}
                type="button"
                onClick={() => locate(issue.paragraphId, issue.page)}
                title="点击定位"
                style={{
                  display: 'flex',
                  alignItems: 'baseline',
                  gap: 8,
                  width: '100%',
                  textAlign: 'left',
                  border: 'none',
                  padding: '3px 12px',
                  fontSize: 12,
                  cursor: 'pointer',
                  background: active
                    ? 'var(--vscode-list-activeSelectionBackground)'
                    : 'transparent',
                  color: active ? 'var(--vscode-list-activeSelectionForeground)' : 'inherit',
                }}
              >
                <span className={`codicon ${icon.icon}`} style={{ color: icon.color }} />
                <span style={{ opacity: 0.75 }}>{issue.code}</span>
                <span style={{ flex: 1, minWidth: 0 }}>{issue.message}</span>
                {issue.paragraphId !== undefined && (
                  <span style={{ opacity: 0.55 }}>{issue.paragraphId}</span>
                )}
                {issue.page !== undefined && <span style={{ opacity: 0.55 }}>p{issue.page}</span>}
              </button>
            );
          })
        )}
      </div>
    </div>
  );
}

function severityLabel(severity: IssueSeverity): string {
  switch (severity) {
    case 'error':
      return '错误';
    case 'warning':
      return '警告';
    case 'info':
      return '提示';
  }
}

function FilterChip({
  label,
  active,
  onClick,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
}): JSX.Element {
  return (
    <button
      type="button"
      aria-pressed={active}
      onClick={onClick}
      style={{
        border: 'none',
        borderRadius: 2,
        padding: '1px 8px',
        fontSize: 11,
        cursor: 'pointer',
        background: active ? 'var(--vscode-list-activeSelectionBackground)' : 'transparent',
        color: active ? 'var(--vscode-list-activeSelectionForeground)' : 'inherit',
      }}
    >
      {label}
    </button>
  );
}
