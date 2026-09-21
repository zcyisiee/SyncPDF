/**
 * 状态栏（§12.1）：阶段、进度、段落统计。
 */
import { useDocumentStore } from '../store/documentStoreStoreHooks';

export function StatusBar(): JSX.Element {
  const stage = useDocumentStore((state) => state.stage);
  const progress = useDocumentStore((state) => state.progress);
  const paragraphCount = useDocumentStore((state) => state.paragraphOrder.length);
  const issueCount = useDocumentStore((state) => state.issues.length);
  const engineVersion = useDocumentStore((state) => state.engineVersion);

  const progressLabel =
    progress !== null && progress.total > 0
      ? `${stage ?? progress.stage} ${progress.done}/${progress.total}`
      : (stage ?? '');

  return (
    <footer
      style={{
        height: 22,
        flex: '0 0 22px',
        display: 'flex',
        alignItems: 'center',
        gap: 16,
        paddingLeft: 12,
        paddingRight: 12,
        fontSize: 11,
        background: 'var(--vscode-statusBar-background)',
        color: 'var(--vscode-statusBar-foreground)',
      }}
    >
      <span>{progressLabel}</span>
      <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
        <span className="codicon codicon-comment-discussion" />
        {paragraphCount} 段
      </span>
      <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
        <span className="codicon codicon-warning" />
        {issueCount}
      </span>
      {engineVersion !== null && (
        <span style={{ marginLeft: 'auto', opacity: 0.75 }}>{engineVersion}</span>
      )}
    </footer>
  );
}
