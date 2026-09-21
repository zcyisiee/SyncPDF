/**
 * 左栏：论文导航（设计稿 §2.1）。一个卡片 = 一篇论文的全部工作（所有路由常驻）。
 *
 * 上传也长在这里：文件列表、上传队列与那个隐藏 `<input type=file>` 全站只有一份。
 * 其他入口（路由为空态时中栏的「上传 PDF」）通过 `useUiStore().requestUpload` 让本组件
 * 去点自己的 input，避免出现第二个文件选择器与第二套队列状态（上传串行、魔数预检、
 * 失败行内错误卡的口径只有一份）。
 *
 * 工作台路由（`activeDid !== null`）时，列底还挂**文档操作区**（`data-od-id="action-bar"`，
 * 本文件的 `DocumentActionBar`：任务控制 + 编译全文），它在所有视图下常驻 ——
 * 任务控制的唯一入口。它自己 `flex-none`，所以论文列表再长也挤不掉它。
 *
 * 右键删除：菜单状态放在 ui store（`libraryMenu`）而不是卡片自己的 state ——
 * **同一时刻只允许一个菜单**（卡片各自持 state 时右键第二张卡会同时开出两个菜单）。
 */
import { useEffect, useRef, useState } from 'react';

import type { DocumentListItem } from '../../api/types';
import type { Route } from '../../lib/routing';
import type { ApiErrorDescription } from '../../lib/api';
import { ApiError, describeApiError } from '../../lib/api';
import { cn } from '../../lib/cn';
import { countLabel, documentStatus, humanizeUpdatedAt, type StatusTone } from '../../lib/humanize';
import { useDeleteDocumentMutation, useDocuments, useUploadMutation } from '../../lib/queries';
import {
  MAX_UPLOAD_BYTES,
  checkUpload,
  formatBytes,
  readPdfMagic,
} from '../../lib/uploads';
import { useUiStore } from '../../stores/ui';
import { DocumentActionBar } from './DocumentActionBar';
import { Icon } from '../icons';
import { Button } from '../ui/Button';
import { ErrorCard } from '../ui/ErrorCard';
import { ScrollArea } from '../ui/ScrollArea';

/** 一次上传的界面状态：上传中（行内）+ 失败（错误卡，可关掉）。 */
type UploadRow =
  | { key: string; name: string; status: 'uploading' }
  | ({ key: string; name: string; status: 'failed' } & ApiErrorDescription);

/** 卡片状态 chip 的色调样式（statusTone + 「未完成」用的 warn）。 */
const CHIP_CLASSES: Record<StatusTone | 'warn', string> = {
  run: 'border-run-soft bg-run-soft text-run-ink',
  pass: 'border-pass-soft bg-pass-soft text-pass-ink',
  err: 'border-err-soft bg-err-soft text-err-ink',
  warn: 'border-warn-soft bg-warn-soft text-warn-ink',
  idle: 'border-hair text-ink-4',
};

/** 卡片状态 chip：整体状态（最"响"的阶段）→ 文案 + 色调。 */
function statusChip(doc: DocumentListItem): { text: string; tone: StatusTone | 'warn' } {
  const status = documentStatus(doc.stage_summary);
  // 全阶段 not_run → 未开始；跑了一部分不算未开始，用 warn 的「未完成」区分。
  if (status.tone === 'idle') {
    return status.label === '未运行' ? { text: '未开始', tone: 'idle' } : { text: status.label, tone: 'warn' };
  }
  return { text: status.label, tone: status.tone };
}

/** 已译/总段数 → 迷你进度条（分母 0 或缺失 → 不显示，不编造进度）。 */
function paperProgress(doc: DocumentListItem): { done: number; total: number } | null {
  const total = doc.paragraph_count;
  const done = doc.translated_count;
  if (typeof total !== 'number' || total <= 0) return null;
  return { done: typeof done === 'number' ? Math.min(done, total) : 0, total };
}

