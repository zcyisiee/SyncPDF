/**
 * 段落树视图（M2-08）：按页分组展示段落，状态图标用 codicons，
 * 页节点可折叠，选中项自动滚进视野（与 PDF 两栏、段落编辑器同一份选中）。
 */
import { useEffect, useMemo, useRef, useState } from 'react';
import type { ParagraphStatus } from '@shared/protocol';
import { documentStore } from '../store/documentStore';
import { useDocumentStore } from '../store/documentStoreStoreHooks';

/** 段落状态 → codicon + 颜色 token。 */
export function statusIcon(status: ParagraphStatus): { icon: string; color: string } {
  switch (status) {
    case 'translated':
      return { icon: 'codicon-check', color: 'var(--vscode-charts-green)' };
    case 'typeset':
      return { icon: 'codicon-layout-panel-justified', color: 'var(--vscode-charts-blue)' };
    case 'not_replaced':
      return { icon: 'codicon-circle-slash', color: 'var(--vscode-editorWarning-foreground)' };
    case 'fallback':
      return { icon: 'codicon-arrow-both', color: 'var(--vscode-editorWarning-foreground)' };
  }
}

/** 状态中文名（title 提示）。 */
export function statusLabel(status: ParagraphStatus): string {
  switch (status) {
    case 'translated':
      return '已翻译';
    case 'typeset':
      return '已排版';
    case 'not_replaced':
      return '未替换';
    case 'fallback':
      return '回退';
  }
}

export function ParagraphTreeView(): JSX.Element {
  const paragraphs = useDocumentStore((state) => state.paragraphs);
  const order = useDocumentStore((state) => state.paragraphOrder);
  const selectedId = useDocumentStore((state) => state.selectedParagraphId);
  const [collapsed, setCollapsed] = useState<ReadonlySet<number>>(() => new Set<number>());
  const selectedRef = useRef<HTMLButtonElement | null>(null);

  /** 页号 → 段落 id 列表（保持事件顺序）。 */
  const byPage = useMemo(() => {
    const map = new Map<number, string[]>();
    for (const id of order) {
      const record = paragraphs[id];
      if (record === undefined) continue;
      const page = record.page ?? 0;
      const list = map.get(page);
      if (list === undefined) map.set(page, [id]);
      else list.push(id);
    }
    return [...map.entries()].sort((a, b) => a[0] - b[0]);
  }, [paragraphs, order]);

  // 选中来自 PDF 段落框 / 问题面板时，把树项滚进视野
  useEffect(() => {
    selectedRef.current?.scrollIntoView({ block: 'nearest' });
  }, [selectedId]);

  if (order.length === 0) {
    return (
      <div style={{ padding: 16, opacity: 0.6, fontSize: 12 }}>
        暂无段落（启动任务后在此显示段落树）
      </div>
    );
  }

  const toggle = (page: number): void => {
    setCollapsed((previous) => {
      const next = new Set(previous);
      if (next.has(page)) next.delete(page);
      else next.add(page);
      return next;
    });
  };

  return (
    <div role="tree" aria-label="段落树" style={{ paddingBottom: 16 }}>
      {byPage.map(([page, ids]) => {
        const isCollapsed = collapsed.has(page);
        return (
          <div key={page}>
            <button
              type="button"
              onClick={() => toggle(page)}
              aria-expanded={!isCollapsed}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: 6,
                width: '100%',
                border: 'none',
                background: 'transparent',
                color: 'inherit',
                textAlign: 'left',
                padding: '4px 12px',
                fontSize: 11,
                opacity: 0.8,
                fontWeight: 600,
                cursor: 'pointer',
              }}
            >
              <span
                className={`codicon ${isCollapsed ? 'codicon-chevron-right' : 'codicon-chevron-down'}`}
              />
              第 {page} 页
              <span style={{ marginLeft: 'auto', opacity: 0.7, fontWeight: 400 }}>{ids.length}</span>
            </button>
            {!isCollapsed &&
              ids.map((id) => {
                const record = paragraphs[id];
                const active = id === selectedId;
                const icon = statusIcon(record.status);
                return (
                  <button
                    key={id}
                    ref={active ? selectedRef : undefined}
                    type="button"
                    role="treeitem"
                    aria-selected={active}
                    title={`${id} · ${statusLabel(record.status)}`}
                    onClick={() => documentStore.getState().selectParagraph(id)}
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: 6,
                      width: '100%',
                      padding: '2px 12px 2px 28px',
                      border: 'none',
                      textAlign: 'left',
                      fontSize: 12,
                      cursor: 'pointer',
                      background: active
                        ? 'var(--vscode-list-activeSelectionBackground)'
                        : 'transparent',
                      color: active ? 'var(--vscode-list-activeSelectionForeground)' : 'inherit',
                    }}
                  >
                    <span className={`codicon ${icon.icon}`} style={{ color: icon.color }} />
                    <span style={{ flex: '0 0 auto' }}>{id}</span>
                    <span
                      style={{
                        overflow: 'hidden',
                        textOverflow: 'ellipsis',
                        whiteSpace: 'nowrap',
                        opacity: 0.6,
                      }}
                    >
                      {preview(record.translatedHtml)}
                    </span>
                  </button>
                );
              })}
          </div>
        );
      })}
    </div>
  );
}

/** 译文首行摘要（树项副标题）。 */
function preview(html: string): string {
  const text = html.replace(/<[^>]*>/g, '').trim();
  return text.length > 40 ? `${text.slice(0, 40)}…` : text;
}
