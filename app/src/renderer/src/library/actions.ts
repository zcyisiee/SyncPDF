/**
 * 论文库操作（组件共用）：加入 PDF 后若当前没打开论文，自动打开第一篇。
 */
import type { LibraryDoc } from '@shared/library';
import { useLibrary } from '@/store/library';

function openFirst(docs: LibraryDoc[]): void {
  const state = useLibrary.getState();
  // 先并入列表，避免 onChanged 推送晚到时卡片缺席
  useLibrary.setState({ docs: { ...state.docs, ...Object.fromEntries(docs.map((d) => [d.id, d])) } });
  if (state.openId === null && docs.length > 0) state.openDoc(docs[0].id);
}

export async function pickFiles(): Promise<void> {
  openFirst(await window.syncpdf.library.pick());
}

export async function addDroppedFiles(files: FileList): Promise<void> {
  const paths = [...files]
    .filter((file) => file.name.toLowerCase().endsWith('.pdf'))
    .map((file) => window.syncpdf.pathForFile(file))
    .filter((path) => path !== '');
  if (paths.length > 0) openFirst(await window.syncpdf.library.add(paths));
}
