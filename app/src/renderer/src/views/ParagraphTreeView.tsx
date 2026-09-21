/**
 * 段落树视图（占位深度版）：按页分组展示 store 中的段落，带状态图标。
 * 页 → 段两级（§12.1 侧栏段落树）。
 */
import { useMemo } from 'react';
import { useDocumentStore } from '../store/documentStoreStoreHooks';
import { useUiStore } from '../store/uiStore';
import type { ParagraphStatus } from '@shared/protocol';

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

export function ParagraphTreeView(): JSX.Element {
  const paragraphs = useDocumentStore((state) => state.paragraphs);
  const order = useDocumentStore((state) => state.paragraphOrder);
  const selectedId = useUiStore((state) => state.selectedParagraphId);
  const selectParagraph = useUiStore((state) => state.selectParagraph);

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

  if (order.length === 0) {
    return (
      <div style={{ padding: 16, opacity: 0.6, fontSize: 12 }}>
        暂无段落（启动任务后在此显示段落树）
      </div>
    );
  }

  return (
    <div role="tree" aria-label="段落树" style={{ paddingBottom: 16 }}>
      {byPage.map(([page, ids]) => (
        <div key={page}>
          <div
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              padding: '4px 12px',
              fontSize: 11,
              opacity: 0.75,
              fontWeight: 600,
            }}
          >
            <span className="codicon codicon-chevron-down" />
            第 {page} 页
          </div>
          {ids.map((id) => {
            const record = paragraphs[id];
            const active = id === selectedId;
            const icon = statusIcon(record.status);
            return (
              <button
                key={id}
                type="button"
                role="treeitem"
                aria-selected={active}
                onClick={() => selectParagraph(id)}
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
                  color: active
                    ? 'var(--vscode-list-activeSelectionForeground)'
                    : 'inherit',
                }}
              >
                <span className={`codicon ${icon.icon}`} style={{ color: icon.color }} />
                <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                  {id}
                </span>
              </button>
            );
          })}
        </div>
      ))}
    </div>
  );
}
