/**
 * 工作台骨架（M2-05）：标题栏 / 活动栏 / 侧栏 / 编辑器区 / 面板 / 状态栏。
 * allotment 分栏（VSCode 式嵌套 + 持久化），尺寸存 localStorage（uiStore.layout）。
 *
 * 尺寸策略：
 * - 侧栏：allotment `preferredSize`（px）直接绑定 uiStore.layout.sidebarWidth；
 * - 编辑器三栏：defaultSizes 百分比（源 / 译文 / 段落编辑），onDragEnd 写回；
 * - 面板高度：preferredSize px 绑定 uiStore.layout.panelHeight。
 * visible=false 的 Pane 由 allotment 折叠（snap），开关在 uiStore。
 */
import { useCallback } from 'react';
import { Allotment } from 'allotment';
import 'allotment/dist/style.css';

const Pane = Allotment.Pane;
import { TitleBar } from './TitleBar';
import { Toolbar } from './Toolbar';
import { ActivityBar } from './ActivityBar';
import { SideBar } from './SideBar';
import { EditorArea } from './EditorArea';
import { Panel } from './Panel';
import { StatusBar } from './StatusBar';
import { useUiStore } from '../store/uiStore';

export function Workbench(): JSX.Element {
  const layout = useUiStore((state) => state.layout);
  const setLayout = useUiStore((state) => state.setLayout);

  /** 编辑器三栏百分比（source / target / paragraph）。 */
  const editorSizes = [layout.editorSourceWidth, layout.editorTargetWidth];

  const onEditorSizesChange = useCallback(
    (sizes: number[]) => {
      if (sizes.length === 3) {
        setLayout({ editorSourceWidth: sizes[0], editorTargetWidth: sizes[1] });
      }
    },
    [setLayout],
  );

  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        height: '100vh',
        background: 'var(--vscode-editor-background)',
        color: 'var(--vscode-editor-foreground)',
      }}
    >
      <TitleBar />
      <Toolbar />
      <div style={{ display: 'flex', flex: 1, minHeight: 0 }}>
        <ActivityBar />
        <div style={{ flex: 1, minWidth: 0, display: 'flex', flexDirection: 'column' }}>
          <Allotment proportionalLayout={false}>
            <Pane visible={layout.sidebarVisible} preferredSize={layout.sidebarWidth} minSize={180}>
              <SideBar />
            </Pane>
            <Pane minSize={200}>
              <Allotment vertical proportionalLayout={false}>
                <Pane minSize={200}>
                  <EditorArea sizes={editorSizes} onSizesChange={onEditorSizesChange} />
                </Pane>
                <Pane
                  visible={layout.panelVisible}
                  preferredSize={layout.panelHeight}
                  minSize={120}
                  maxSize={600}
                >
                  <Panel />
                </Pane>
              </Allotment>
            </Pane>
          </Allotment>
        </div>
      </div>
      <StatusBar />
    </div>
  );
}
