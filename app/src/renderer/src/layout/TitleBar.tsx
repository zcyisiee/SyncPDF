/**
 * 标题栏（§12.1）：自绘（titleBarStyle: hiddenInset），显示文档名与运行状态。
 * macOS 交通灯在左侧预留 80px。
 */
import { useDocumentStore } from '../store/documentStoreStoreHooks';

export function TitleBar(): JSX.Element {
  const docId = useDocumentStore((state) => state.docId);
  const runState = useDocumentStore((state) => state.runState);
  const stage = useDocumentStore((state) => state.stage);

  const runLabel =
    runState === 'running'
      ? `运行中 · ${stage ?? '…'}`
      : runState === 'finished'
        ? '完成'
        : runState === 'failed'
          ? '失败'
          : runState === 'cancelled'
            ? '已取消'
            : '';

  return (
    <header
      style={{
        height: 36,
        flex: '0 0 36px',
        display: 'flex',
        alignItems: 'center',
        gap: 12,
        paddingLeft: 84, // 交通灯区域
        paddingRight: 12,
        background: 'var(--vscode-titleBar-activeBackground)',
        color: 'var(--vscode-titleBar-activeForeground)',
        fontSize: 12,
        WebkitAppRegion: 'drag', // 允许拖拽窗口
      } as React.CSSProperties}
    >
      <span style={{ fontWeight: 600 }}>SyncPDF</span>
      {docId !== null && <span style={{ opacity: 0.75 }}>{docId}</span>}
      {runLabel !== '' && (
        <span
          style={{
            marginLeft: 'auto',
            color: runState === 'running' ? 'var(--syncpdf-run-active)' : 'inherit',
            display: 'flex',
            alignItems: 'center',
            gap: 6,
          }}
        >
          <span
            className="codicon codicon-sync"
            style={{
              display: runState === 'running' ? 'inline-block' : 'none',
              animation: 'syncpdf-spin 1.2s linear infinite',
            }}
          />
          {runLabel}
        </span>
      )}
      <style>{`@keyframes syncpdf-spin { to { transform: rotate(360deg); } }`}</style>
    </header>
  );
}
