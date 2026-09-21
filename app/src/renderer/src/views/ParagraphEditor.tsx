/**
 * 段落编辑器（占位）：显示当前选中段落的译文 HTML（M3-04 做完整编辑器）。
 */
import { useDocumentStore } from '../store/documentStoreStoreHooks';
import { useUiStore } from '../store/uiStore';

export function ParagraphEditor(): JSX.Element {
  const selectedId = useUiStore((state) => state.selectedParagraphId);
  const paragraphs = useDocumentStore((state) => state.paragraphs);

  const record = selectedId !== null ? paragraphs[selectedId] : undefined;

  if (record === undefined) {
    return (
      <div style={{ display: 'grid', placeItems: 'center', height: '100%', opacity: 0.5, fontSize: 12 }}>
        在段落树中选择一个段落
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div
        style={{
          flex: '0 0 auto',
          padding: '8px 16px',
          fontSize: 11,
          opacity: 0.75,
          borderBottom: '1px solid var(--vscode-editorGroup-border)',
        }}
      >
        {record.id} · {record.status}
      </div>
      <textarea
        readOnly
        value={record.translatedHtml}
        spellCheck={false}
        style={{
          flex: 1,
          minHeight: 0,
          width: '100%',
          resize: 'none',
          border: 'none',
          outline: 'none',
          padding: 12,
          fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
          fontSize: 12,
          background: 'var(--vscode-editor-background)',
          color: 'var(--vscode-editor-foreground)',
          userSelect: 'text',
        }}
      />
    </div>
  );
}
