/**
 * 段落编辑器（M2-09）：选中段落的原文标识 / 状态 / 译文 HTML 编辑。
 *
 * - "应用编辑"：`apply_edit{ base_revision }`，base_revision 取提交那一刻
 *   documentStore 的 revision（协议 §9.2：不匹配引擎回 `error{code:"conflict"}`）；
 * - 冲突提示：提交后监听 `lastError`，seq 比提交时新且 code === 'conflict' 就提示
 *   "文档已被更新"，并给一个"用最新译文覆盖草稿"的出口；
 * - "重译"：`retranslate{ paragraph_ids:[id] }`。
 */
import { useCallback, useEffect, useRef, useState } from 'react';
import { documentStore } from '../store/documentStore';
import { useDocumentStore } from '../store/documentStoreStoreHooks';

type Submission = 'idle' | 'sending' | 'sent' | 'conflict' | 'failed';

export function ParagraphEditor(): JSX.Element {
  const selectedId = useDocumentStore((state) => state.selectedParagraphId);
  const paragraphs = useDocumentStore((state) => state.paragraphs);
  const docId = useDocumentStore((state) => state.docId);
  const revision = useDocumentStore((state) => state.revision);
  const record = selectedId !== null ? paragraphs[selectedId] : undefined;
  const engineHtml = record?.translatedHtml ?? '';

  const [draft, setDraft] = useState(engineHtml);
  const [status, setStatus] = useState<Submission>('idle');
  const [message, setMessage] = useState<string | null>(null);
  /**
   * 未决提交：base_revision、提交时的 lastError.seq（用来识别"这次提交引发的错误"）
   * 以及提交的段落 id（选中被换走后这条回执就不再属于当前视图）。
   */
  const pendingRef = useRef<{
    baseRevision: number;
    sinceSeq: number;
    paragraphId: string;
  } | null>(null);
  const [loadedId, setLoadedId] = useState<string | null>(selectedId);

  // 换段落：渲染期直接调整状态（React 官方的"props 变化时调整 state"写法，
  // 比 useEffect + setState 少一轮渲染，也不触发 set-state-in-effect 规则）。
  if (selectedId !== loadedId) {
    setLoadedId(selectedId);
    setDraft(engineHtml);
    setStatus('idle');
    setMessage(null);
  }

  // 提交后的引擎回执：conflict / 其他 error。
  // 订阅外部 store（setState 在回调里，不在 effect 体内）。
  useEffect(() => {
    return documentStore.subscribe((state) => {
      const pending = pendingRef.current;
      const error = state.lastError;
      if (pending === null) return;
      if (state.selectedParagraphId !== pending.paragraphId) {
        // 用户已经切到别的段落：这条回执与当前视图无关
        pendingRef.current = null;
        return;
      }
      if (error === null || error.seq <= pending.sinceSeq) return;
      pendingRef.current = null;
      if (error.code === 'conflict') {
        setStatus('conflict');
        setMessage(
          `文档已被更新（提交时 base_revision=${pending.baseRevision}，当前 revision=${state.revision}）。请先加载最新译文再编辑。`,
        );
      } else {
        setStatus('failed');
        setMessage(`${error.code}: ${error.message}`);
      }
    });
  }, []);

  const dirty = record !== undefined && draft !== engineHtml;

  const applyEdit = useCallback(async () => {
    if (record === undefined || docId === null) return;
    const baseRevision = documentStore.getState().revision;
    pendingRef.current = {
      baseRevision,
      sinceSeq: documentStore.getState().lastError?.seq ?? 0,
      paragraphId: record.id,
    };
    setStatus('sending');
    setMessage(null);
    try {
      await window.syncpdf.applyEdit({
        type: 'apply_edit',
        doc_id: docId,
        paragraph_id: record.id,
        translated_html: draft,
        base_revision: baseRevision,
      });
      setStatus('sent');
      setMessage(`已提交（base_revision=${baseRevision}），等待重排版…`);
    } catch (error) {
      pendingRef.current = null;
      setStatus('failed');
      setMessage(describe(error));
    }
  }, [record, docId, draft]);

  const retranslate = useCallback(async () => {
    if (record === undefined || docId === null) return;
    setStatus('sending');
    setMessage(null);
    try {
      await window.syncpdf.retranslate({
        type: 'retranslate',
        doc_id: docId,
        paragraph_ids: [record.id],
      });
      setStatus('sent');
      setMessage('已请求重译…');
    } catch (error) {
      setStatus('failed');
      setMessage(describe(error));
    }
  }, [record, docId]);

  if (record === undefined) {
    return (
      <div
        style={{ display: 'grid', placeItems: 'center', height: '100%', opacity: 0.5, fontSize: 12 }}
      >
        在源 PDF 上点击段落框，或在段落树中选择一个段落
      </div>
    );
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', height: '100%', minWidth: 0 }}>
      <div
        style={{
          flex: '0 0 auto',
          display: 'flex',
          alignItems: 'center',
          gap: 8,
          padding: '6px 12px',
          fontSize: 11,
          borderBottom: '1px solid var(--vscode-editorGroup-border)',
        }}
      >
        <span style={{ fontWeight: 600 }}>{record.id}</span>
        <span style={{ opacity: 0.75 }}>{record.status}</span>
        <span style={{ opacity: 0.55 }}>rev {revision}</span>
        {dirty && <span style={{ color: 'var(--vscode-editorWarning-foreground)' }}>已修改</span>}
      </div>

      <textarea
        aria-label="译文 HTML"
        value={draft}
        spellCheck={false}
        onChange={(domEvent) => setDraft(domEvent.target.value)}
        style={{
          flex: 1,
          minHeight: 0,
          width: '100%',
          boxSizing: 'border-box',
          resize: 'none',
          border: 'none',
          outline: 'none',
          padding: 12,
          fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
          fontSize: 12,
          lineHeight: 1.6,
          background: 'var(--vscode-editor-background)',
          color: 'var(--vscode-editor-foreground)',
        }}
      />

      <div
        aria-label="译文预览"
        className="syncpdf-scroll"
        style={{
          flex: '0 0 96px',
          overflow: 'auto',
          padding: '8px 12px',
          fontSize: 13,
          lineHeight: 1.7,
          whiteSpace: 'pre-wrap',
          borderTop: '1px solid var(--vscode-editorGroup-border)',
          background: 'var(--vscode-editorWidget-background, #252526)',
        }}
      >
        {/* 译文 HTML 来自引擎（受信），预览仍用纯文本降级，不给注入面 */}
        {stripTags(draft)}
      </div>

      {message !== null && (
        <div
          role="status"
          style={{
            padding: '6px 12px',
            fontSize: 11,
            color:
              status === 'conflict' || status === 'failed'
                ? 'var(--vscode-errorForeground)'
                : 'var(--vscode-descriptionForeground, inherit)',
            background:
              status === 'conflict'
                ? 'var(--vscode-inputValidation-warningBackground, transparent)'
                : 'transparent',
          }}
        >
          {message}
          {status === 'conflict' && (
            <button
              type="button"
              onClick={() => {
                setDraft(engineHtml);
                setStatus('idle');
                setMessage(null);
              }}
              style={linkButtonStyle}
            >
              加载最新译文
            </button>
          )}
        </div>
      )}

      <div
        style={{
          flex: '0 0 auto',
          display: 'flex',
          gap: 8,
          padding: 8,
          borderTop: '1px solid var(--vscode-editorGroup-border)',
        }}
      >
        <ActionButton
          label="应用编辑"
          icon="codicon-check"
          primary
          disabled={!dirty || status === 'sending' || docId === null}
          onClick={() => void applyEdit()}
        />
        <ActionButton
          label="重译"
          icon="codicon-sync"
          disabled={status === 'sending' || docId === null}
          onClick={() => void retranslate()}
        />
        <ActionButton
          label="还原"
          icon="codicon-discard"
          disabled={!dirty}
          onClick={() => {
            setDraft(engineHtml);
            setStatus('idle');
            setMessage(null);
          }}
        />
      </div>
    </div>
  );
}

