/**
 * 编辑器区（§12.1）：三栏——源 PDF / 译文 PDF / 段落编辑（M2-06）。
 * 三栏共享 documentStore 的选中段落；PDF 渲染在 `src/renderer/src/pdf/`。
 */
import { Allotment } from 'allotment';
import { useDocumentStore } from '../store/documentStoreStoreHooks';
import { ParagraphEditor } from '../views/ParagraphEditor';
import { SourcePdfView } from '../views/SourcePdfView';
import { TargetPdfView } from '../views/TargetPdfView';

const Pane = Allotment.Pane;

export interface EditorAreaProps {
  /** 前两栏百分比（源 / 译文）；段落编辑栏取剩余。 */
  sizes: number[];
  onSizesChange: (sizes: number[]) => void;
}

export function EditorArea({ sizes, onSizesChange }: EditorAreaProps): JSX.Element {
  const sourcePath = useDocumentStore((state) => state.sourcePath);
  const targetPath = useDocumentStore((state) => state.targetPath);
  const docId = useDocumentStore((state) => state.docId);

  return (
    <Allotment
      proportionalLayout={false}
      defaultSizes={[...sizes, Math.max(0, 100 - sizes[0] - sizes[1])]}
      onDragEnd={onSizesChange}
    >
      <Pane minSize={160}>
        <EditorPane title="源 PDF" empty={sourcePath === null}>
          <SourcePdfView />
        </EditorPane>
      </Pane>
      <Pane minSize={160}>
        <EditorPane title="译文 PDF" empty={targetPath === null}>
          <TargetPdfView />
        </EditorPane>
      </Pane>
      <Pane minSize={200}>
        <EditorPane title="段落编辑" empty={docId === null}>
          <ParagraphEditor />
        </EditorPane>
      </Pane>
    </Allotment>
  );
}

interface EditorPaneProps {
  title: string;
  empty: boolean;
  children: JSX.Element;
}

function EditorPane({ title, empty, children }: EditorPaneProps): JSX.Element {
  return (
    <section
      style={{
        display: 'flex',
        flexDirection: 'column',
        height: '100%',
        minWidth: 0,
        background: 'var(--vscode-editor-background)',
      }}
    >
      <div
        style={{
          flex: '0 0 28px',
          display: 'flex',
          alignItems: 'center',
          paddingLeft: 12,
          fontSize: 11,
          textTransform: 'uppercase',
          letterSpacing: 0.5,
          borderBottom: '1px solid var(--vscode-editorGroup-border)',
          opacity: 0.85,
        }}
      >
        {title}
      </div>
      <div style={{ flex: 1, minHeight: 0, position: 'relative' }}>
        {empty ? (
          <div
            style={{
              position: 'absolute',
              inset: 0,
              display: 'grid',
              placeItems: 'center',
              opacity: 0.4,
              gap: 8,
              fontSize: 12,
            }}
          >
            <span className="codicon codicon-file-pdf" style={{ fontSize: 40 }} />
          </div>
        ) : (
          children
        )}
      </div>
    </section>
  );
}
