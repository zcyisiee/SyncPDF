/**
 * 右栏检查器：块详情（原文、译文编辑与单块排版）与论文信息。
 */
import { useState } from 'react';
import type { LibraryDoc } from '@shared/library';
import { useLibrary, useOpenDocMeta } from '@/store/library';
import { useWorkbench, type InspectorTab } from '@/store/workbench';
import { IconButton, Panel, PanelHeader, type PanelView } from '@/layout/Panel';
import { REGION_STYLE } from '@/pdf/regionKinds';
import { StatusBadge } from '@/library/StatusBadge';
import { BlockEditor } from './BlockEditor';

const VIEWS: PanelView<InspectorTab>[] = [
  { id: 'block', title: '块详情', icon: 'inspect' },
  { id: 'info', title: '论文信息', icon: 'info' },
];

const PARAGRAPH_STATUS: Record<string, string> = {
  pending: '等待翻译',
  translated: '已翻译',
  typeset: '已排版',
  not_replaced: '保留原文',
  fallback: '回退为原文',
};

export function Inspector(): JSX.Element {
  const tab = useWorkbench((s) => s.inspectorTab);
  const setTab = useWorkbench((s) => s.setInspectorTab);
  const doc = useOpenDocMeta();
  return (
    <Panel className="sp-inspector">
      <PanelHeader
        views={VIEWS}
        active={tab}
        onSelect={setTab}
        actions={tab === 'info' && doc !== null ? <DocActions doc={doc} /> : undefined}
      />
      <div className="sp-panel-body">
        {doc === null ? (
          <p className="sp-hint">打开一篇论文后在这里查看详情。</p>
        ) : tab === 'block' ? (
          <BlockDetails />
        ) : (
          <DocInfo key={doc.id} doc={doc} />
        )}
      </div>
    </Panel>
  );
}

function BlockDetails(): JSX.Element {
  const selected = useLibrary((s) => s.selected);
  const paragraph = useLibrary((s) => (s.selected === null ? undefined : s.open?.paragraphs[s.selected]));
  const edit = useLibrary((s) => (s.selected === null ? undefined : s.open?.edits?.[s.selected]));
  const docId = useLibrary((s) => s.openId);
  if (selected === null || paragraph === undefined) {
    return <p className="sp-hint">在 PDF 上点击一个块查看原文与译文。</p>;
  }
  const style = REGION_STYLE[paragraph.kind];
  return (
    <div className="sp-block">
      <div className="sp-block-head">
        <span className="sp-kind" style={{ ['--kind' as string]: style.color }}>
          {style.label}
        </span>
        <code>{paragraph.paragraph_id}</code>
        <span className="sp-muted">第 {paragraph.page} 页</span>
        <span className="sp-spacer" />
        <span className={`sp-para-status status-${paragraph.status}`}>{PARAGRAPH_STATUS[paragraph.status]}</span>
      </div>
      <h3>原文</h3>
      <p className="sp-text sp-selectable">{paragraph.source_text}</p>
      <BlockEditor
        key={`${paragraph.paragraph_id}\n${paragraph.translated_html ?? ''}\n${JSON.stringify(edit ?? null)}`}
        paragraph={paragraph}
        edit={edit}
        onSubmit={(requests) => {
          if (docId !== null) void window.syncpdf.engine.edit(docId, requests);
        }}
      />
    </div>
  );
}

function DocActions({ doc }: { doc: LibraryDoc }): JSX.Element {
  const busy = doc.status === 'queued' || doc.status === 'running';
  return (
    <>
      {busy ? (
        <IconButton icon="debug-stop" title="取消翻译" onClick={() => void window.syncpdf.engine.cancel(doc.id)} />
      ) : (
        <IconButton
          icon={doc.status === 'done' ? 'refresh' : 'play'}
          title={doc.status === 'done' ? '重新翻译' : '开始翻译'}
          onClick={() => void window.syncpdf.engine.enqueue(doc.id)}
        />
      )}
      <IconButton icon="folder-opened" title="在 Finder 中显示" onClick={() => void window.syncpdf.library.reveal(doc.id)} />
    </>
  );
}

function DocInfo({ doc }: { doc: LibraryDoc }): JSX.Element {
  return (
    <div className="sp-info">
      <MetaField label="标题" value={doc.title} onCommit={(title) => updateMeta(doc.id, { title })} multiline />
      <MetaField label="作者" value={doc.authors ?? ''} onCommit={(authors) => updateMeta(doc.id, { authors })} multiline />
      <dl>
        <dt>状态</dt>
        <dd>
          <StatusBadge doc={doc} />
        </dd>
        {doc.error !== null && doc.status === 'failed' && (
          <>
            <dt>错误</dt>
            <dd className="sp-error sp-selectable">{doc.error}</dd>
          </>
        )}
        <dt>文件</dt>
        <dd className="sp-selectable">{doc.fileName}</dd>
        <dt>页数</dt>
        <dd>{doc.pages ?? '—'}</dd>
        <dt>大小</dt>
        <dd>{(doc.size / 1024 / 1024).toFixed(1)} MB</dd>
        <dt>模型</dt>
        <dd>{doc.model ?? '—'}</dd>
        <dt>用时</dt>
        <dd>{doc.elapsedMs === null ? '—' : `${(doc.elapsedMs / 1000).toFixed(1)} 秒`}</dd>
        <dt>回退段</dt>
        <dd>{doc.fallbacks}</dd>
        <dt>加入于</dt>
        <dd>{new Date(doc.addedAt).toLocaleString()}</dd>
      </dl>
    </div>
  );
}

function updateMeta(id: string, meta: { title?: string; authors?: string }): void {
  void window.syncpdf.library.updateMeta(id, meta, 'user');
}

/** 失焦或回车提交；未改动不提交。 */
function MetaField({
  label,
  value,
  onCommit,
  multiline = false,
}: {
  label: string;
  value: string;
  onCommit: (value: string) => void;
  multiline?: boolean;
}): JSX.Element {
  const [draft, setDraft] = useState(value);
  const [base, setBase] = useState(value);
  if (value !== base) {
    // 外部更新（layout 识别到标题）覆盖草稿
    setBase(value);
    setDraft(value);
  }
  const commit = (): void => {
    if (draft.trim() !== '' && draft !== value) onCommit(draft.trim());
    else setDraft(value);
  };
  return (
    <label className="sp-field">
      <span>{label}</span>
      <textarea
        rows={multiline ? 2 : 1}
        value={draft}
        onChange={(event) => setDraft(event.target.value)}
        onBlur={commit}
        onKeyDown={(event) => {
          if (event.key === 'Enter' && !event.shiftKey) {
            event.preventDefault();
            event.currentTarget.blur();
          }
        }}
      />
    </label>
  );
}
