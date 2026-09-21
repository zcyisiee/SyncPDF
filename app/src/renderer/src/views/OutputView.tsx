/**
 * 输出面板（占位）：引擎 stderr 日志与错误事件。
 * 本任务先显示 store 内 lastError / 事件计数（引擎日志通道 M2-08 接通）。
 */
import { useDocumentStore } from '../store/documentStoreStoreHooks';

export function OutputView(): JSX.Element {
  const lastError = useDocumentStore((state) => state.lastError);
  const lastSeq = useDocumentStore((state) => state.lastSeq);
  const runState = useDocumentStore((state) => state.runState);

  return (
    <div className="syncpdf-scroll" style={{ height: '100%', padding: 12, fontSize: 12 }}>
      <div style={{ opacity: 0.75 }}>
        run_state={runState} consumed_events={lastSeq}
      </div>
      {lastError !== null && (
        <div
          style={{
            marginTop: 8,
            color: lastError.fatal ? 'var(--vscode-errorForeground)' : 'var(--vscode-editorWarning-foreground)',
          }}
        >
          [error{lastError.fatal ? ' fatal' : ''}] {lastError.code}: {lastError.message}
        </div>
      )}
    </div>
  );
}
