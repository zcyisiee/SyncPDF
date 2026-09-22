/**
 * 源 PDF 栏（M2-06）：渲染用户打开的原始 PDF，叠加段落框（可 hover / 点击选中）。
 * 源文件运行过程中不会变，revision 固定 0（一次加载长期缓存）。
 */
import { PdfPane } from '../pdf/PdfPane';
import { useParagraphBoxes } from '../pdf/useParagraphBoxes';
import { documentStore } from '../store/documentStore';
import { useDocumentStore } from '../store/documentStoreStoreHooks';
import { useUiStore } from '../store/uiStore';

export function SourcePdfView(): JSX.Element {
  const sourcePath = useDocumentStore((state) => state.sourcePath);
  const selectedId = useDocumentStore((state) => state.selectedParagraphId);
  const syncScroll = useUiStore((state) => state.scrollSyncEnabled);
  const boxesByPage = useParagraphBoxes();

  return (
    <PdfPane
      path={sourcePath}
      revision={0}
      boxesByPage={boxesByPage}
      selectedId={selectedId}
      onSelect={(id) => documentStore.getState().selectParagraph(id)}
      origin="source"
      syncScroll={syncScroll}
      emptyHint="打开一个 PDF 开始（工具栏 → 打开 PDF）"
    />
  );
}
