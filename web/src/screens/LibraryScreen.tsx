import { useDocuments } from '../lib/queries';
import { Button } from '../components/ui/Button';
import { ErrorCard } from '../components/ui/ErrorCard';
import { ScrollArea } from '../components/ui/ScrollArea';
import { Tooltip } from '../components/ui/Tooltip';
import { Icon } from '../components/icons';
import { ScreenFrame } from '../components/shell/ScreenFrame';
import { DocumentCard } from './DocumentCard';

/**
 * `#/library` 文件库：真数据来自 `GET /api/v1/documents`。
 * 上传按钮与拖放区先渲染但 disabled（tooltip 说明 W08 接入）；serve 未启动时显示连接错误卡，
 * 不允许白屏。
 */
export function LibraryScreen() {
  const documentsQuery = useDocuments();
  const documents = documentsQuery.data ?? [];

  return (
    <ScreenFrame>
      <ScrollArea className="h-full" data-od-id="screen-library">
        <div className="w-full max-w-[1220px] px-s7 pb-12 pt-s7">
          <div className="mb-s6 flex items-end justify-between gap-s5">
            <div className="min-w-0">
              <h1
                data-od-id="library-title"
                className="font-serif text-h1 font-medium leading-[1.3] text-ink"
              >
                文件库
              </h1>
              <p className="mt-s1 text-body text-ink-4">
                {documentsQuery.isPending
                  ? '正在加载文档…'
                  : `${documents.length} 篇文档 · 数据来自 bdt serve（--root 下的 workdir）`}
              </p>
            </div>
            <Tooltip content="W08 接入">
              <Button variant="primary" disabled data-od-id="cta-upload">
                <Icon name="upload" className="h-[13px] w-[13px]" />
                上传 PDF
              </Button>
            </Tooltip>
          </div>

          <Tooltip wide content="W08 接入：上传与拖放会创建一个新的 workdir（did）" className="block">
            <div
              data-od-id="dropzone"
              aria-disabled="true"
              className="mb-s7 flex cursor-not-allowed items-center gap-s4 rounded-card border border-dashed border-hair-2 bg-ivory px-s5 py-s5 opacity-45"
            >
              <span className="grid h-10 w-10 flex-none place-items-center rounded-card bg-sand text-ink-3">
                <Icon name="drop" />
              </span>
              <span className="min-w-0">
                <span className="block font-serif text-md font-medium leading-[1.4] text-ink">
                  拖入 PDF 或点击选择
                </span>
                <span className="mt-[2px] block text-sm text-ink-4">
                  上传能力在 W08 接入；当前文件库只读展示 bdt serve 的 --root 目录
                </span>
              </span>
            </div>
          </Tooltip>

          {documentsQuery.isError ? (
            <ErrorCard className="mb-s6" error={documentsQuery.error} data-od-id="error-card">
              <Button onClick={() => void documentsQuery.refetch()} disabled={documentsQuery.isFetching}>
                重试
              </Button>
            </ErrorCard>
          ) : null}

          {documentsQuery.isPending || documentsQuery.isError ? null : documents.length === 0 ? (
            <div
              data-od-id="library-empty"
              className="rounded-card border border-dashed border-hair-2 bg-ivory px-s7 py-s7 text-center"
            >
              <p className="font-serif text-md font-medium text-ink-2">还没有文档</p>
              <p className="mt-s2 font-mono text-tiny text-ink-4">
                把 bdt 的 workdir 放进 serve 的 --root 目录后刷新即可
              </p>
            </div>
          ) : (
            <div className="grid grid-cols-[repeat(auto-fill,minmax(360px,1fr))] gap-s4">
              {documents.map((doc) => (
                <DocumentCard key={doc.did} doc={doc} />
              ))}
            </div>
          )}
        </div>
      </ScrollArea>
    </ScreenFrame>
  );
}
