/**
 * 编辑区：未打开论文时显示欢迎页，否则显示 PDF 视图。
 */
import { useLibrary } from '@/store/library';
import { Welcome } from '@/library/Welcome';
import { DocumentView } from '@/pdf/DocumentView';
import { Panel } from './Panel';

export function EditorArea(): JSX.Element {
  const openId = useLibrary((s) => s.openId);
  return (
    <Panel className="sp-editor">{openId === null ? <Welcome /> : <DocumentView key={openId} />}</Panel>
  );
}
