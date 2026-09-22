/**
 * 状态栏（§12.1）：阶段、进度条、段落 / 问题统计、译文修订号。
 */
import { useDocumentStore } from '../store/documentStoreStoreHooks';

export function StatusBar(): JSX.Element {
  const stage = useDocumentStore((state) => state.stage);
  const progress = useDocumentStore((state) => state.progress);
  const paragraphCount = useDocumentStore((state) => state.paragraphOrder.length);
  const issueCount = useDocumentStore((state) => state.issues.length);
  const engineVersion = useDocumentStore((state) => state.engineVersion);
  const revision = useDocumentStore((state) => state.revision);
  const pageCount = useDocumentStore((state) => state.pageCount);
  const readyPages = useDocumentStore((state) => Object.keys(state.pageRevisions).length);

  const ratio =
    progress !== null && progress.total > 0
      ? Math.min(1, Math.max(0, progress.done / progress.total))
      : null;

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
      {ratio !== null && (
        <span
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={progress?.total ?? 0}
          aria-valuenow={progress?.done ?? 0}
          title={progressLabel}
          style={{
            width: 120,
            height: 6,
            borderRadius: 3,
            overflow: 'hidden',
            background: 'var(--vscode-progressBar-background, rgba(255,255,255,0.2))',
            opacity: 0.6,
          }}
        >
          <span
            style={{
              display: 'block',
              width: `${(ratio * 100).toFixed(1)}%`,
              height: '100%',
              background: 'var(--vscode-progressBar-background, #0e70c0)',
              opacity: 1,
            }}
          />
        </span>
      )}
      <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
        <span className="codicon codicon-comment-discussion" />
        {paragraphCount} 段
      </span>
      <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
        <span className="codicon codicon-warning" />
        {issueCount}
      </span>
      {pageCount !== null && (
        <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
          <span className="codicon codicon-file-pdf" />
          {readyPages}/{pageCount} 页
        </span>
      )}
      <span style={{ opacity: 0.75 }}>rev {revision}</span>
      {engineVersion !== null && (
        <span style={{ marginLeft: 'auto', opacity: 0.75 }}>{engineVersion}</span>
      )}
    </footer>
  );
}
