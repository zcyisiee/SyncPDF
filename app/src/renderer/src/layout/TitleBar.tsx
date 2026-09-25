/**
 * 标题栏：左侧给红绿灯留位，中间显示当前论文，右上角三块区域开关。
 */
import { useOpenDocMeta } from '@/store/library';
import { useWorkbench } from '@/store/workbench';
import { IconButton } from './Panel';

export function TitleBar(): JSX.Element {
  const doc = useOpenDocMeta();
  const { leftVisible, panelVisible, rightVisible, toggleLeft, togglePanel, toggleRight } = useWorkbench();
  return (
    <header className="sp-titlebar">
      <div className="sp-titlebar-title">{doc === null ? 'SyncPDF' : doc.title}</div>
      <div className="sp-titlebar-actions">
        <IconButton
          icon={leftVisible ? 'layout-sidebar-left' : 'layout-sidebar-left-off'}
          title="切换论文栏 (⌘B)"
          active={leftVisible}
          onClick={toggleLeft}
        />
        <IconButton
          icon={panelVisible ? 'layout-panel' : 'layout-panel-off'}
          title="切换底部面板 (⌘J)"
          active={panelVisible}
          onClick={togglePanel}
        />
        <IconButton
          icon={rightVisible ? 'layout-sidebar-right' : 'layout-sidebar-right-off'}
          title="切换检查器 (⌥⌘B)"
          active={rightVisible}
          onClick={toggleRight}
        />
      </div>
    </header>
  );
}