export function PaperNav({ activeDid, route }: { activeDid: string | null; route: Route }) {
  const documentsQuery = useDocuments();
  const upload = useUploadMutation();
  const deleteMutation = useDeleteDocumentMutation();
  const documents = documentsQuery.data ?? [];
  const [query, setQuery] = useState('');
  const [rows, setRows] = useState<UploadRow[]>([]);
  const [dragActive, setDragActive] = useState(false);
  const [deleteError, setDeleteError] = useState<{ did: string; message: string } | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const counter = useRef(0);
  const menu = useUiStore((state) => state.libraryMenu);
  const openLibraryMenu = useUiStore((state) => state.openLibraryMenu);
  const closeLibraryMenu = useUiStore((state) => state.closeLibraryMenu);
  const uploadRequest = useUiStore((state) => state.uploadRequest);
  const uploading = rows.some((row) => row.status === 'uploading');

  // 空态/其他入口的「上传 PDF」→ 自增的 uploadRequest：跳过挂载时的初值，之后每变一次点一次。
  const seenUploadRequest = useRef(uploadRequest);
  useEffect(() => {
    if (uploadRequest === seenUploadRequest.current) return;
    seenUploadRequest.current = uploadRequest;
    inputRef.current?.click();
  }, [uploadRequest]);

  // 菜单打开时：Esc / 滚动都关掉（不引入常驻全局监听：只在开着的时候挂）
  useEffect(() => {
    if (menu === null) return;
    const close = () => closeLibraryMenu();
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') close();
    };
    window.addEventListener('scroll', close, true);
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('scroll', close, true);
      window.removeEventListener('keydown', onKey);
    };
  }, [closeLibraryMenu, menu]);

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
          message: '前 5 字节不是 %PDF-（服务端也会拒收 422 invalid_pdf）',
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

  const confirmDelete = () => {
    if (menu === null) return;
    const doc = documents.find((item) => item.did === menu.did);
    closeLibraryMenu();
    if (doc === undefined) return;
    // 破坏性且不可撤销（workdir 目录树会被删）：先确认再发请求。
    const ok = window.confirm(
      `删除文档「${doc.title ?? doc.did}」？\n\n这会删除它的解析产物、译文草稿与任务记录，无法撤销。(${doc.did})`,
    );
    if (!ok) return;
    setDeleteError(null);
    deleteMutation.mutate(doc.did, {
      onError: (exc) => {
        const described = describeApiError(exc);
        // 两类可预期拒绝用服务端原文（它已说清下一步）；其余套统一前缀。
        const code = exc instanceof ApiError ? exc.code : null;
        setDeleteError({
          did: doc.did,
          message:
            code === 'document_busy' || code === 'delete_not_allowed'
              ? described.message
              : `删除失败：${described.message}`,
        });
      },
    });
  };

  const normalized = query.trim().toLowerCase();
  const visible = normalized === ''
    ? documents
    : documents.filter(
        (doc) =>
          doc.did.toLowerCase().includes(normalized) ||
          (doc.title ?? '').toLowerCase().includes(normalized),
      );

  return (
    <aside
      aria-label="论文导航"
      data-od-id="nav-rail"
      data-drag={dragActive ? 'active' : 'idle'}
      onDragOver={(event) => {
        event.preventDefault();
        setDragActive(true);
      }}
      onDragLeave={(event) => {
        // dragleave 会从子元素冒泡上来：指针还在 nav 内部时（relatedTarget 仍是后代）不要关高亮。
        const next = event.relatedTarget as Node | null;
        if (next === null || !event.currentTarget.contains(next)) setDragActive(false);
      }}
      onDrop={(event) => {
        event.preventDefault();
        setDragActive(false);
        void handleFiles(Array.from(event.dataTransfer?.files ?? []));
      }}
      className={cn(
        'flex min-h-0 min-w-0 flex-col overflow-hidden bg-parchment',
        dragActive && 'outline outline-2 -outline-offset-2 outline-accent',
      )}
    >
      <div data-od-id="brand-lockup" className="flex flex-none items-center gap-[9px] border-b border-hair px-s4 py-[11px]">
        <span
          aria-hidden="true"
          className="grid h-[22px] w-[22px] flex-none place-items-center rounded-[3px] border border-hair-2 bg-ivory font-serif text-sm leading-none text-ink-2"
        >
          译
        </span>
        <span className="min-w-0">
          <span className="block truncate font-serif text-md leading-[1.15] text-ink">
            ieeTranslater
          </span>
          <span className="block truncate font-mono text-micro tracking-[0.02em] text-ink-4">
            学术 PDF 翻译工作台
          </span>
        </span>
      </div>

      <div className="flex-none px-s4 pt-s3">
        <input
          data-od-id="nav-search"
          type="search"
          value={query}
          onChange={(event) => setQuery(event.target.value)}
          placeholder="搜索论文（标题 / did）"
          aria-label="搜索论文（标题 / did）"
          className="h-[30px] w-full rounded-[3px] border border-hair bg-ivory px-s4 text-sm text-ink placeholder:text-ink-4 hover:border-hair-2 focus:border-accent focus:outline-none"
        />
      </div>

      <ScrollArea className="flex-1 px-s4 pb-s5">
        <div className="flex items-center justify-between gap-s3 px-s1 pb-s2 pt-s3">
          <span data-od-id="paper-count" className="font-mono text-micro uppercase tracking-[0.09em] text-ink-4">
            论文 · {visible.length}
          </span>
          <button
            type="button"
            aria-label="上传论文"
            title={`上传论文 PDF（上限 ${formatBytes(MAX_UPLOAD_BYTES)}）`}
            onClick={openPicker}
            disabled={uploading}
            className="grid h-[22px] w-[22px] place-items-center rounded text-ink-4 transition-colors hover:bg-sand hover:text-ink disabled:cursor-not-allowed disabled:opacity-45"
          >
            <Icon name="upload" className="h-[13px] w-[13px]" />
          </button>
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

        {documentsQuery.isPending ? (
          <p className="px-s1 py-s2 text-tiny text-ink-4">正在加载文档…</p>
        ) : null}

        {documentsQuery.isError ? (
          <ErrorCard className="my-s3" error={documentsQuery.error} data-od-id="error-card">
            <Button onClick={() => void documentsQuery.refetch()} disabled={documentsQuery.isFetching}>
              重试
            </Button>
          </ErrorCard>
        ) : null}

        {documentsQuery.isPending || documentsQuery.isError ? null : (
          <ul className="m-0 flex list-none flex-col gap-s2 p-0">
            {visible.map((doc) => {
              const active = doc.did === activeDid;
              const chip = statusChip(doc);
              const progress = paperProgress(doc);
              const done = progress !== null && progress.done >= progress.total;
              const deleting = deleteMutation.isPending && deleteMutation.variables === doc.did;
              return (
                <li key={doc.did}>
                  <button
                    type="button"
                    data-od-id={`paper-${doc.did}`}
                    data-did={doc.did}
                    aria-current={active ? 'true' : undefined}
                    aria-busy={deleting}
                    title={doc.title ?? doc.did}
                    onClick={() => {
                      window.location.hash = `#/d/${encodeURIComponent(doc.did)}/progress`;
                    }}
                    onContextMenu={(event) => {
                      event.preventDefault();
                      // 固定定位 + 视口坐标：卡片会被列表滚动裁切，absolute 菜单会跟着跑。
                      openLibraryMenu(doc.did, { x: event.clientX, y: event.clientY });
                      setDeleteError(null);
                    }}
                    className={cn(
                      'block w-full rounded border border-hair bg-ivory px-[10px] py-[9px] text-left text-ink transition-colors hover:border-hair-2 hover:bg-sand-2',
                      active && 'border-accent-line bg-accent-soft',
                      deleting && 'pointer-events-none opacity-45',
                    )}
                  >
                    <span className="line-clamp-2 block text-sm leading-[1.45] text-ink">
                      {doc.title ?? doc.did}
                    </span>
                    {doc.first_author === null || doc.first_author === undefined ? null : (
                      <span
                        data-od-id="paper-author"
                        className="mt-[2px] block truncate text-tiny text-ink-3"
                      >
                        {doc.first_author}
                      </span>
                    )}
                    <span className="mt-s1 block font-mono text-micro leading-[1.5] text-ink-4 [font-variant-numeric:tabular-nums]">
                      {countLabel(doc.pages, '页')} · {countLabel(doc.paragraph_count, '段')} · 已译{' '}
                      {countLabel(doc.translated_count, '段')} · {humanizeUpdatedAt(doc.updated_at)}
                    </span>
                    <span className="mt-[7px] flex items-center gap-s2">
                      <span
                        className={cn(
                          'inline-flex h-[19px] items-center rounded-[3px] border px-[5px] font-mono text-micro leading-none',
                          CHIP_CLASSES[chip.tone],
                        )}
                      >
                        {chip.text}
                      </span>
                    </span>
                    {progress === null ? null : (
                      <span className="mt-s2 block h-[3px] overflow-hidden rounded-[2px] bg-canvas">
                        <span
                          data-od-id="paper-progress"
                          className={cn('block h-full rounded-[2px]', done ? 'bg-pass' : 'bg-run')}
                          style={{ width: `${Math.round((progress.done / progress.total) * 100)}%` }}
                        />
                      </span>
                    )}
                  </button>

                  {deleteError === null || deleteError.did !== doc.did ? null : (
                    <p
                      data-od-id="doc-card-delete-error"
                      className="mt-1 rounded border border-hair-2 bg-sand px-s2 py-1 text-micro text-err"
                    >
                      {deleteError.message}
                    </p>
                  )}
                </li>
              );
            })}

            <li>
              <button
                type="button"
                data-od-id="paper-upload"
                onClick={openPicker}
                disabled={uploading}
                className="flex w-full items-center justify-center gap-s2 rounded border border-dashed border-hair-2 px-[10px] py-[9px] text-sm text-ink-3 transition-colors hover:border-accent hover:bg-sand-2 hover:text-ink disabled:cursor-not-allowed disabled:opacity-45"
              >
                <Icon name="upload" className="h-[13px] w-[13px]" />
                {uploading ? '正在上传…' : '上传论文 PDF'}
              </button>
            </li>
          </ul>
        )}

        {documentsQuery.isPending || documentsQuery.isError || visible.length > 0 ? null : (
          <div
            data-od-id="nav-empty"
            className="mt-s2 rounded border border-dashed border-hair-2 bg-ivory px-s4 py-s5 text-center"
          >
            <p className="font-serif text-md font-medium text-ink-2">
              {documents.length === 0 ? '还没有文档' : '没有匹配的论文'}
            </p>
            <p className="mt-s2 font-mono text-micro leading-[1.6] text-ink-4">
              {documents.length === 0
                ? '拖一个 PDF 进来，或点上面的上传按钮'
                : '换个关键词试试（标题 / did）'}
            </p>
          </div>
        )}

        {rows.length === 0 ? null : (
          <div className="mt-s3 flex flex-col gap-s2" data-od-id="upload-queue">
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
      </ScrollArea>

      {activeDid === null ? null : <DocumentActionBar did={activeDid} />}

      <nav
        data-od-id="nav-foot"
        aria-label="全局导航"
        className="flex flex-none items-center gap-s2 border-t border-hair px-s4 py-[10px]"
      >
        {([
          { href: '#/glossary', label: '词表', icon: 'glossary', kind: 'glossary' },
          { href: '#/settings', label: '设置', icon: 'settings', kind: 'settings' },
        ] as const).map((item) => (
          <a
            key={item.href}
            href={item.href}
            data-rail-item={item.kind}
            aria-current={route.kind === item.kind ? 'page' : undefined}
            className={cn(
              'inline-flex h-7 flex-1 items-center justify-center gap-[6px] rounded text-sm text-ink-3 transition-colors hover:bg-sand hover:text-ink',
              route.kind === item.kind && 'bg-sand text-ink',
            )}
          >
            <Icon name={item.icon} className="h-[13px] w-[13px]" />
            {item.label}
          </a>
        ))}
      </nav>

      {menu === null ? null : (
        <>
          {/* 点击任意处关掉菜单。遮罩覆盖全屏，所以右键另一张卡片也先落在它上面：
              那一格只关菜单，用户的下一次右键才在新卡片上开（一次一个菜单）。 */}
          <div
            className="fixed inset-0 z-40"
            onClick={() => closeLibraryMenu()}
            onContextMenu={(event) => {
              event.preventDefault();
              closeLibraryMenu();
            }}
            aria-hidden="true"
          />
          <div
            role="menu"
            data-od-id="doc-card-menu"
            data-did={menu.did}
            style={{ left: menu.x, top: menu.y }}
            className="fixed z-50 min-w-[132px] rounded border border-hair-2 bg-ivory py-1 shadow-lift"
          >
            <button
              type="button"
              role="menuitem"
              data-od-id="doc-card-delete"
              onClick={confirmDelete}
              className="block w-full px-s3 py-[5px] text-left text-tiny text-err hover:bg-sand"
            >
              删除此文档…
            </button>
          </div>
        </>
      )}
    </aside>
  );
}
