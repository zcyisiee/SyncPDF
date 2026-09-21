/**
 * 占位视图（M2-08 深化）：文档 / 术语 / 设置。
 */
import type { ViewId } from '../store/uiStore';

export function PlaceholderView({ view }: { view: ViewId }): JSX.Element {
  return (
    <div style={{ padding: 16, opacity: 0.7, fontSize: 12 }}>
      {label(view)}视图（占位，后续任务实现）
    </div>
  );
}

function label(view: ViewId): string {
  switch (view) {
    case 'documents':
      return '文档';
    case 'terminology':
      return '术语';
    case 'settings':
      return '设置';
    case 'paragraphs':
      return '段落';
  }
}
