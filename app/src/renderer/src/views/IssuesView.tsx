/**
 * 问题面板（占位）：显示 documentStore.issues。
 */
import { useDocumentStore } from '../store/documentStoreStoreHooks';

export function IssuesView(): JSX.Element {
  const issues = useDocumentStore((state) => state.issues);

  if (issues.length === 0) {
    return <div style={{ padding: 12, opacity: 0.6, fontSize: 12 }}>无问题</div>;
  }

  return (
    <div className="syncpdf-scroll" style={{ height: '100%' }}>
      {issues.map((issue) => (
        <div
          key={issue.seq}
          style={{
            display: 'flex',
            alignItems: 'baseline',
            gap: 8,
            padding: '3px 12px',
            fontSize: 12,
          }}
        >
          <span
            className={`codicon ${
              issue.severity === 'error'
                ? 'codicon-error'
                : issue.severity === 'warning'
                  ? 'codicon-warning'
                  : 'codicon-info'
            }`}
            style={{
              color:
                issue.severity === 'error'
                  ? 'var(--vscode-errorForeground)'
                  : issue.severity === 'warning'
                    ? 'var(--vscode-editorWarning-foreground)'
                    : 'var(--vscode-editorInfo-foreground)',
            }}
          />
          <span style={{ opacity: 0.75 }}>{issue.code}</span>
          <span>{issue.message}</span>
          {issue.paragraphId !== undefined && (
            <span style={{ opacity: 0.55 }}>{issue.paragraphId}</span>
          )}
          {issue.page !== undefined && <span style={{ opacity: 0.55 }}>p{issue.page}</span>}
        </div>
      ))}
    </div>
  );
}
