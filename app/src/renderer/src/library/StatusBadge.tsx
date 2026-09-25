/**
 * 论文状态小标记：排队 / 翻译中 N% / 完成（含回退段数）/ 失败 / 已取消。
 */
import type { LibraryDoc } from '@shared/library';

export function statusText(doc: LibraryDoc): { text: string; tone: string; icon: string } {
  switch (doc.status) {
    case 'new':
      return { text: '未翻译', tone: 'muted', icon: 'circle-large-outline' };
    case 'queued':
      return { text: '排队中', tone: 'muted', icon: 'watch' };
    case 'running':
      return { text: `翻译中 ${Math.round(doc.progress * 100)}%`, tone: 'accent', icon: 'sync' };
    case 'done':
      return doc.fallbacks > 0
        ? { text: `完成 · ${doc.fallbacks} 段回退`, tone: 'warning', icon: 'warning' }
        : { text: '完成', tone: 'ok', icon: 'pass' };
    case 'failed':
      return { text: '失败', tone: 'error', icon: 'error' };
    case 'cancelled':
      return { text: '已取消', tone: 'muted', icon: 'circle-slash' };
  }
}

export function StatusBadge({ doc }: { doc: LibraryDoc }): JSX.Element {
  const { text, tone, icon } = statusText(doc);
  return (
    <span className={`sp-badge tone-${tone}`} title={doc.error ?? undefined}>
      <i className={`codicon codicon-${icon} ${doc.status === 'running' ? 'codicon-modifier-spin' : ''}`} />
      {text}
    </span>
  );
}
