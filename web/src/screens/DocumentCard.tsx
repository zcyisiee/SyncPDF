/**
 * 文件库文档卡（§4.4：ivory + 1px hair + r6，hover 才加 lift 阴影）。点击进工作台进度视图。
 *
 * 右键（contextmenu）弹出删除菜单：删除是**破坏性**操作，所以菜单里的「删除」需要二次
 * 确认（原生 `confirm`，与顶栏取消任务同一口径），且失败原因按服务端错误码分类展示
 * （`document_busy` = 有活动任务；`delete_not_allowed` = workdir 模式不支持）。
 *
 * 菜单在卡片内是绝对定位的小浮层：点任意处/按 Esc/滚动都关掉，不做全局单例菜单
 * （卡片数量小，每个卡片自带一个更简单，也不会出现"菜单属于哪个卡片"的状态同步）。
 */
import { useEffect, useRef, useState } from 'react';

import type { DocumentListItem } from '../api/types';
import { ApiError, describeApiError } from '../lib/api';
import { countLabel, humanizeUpdatedAt, STAGE_NAMES } from '../lib/humanize';
import { useDeleteDocumentMutation } from '../lib/queries';
import { StageBadge } from '../components/ui/StatusBadge';

export function DocumentCard({ doc }: { doc: DocumentListItem }) {
  const title = doc.title ?? doc.did;
  const [menuAt, setMenuAt] = useState<{ x: number; y: number } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const cardRef = useRef<HTMLAnchorElement>(null);
  const deleteMutation = useDeleteDocumentMutation();

  // 菜单打开时：Esc / 滚动 / 点别处都关掉（不引入全局监听：只挂当前卡片的那一次）
  useEffect(() => {
    if (menuAt === null) return;
    const close = () => setMenuAt(null);
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') close();
    };
    window.addEventListener('scroll', close, true);
    window.addEventListener('keydown', onKey);
    return () => {
      window.removeEventListener('scroll', close, true);
      window.removeEventListener('keydown', onKey);
    };
  }, [menuAt]);

  const confirmDelete = () => {
    setMenuAt(null);
    // 破坏性且不可撤销（workdir 目录树会被删）：先确认再发请求。
    const ok = window.confirm(
      `删除文档「${title}」？\n\n这会删除它的解析产物、译文草稿与任务记录，无法撤销。(${doc.did})`,
    );
    if (!ok) return;
    setError(null);
    deleteMutation.mutate(doc.did, {
      onError: (exc) => {
        const described = describeApiError(exc);
        // 两类可预期拒绝用服务端原文（它已经说清了下一步）；其余用统一文案。
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
        ref={cardRef}
        href={`#/d/${encodeURIComponent(doc.did)}/progress`}
        data-od-id="doc-card"
        data-did={doc.did}
        onContextMenu={(event) => {
          event.preventDefault();
          // 固定定位 + 视口坐标：卡片会被列表滚动裁切，absolute 菜单会跟着跑。
          setMenuAt({ x: event.clientX, y: event.clientY });
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

      {menuAt === null ? null : (
        <>
          {/* 点击任意处关掉菜单（透明遮罩在最上层，不吃卡片的点击语义） */}
          <div
            className="fixed inset-0 z-40"
            onClick={() => setMenuAt(null)}
            onContextMenu={(event) => {
              event.preventDefault();
              setMenuAt(null);
            }}
            aria-hidden="true"
          />
          <div
            role="menu"
            data-od-id="doc-card-menu"
            style={{ left: menuAt.x, top: menuAt.y }}
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
