/**
 * 译文 PDF 栏（M2-06）：渲染引擎写出的 output。
 *
 * 增量刷新：`page_ready` 让 documentStore 的 `revision` +1 并记到
 * `pageRevisions[page]`；整册 revision 变化触发重新 `getDocument`（文件被重写了），
 * 每页的 `pageRevisions[page]` 变化只让那一页重画（其余页复用已渲染的画布）。
 * `preview_path` 为 null / 缺省时就读 output 本身（fake-sidecar 即此形态）。
 */
import { PdfPane } from '../pdf/PdfPane';
import { useParagraphBoxes } from '../pdf/useParagraphBoxes';
import { useDocumentStore } from '../store/documentStoreStoreHooks';
import { useUiStore } from '../store/uiStore';

export function TargetPdfView(): JSX.Element {
  const targetPath = useDocumentStore((state) => state.targetPath);
  const revision = useDocumentStore((state) => state.revision);
  const pageRevisions = useDocumentStore((state) => state.pageRevisions);
  const selectedId = useDocumentStore((state) => state.selectedParagraphId);
  const syncScroll = useUiStore((state) => state.scrollSyncEnabled);
  const boxesByPage = useParagraphBoxes();

  return (
    <PdfPane
      path={targetPath}
      revision={revision}
      pageRevisions={pageRevisions}
      boxesByPage={boxesByPage}
      selectedId={selectedId}
      readOnlyBoxes
      origin="target"
      syncScroll={syncScroll}
      emptyHint="运行任务后在此显示译文 PDF"
    />
  );
}
