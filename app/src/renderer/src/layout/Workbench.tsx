/**
 * 工作台骨架：标题栏 / [左栏 | 编辑区 + 底部面板 | 右栏] / 状态栏。
 * 分栏用 allotment，拖动结束时把尺寸存进 workbench store（持久化）。
 * 快捷键：⌘B 左栏、⌘J 底部面板、⌥⌘B 右栏。整个窗口可拖入 PDF。
 */
import { useEffect, useState, type DragEvent } from 'react';
import { Allotment } from 'allotment';
import { useWorkbench } from '@/store/workbench';
import { addDroppedFiles } from '@/library/actions';
import { LibraryBar } from '@/library/LibraryBar';
import { Inspector } from '@/inspector/Inspector';
import { BottomPanel } from '@/panel/BottomPanel';
import { EditorArea } from './EditorArea';
import { StatusBar } from './StatusBar';
import { TitleBar } from './TitleBar';

const hasFiles = (event: DragEvent): boolean => event.dataTransfer.types.includes('Files');

export function Workbench(): JSX.Element {
  const leftVisible = useWorkbench((s) => s.leftVisible);
  const rightVisible = useWorkbench((s) => s.rightVisible);
  const panelVisible = useWorkbench((s) => s.panelVisible);
  // 只在首次挂载时读取持久化尺寸（allotment 的 defaultSizes 不响应后续变化）
  const [columnSizes] = useState(() => useWorkbench.getState().columnSizes ?? undefined);
  const [rowSizes] = useState(() => useWorkbench.getState().rowSizes ?? undefined);
  const [dragging, setDragging] = useState(0);

  useEffect(() => {
    const onKey = (event: KeyboardEvent): void => {
      if (!event.metaKey || event.shiftKey || event.ctrlKey) return;
      const state = useWorkbench.getState();
      if (event.code === 'KeyB' && event.altKey) state.toggleRight();
      else if (event.code === 'KeyB') state.toggleLeft();
      else if (event.code === 'KeyJ' && !event.altKey) state.togglePanel();
      else return;
      event.preventDefault();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, []);

  return (
    <div
      className="sp-workbench"
      onDragEnter={(event) => hasFiles(event) && setDragging((n) => n + 1)}
      onDragLeave={(event) => hasFiles(event) && setDragging((n) => Math.max(0, n - 1))}
      onDragOver={(event) => {
        if (!hasFiles(event)) return;
        event.preventDefault();
        event.dataTransfer.dropEffect = 'copy';
      }}
      onDrop={(event) => {
        if (!hasFiles(event)) return;
        event.preventDefault();
        setDragging(0);
        void addDroppedFiles(event.dataTransfer.files);
      }}
    >
      <TitleBar />
      <main className="sp-main">
        <Allotment
          defaultSizes={columnSizes}
          onDragEnd={(sizes) => useWorkbench.getState().setColumnSizes(sizes)}
        >
          <Allotment.Pane minSize={220} preferredSize={280} visible={leftVisible}>
            <LibraryBar />
          </Allotment.Pane>
          <Allotment.Pane minSize={360}>
            <Allotment
              vertical
              defaultSizes={rowSizes}
              onDragEnd={(sizes) => useWorkbench.getState().setRowSizes(sizes)}
            >
              <Allotment.Pane minSize={200}>
                <EditorArea />
              </Allotment.Pane>
              <Allotment.Pane minSize={120} preferredSize={220} visible={panelVisible}>
                <BottomPanel />
              </Allotment.Pane>
            </Allotment>
          </Allotment.Pane>
          <Allotment.Pane minSize={260} preferredSize={340} visible={rightVisible}>
            <Inspector />
          </Allotment.Pane>
        </Allotment>
      </main>
      <StatusBar />
      {dragging > 0 && (
        <div className="sp-drop-overlay">
          <i className="codicon codicon-file-pdf" />
          松开以加入论文库
        </div>
      )}
    </div>
  );
}
