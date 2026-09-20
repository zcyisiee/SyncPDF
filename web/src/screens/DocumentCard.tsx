/**
 * 文件库文档卡（§4.4：ivory + 1px hair + r6，hover 才加 lift 阴影）。点击进工作台进度视图。
 *
 * 右键（contextmenu）弹出删除菜单。菜单状态放在 ui store（`libraryMenu`）而不是卡片自己的
 * state：**同一时刻只允许一个菜单**——卡片各自持 state 时右键第二张卡会同时开出两个菜单。
 *
 * 删除是破坏性操作：菜单里的「删除」先弹原生 `confirm` 二次确认；失败原因按服务端错误码
 * 分类展示（`document_busy` = 有活动任务；`delete_not_allowed` = workdir 模式不支持）。
 */
import { useEffect, useState } from 'react';

import type { DocumentListItem } from '../api/types';
import { ApiError, describeApiError } from '../lib/api';
import { countLabel, humanizeUpdatedAt, STAGE_NAMES } from '../lib/humanize';
import { useDeleteDocumentMutation } from '../lib/queries';
import { useUiStore } from '../stores/ui';
import { StageBadge } from '../components/ui/StatusBadge';

export function DocumentCard({ doc }: { doc: DocumentListItem }) {
  const title = doc.title ?? doc.did;
  const menu = useUiStore((state) => state.libraryMenu);
  const openLibraryMenu = useUiStore((state) => state.openLibraryMenu);
  const closeLibraryMenu = useUiStore((state) => state.closeLibraryMenu);
  const [error, setError] = useState<string | null>(null);
  const deleteMutation = useDeleteDocumentMutation();

  const menuOpen = menu !== null && menu.did === doc.did;

  // 菜单打开时：Esc / 滚动都关掉（不引入常驻全局监听：只在开着的时候挂）
  useEffect(() => {
    if (!menuOpen) return;
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
  }, [closeLibraryMenu, menuOpen]);

  const confirmDelete = () => {
    closeLibraryMenu();
    // 破坏性且不可撤销（workdir 目录树会被删）：先确认再发请求。
    const ok = window.confirm(
      `删除文档「${title}」？\n\n这会删除它的解析产物、译文草稿与任务记录，无法撤销。(${doc.did})`,
    );
    if (!ok) return;
    setError(null);
    deleteMutation.mutate(doc.did, {
      onError: (exc) => {
        const described = describeApiError(exc);
        // 两类可预期拒绝用服务端原文（它已说清下一步）；其余套统一前缀。
        const code = exc instanceof ApiError ? exc.code : null;
        setError(
          code === 'document_busy' || code === 'delete_not_allowed'
            ? described.message
            : `删除失败：${described.message}`,
        );
      },
    });
  };

  const deleting = deleteMutation.isPending;
  return (
    <div className="relative">
      <a
        href={`#/d/${encodeURIComponent(doc.did)}/progress`}
        data-od-id="doc-card"
        data-did={doc.did}
        onContextMenu={(event) => {
          event.preventDefault();
          // 固定定位 + 视口坐标：卡片会被列表滚动裁切，absolute 菜单会跟着跑。
          openLibraryMenu(doc.did, { x: event.clientX, y: event.clientY });
          setError(null);
        }}
        aria-busy={deleting}
        className={`flex min-w-0 flex-col gap-s3 rounded-card border border-hair bg-ivory p-s4 transition-shadow hover:border-hair-2 hover:shadow-lift ${
          deleting ? 'pointer-events-none opacity-45' : ''
        }`}
      >
        <div className="min-w-0">
          <h3 className="break-words font-serif text-md font-medium leading-[1.35] text-ink">
            {title}
          </h3>
          {doc.title === null || doc.title === undefined ? null : (
            <p className="mt-1 break-all font-mono text-micro text-ink-4">{doc.did}</p>
          )}
        </div>
        <p className="flex flex-wrap gap-x-s3 gap-y-[5px] font-mono text-micro text-ink-4 [font-variant-numeric:tabular-nums]">
          <span>{countLabel(doc.pages, '页')}</span>
          <span>{countLabel(doc.paragraph_count, '段')}</span>
          <span>已译 {countLabel(doc.translated_count, '段')}</span>
          <span>{humanizeUpdatedAt(doc.updated_at)}</span>
        </p>
        <div className="grid grid-cols-4 gap-[5px]">
          {STAGE_NAMES.map((stage) => (
            <StageBadge key={stage} stage={stage} status={doc.stage_summary[stage] ?? 'not_run'} />
          ))}
        </div>
      </a>

      {!menuOpen || menu === null ? null : (
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
            data-did={doc.did}
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

      {error === null ? null : (
        <p
          data-od-id="doc-card-delete-error"
          className="mt-1 rounded border border-hair-2 bg-sand px-s2 py-1 text-micro text-err"
        >
          {error}
        </p>
      )}
    </div>
  );
}
