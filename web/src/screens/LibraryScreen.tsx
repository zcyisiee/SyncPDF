import { useRef, useState } from 'react';

import type { ApiErrorDescription } from '../lib/api';
import { describeApiError } from '../lib/api';
import { useDocuments, useUploadMutation } from '../lib/queries';
import {
  MAX_UPLOAD_BYTES,
  checkUpload,
  formatBytes,
  readPdfMagic,
} from '../lib/uploads';
import { Button } from '../components/ui/Button';
import { ErrorCard } from '../components/ui/ErrorCard';
import { ScrollArea } from '../components/ui/ScrollArea';
import { Icon } from '../components/icons';
import { ScreenFrame } from '../components/shell/ScreenFrame';
import { DocumentCard } from './DocumentCard';

/** 一次上传的界面状态：上传中（行内）+ 失败（错误卡，可关掉）。 */
type UploadRow =
  | { key: string; name: string; status: 'uploading' }
  | ({ key: string; name: string; status: 'failed' } & ApiErrorDescription);

/**
 * `#/library` 文件库：真数据来自 `GET /api/v1/documents`；上传走 `POST /api/v1/documents`（W08）。
 *
 * 上传契约（`docs/reference/http-api.md`）：请求体只有 `file` 字段，did 由**服务端**生成
 * （`up-<slug>-<时间戳>`），客户端不提供路径或命令。多选时**逐个串行** POST（并发上传
 * 不是本任务范围），不伪造百分比；成功后列表刷新，**不自动跳转**（用户自己点卡片进去）。
 */
export function LibraryScreen() {
  const documentsQuery = useDocuments();
  const upload = useUploadMutation();
  const documents = documentsQuery.data ?? [];
  const [rows, setRows] = useState<UploadRow[]>([]);
  const [dragActive, setDragActive] = useState(false);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const counter = useRef(0);
  const uploading = rows.some((row) => row.status === 'uploading');

  const nextKey = () => {
    counter.current += 1;
    return `upload-${counter.current}`;
  };

  /**
   * 记一次失败：**替换**同一个 key 的"上传中"行（服务端拒绝时那一行还在），
   * 没有就追加（本地预检直接失败的情况）。
   */
  const fail = (key: string, name: string, description: ApiErrorDescription) =>
    setRows((previous) => {
      const row: UploadRow = { key, name, status: 'failed', ...description };
      return previous.some((item) => item.key === key)
        ? previous.map((item) => (item.key === key ? row : item))
        : [...previous, row];
    });

  /** 选中的文件**串行**上传：逐个 POST（不并发），失败只影响那一行。 */
  const handleFiles = async (files: File[]) => {
    for (const file of files) {
      const key = nextKey();
      const checked = checkUpload(file);
      if (!checked.ok) {
        fail(key, file.name, {
          title: checked.rejection === 'too_large' ? '文件过大' : '这个文件不是 PDF',
          message: checked.message ?? '',
        });
        continue;
      }
      // 魔数预检（只读前 5 字节）：服务端会再校验一次，这里只是不让用户白等往返。
      if (!(await readPdfMagic(file).catch(() => false))) {
        fail(key, file.name, {
          title: '这个文件不是 PDF',
          message: `前 5 字节不是 %PDF-（服务端也会拒收 422 invalid_pdf）`,
        });
        continue;
      }
      setRows((previous) => [...previous, { key, name: file.name, status: 'uploading' }]);
      try {
        await upload.mutateAsync(file);
        setRows((previous) => previous.filter((row) => row.key !== key));
      } catch (error) {
        fail(key, file.name, describeApiError(error));
      }
    }
  };

  const openPicker = () => inputRef.current?.click();

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
            <Button
              variant="primary"
              data-od-id="cta-upload"
              onClick={openPicker}
              disabled={uploading}
            >
              <Icon name="upload" className="h-[13px] w-[13px]" />
              {uploading ? '正在上传…' : '上传 PDF'}
            </Button>
          </div>

          <input
            ref={inputRef}
            type="file"
            accept="application/pdf,.pdf"
            multiple
            hidden
            data-od-id="upload-input"
            onChange={(event) => {
              const files = Array.from(event.target.files ?? []);
              event.target.value = ''; // 同一个文件能再次选（否则 change 不再触发）
              void handleFiles(files);
            }}
          />

          <div
            data-od-id="dropzone"
            data-drag={dragActive ? 'active' : 'idle'}
            role="button"
            tabIndex={0}
            aria-label="拖入 PDF 或点击选择"
            onClick={openPicker}
            onKeyDown={(event) => {
              if (event.key === 'Enter' || event.key === ' ') openPicker();
            }}
            onDragOver={(event) => {
              event.preventDefault();
              setDragActive(true);
            }}
            onDragLeave={() => setDragActive(false)}
            onDrop={(event) => {
              event.preventDefault();
              setDragActive(false);
              void handleFiles(Array.from(event.dataTransfer?.files ?? []));
            }}
            className={`mb-s5 flex cursor-pointer items-center gap-s4 rounded-card border border-dashed px-s5 py-s5 transition-colors ${
              dragActive ? 'border-accent bg-sand' : 'border-hair-2 bg-ivory hover:bg-sand'
            }`}
          >
            <span className="grid h-10 w-10 flex-none place-items-center rounded-card bg-sand text-ink-3">
              <Icon name="drop" />
            </span>
            <span className="min-w-0">
              <span className="block font-serif text-md font-medium leading-[1.4] text-ink">
                拖入 PDF 或点击选择
              </span>
              <span className="mt-[2px] block text-sm text-ink-4">
                上传会在 serve 的 --root 下建一个新文档（did 由服务端生成，上限{' '}
                {formatBytes(MAX_UPLOAD_BYTES)}）；可以多选，逐个上传
              </span>
            </span>
          </div>

          {rows.length === 0 ? null : (
            <div className="mb-s5 flex flex-col gap-s3" data-od-id="upload-queue">
              {rows.map((row) =>
                row.status === 'uploading' ? (
                  <p
                    key={row.key}
                    data-od-id="upload-row"
                    data-status="uploading"
                    className="flex items-center gap-s2 text-tiny text-ink-3"
                  >
                    <span className="h-[6px] w-[6px] flex-none rounded-full bg-run-ink pulse-dot" />
                    正在上传 <span className="font-mono text-ink-2">{row.name}</span>…
                  </p>
                ) : (
                  <ErrorCard
                    key={row.key}
                    data-od-id="upload-error"
                    title={`${row.title}：${row.name}`}
                    message={row.message}
                  >
                    <Button
                      size="sm"
                      onClick={() =>
                        setRows((previous) => previous.filter((item) => item.key !== row.key))
                      }
                    >
                      知道了
                    </Button>
                  </ErrorCard>
                ),
              )}
            </div>
          )}

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
                拖一个 PDF 进上面的区域，或把 bdt 的 workdir 放进 serve 的 --root 目录
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