const linkButtonStyle: React.CSSProperties = {
  marginLeft: 8,
  border: 'none',
  background: 'transparent',
  color: 'var(--vscode-textLink-foreground, #3794ff)',
  cursor: 'pointer',
  fontSize: 11,
  padding: 0,
  textDecoration: 'underline',
};

function ActionButton({
  label,
  icon,
  onClick,
  disabled,
  primary,
}: {
  label: string;
  icon: string;
  onClick: () => void;
  disabled?: boolean;
  primary?: boolean;
}): JSX.Element {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      style={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 6,
        padding: '4px 10px',
        fontSize: 12,
        border: '1px solid var(--vscode-button-border, transparent)',
        borderRadius: 2,
        cursor: disabled === true ? 'default' : 'pointer',
        opacity: disabled === true ? 0.45 : 1,
        background: primary === true
          ? 'var(--vscode-button-background, #0e639c)'
          : 'var(--vscode-button-secondaryBackground, #3a3d41)',
        color: primary === true
          ? 'var(--vscode-button-foreground, #ffffff)'
          : 'var(--vscode-button-secondaryForeground, #ffffff)',
      }}
    >
      <span className={`codicon ${icon}`} />
      {label}
    </button>
  );
}

/** 极简去标签（预览只要读感，不做 HTML 渲染）。 */
export function stripTags(html: string): string {
  return html
    .replace(/<br\s*\/?>/gi, '\n')
    .replace(/<\/p>/gi, '\n')
    .replace(/<[^>]*>/g, '')
    .replace(/&nbsp;/g, ' ')
    .replace(/&amp;/g, '&')
    .replace(/&lt;/g, '<')
    .replace(/&gt;/g, '>')
    .trim();
}

function describe(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}
