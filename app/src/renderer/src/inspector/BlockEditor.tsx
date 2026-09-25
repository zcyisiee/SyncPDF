/**
 * 块详情的译文编辑器：contentEditable，受保护原子（`{{KEEP_n}}`）渲染为不可改的胶囊，
 * 原文样式段（`data-style`）保留为浅色底。应用 / 恢复 / 重译都经主进程排一次走缓存的重跑，
 * 译文 PDF 随 `page_ready` / `run_finished` 重载。
 */
import { useEffect, useRef, useState } from 'react';
import type { BlockAlign, BlockEditState, BlockStyle, FontFamily } from '@shared/protocol';
import type { BlockEditRequest, ParagraphRecord } from '@shared/library';
import { IconButton } from '@/layout/Panel';
import { keepsOf, missingKeeps, parseUnit, serializeUnit, type Piece } from './unitHtml';

/** 片段 → 编辑器 DOM。 */
export function renderPieces(root: HTMLElement, pieces: Piece[]): void {
  const doc = root.ownerDocument;
  root.replaceChildren();
  let group: HTMLElement | null = null;
  let groupStyle: number | null = null;
  for (const piece of pieces) {
    let parent: HTMLElement = root;
    if (piece.style !== null) {
      if (group === null || groupStyle !== piece.style) {
        group = doc.createElement('span');
        group.dataset.style = String(piece.style);
        group.className = 'sp-styled';
        group.title = `原文样式 ${piece.style}`;
        root.append(group);
        groupStyle = piece.style;
      }
      parent = group;
    } else {
      group = null;
      groupStyle = null;
    }
    if (piece.kind === 'text') parent.append(doc.createTextNode(piece.text));
    else if (piece.kind === 'br') parent.append(doc.createElement('br'));
    else parent.append(capsule(doc, piece.n));
  }
}

function capsule(doc: Document, n: number): HTMLElement {
  const el = doc.createElement('span');
  el.setAttribute('contenteditable', 'false');
  el.className = 'sp-capsule';
  el.dataset.keep = String(n);
  el.textContent = String(n);
  el.title = `受保护内容 #${n}（公式、引用、链接等）：可删除或拖动位置，不能改写`;
  return el;
}

/** 编辑器 DOM → 片段。浏览器插入的其它元素按其文本并入所在样式段。 */
export function readPieces(root: HTMLElement): Piece[] {
  const pieces: Piece[] = [];
  const walk = (node: Node, style: number | null): void => {
    for (const child of Array.from(node.childNodes)) {
      if (child.nodeType === Node.TEXT_NODE) {
        pieces.push({ kind: 'text', text: child.textContent ?? '', style });
      } else if (child instanceof HTMLElement) {
        if (child.dataset.keep !== undefined) pieces.push({ kind: 'keep', n: Number(child.dataset.keep), style });
        else if (child.tagName === 'BR') pieces.push({ kind: 'br', style });
        else walk(child, child.dataset.style !== undefined ? Number(child.dataset.style) : style);
      }
    }
  };
  walk(root, null);
  return pieces;
}

const FAMILY_LABEL: Record<FontFamily, string> = { serif: '衬线', sans: '无衬线' };
const ALIGN_LABEL: Record<BlockAlign, string> = {
  left: '左对齐',
  center: '居中',
  right: '右对齐',
  justify: '两端对齐',
};

const sameStyle = (a: BlockStyle, b: BlockStyle): boolean =>
  a.font_scale === b.font_scale &&
  a.line_height === b.line_height &&
  a.font_family === b.font_family &&
  a.align === b.align;

export interface BlockEditorProps {
  paragraph: ParagraphRecord;
  edit: BlockEditState | undefined;
  onSubmit: (requests: BlockEditRequest[]) => void;
}

export function BlockEditor({ paragraph, edit, onSubmit }: BlockEditorProps): JSX.Element {
  const html = paragraph.translated_html;
  const unit = html === null ? null : parseUnit(html);
  const baseStyle = edit?.style ?? {};
  const manual = edit?.manual ?? false;
  const editorRef = useRef<HTMLDivElement>(null);
  const [draft, setDraft] = useState<string | null>(null);
  const [style, setStyle] = useState<BlockStyle>(baseStyle);

  useEffect(() => {
    if (editorRef.current !== null && unit !== null) renderPieces(editorRef.current, unit.pieces);
    // 只在模型 / 手改译文变化时重建（组件按段 ID + 译文 key，这里只跑一次）
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const expected = unit === null ? [] : keepsOf(unit.pieces);
  // 校验将要发出的 HTML（手打的 `{{KEEP_n}}` 也会被引擎当作原子）
  const sent = draft === null ? null : parseUnit(draft);
  const actual = sent === null ? expected : keepsOf(sent.pieces);
  const missing = missingKeeps(expected, actual);
  const extra = missingKeeps(actual, expected);
  const textDirty = draft !== null && draft !== html;
  const styleDirty = !sameStyle(style, baseStyle);
  const valid = missing.length === 0 && extra.length === 0;

  const onInput = (): void => {
    if (editorRef.current === null || unit === null) return;
    setDraft(serializeUnit(unit.id, readPieces(editorRef.current)));
  };
  const resetDraft = (): void => {
    if (editorRef.current !== null && unit !== null) renderPieces(editorRef.current, unit.pieces);
    setDraft(null);
  };
  const apply = (): void => {
    onSubmit([
      {
        type: 'apply_edit',
        paragraph_id: paragraph.paragraph_id,
        // 只改排版时保留已有手改
        translated_html: textDirty ? draft : manual ? html : null,
        style,
      },
    ]);
  };
  const restore = (): void => {
    if (manual) {
      onSubmit([{ type: 'apply_edit', paragraph_id: paragraph.paragraph_id, translated_html: null, style: baseStyle }]);
    }
    resetDraft();
  };
  const retranslate = (): void => {
    onSubmit([{ type: 'retranslate', paragraph_ids: [paragraph.paragraph_id] }]);
  };

  return (
    <div className="sp-block-editor">
      <div className="sp-block-editor-head">
        <h3>译文{manual && <span className="sp-muted">（手改）</span>}</h3>
        <span className="sp-spacer" />
        <IconButton
          icon="discard"
          title={manual ? '恢复模型译文' : '撤销未应用的修改'}
          disabled={!manual && !textDirty}
          onClick={restore}
        />
        <IconButton icon="sync" title="重译此块（绕过缓存重新请求模型）" onClick={retranslate} />
        <IconButton icon="check" title="应用" disabled={!(textDirty || styleDirty) || !valid} onClick={apply} />
      </div>
      {unit === null ? (
        <p className="sp-hint">{html === null ? '暂无译文，可重译此块。' : '译文格式无法编辑，可重译此块。'}</p>
      ) : (
        <div
          ref={editorRef}
          className="sp-text sp-translation-editor"
          contentEditable
          suppressContentEditableWarning
          spellCheck={false}
          role="textbox"
          aria-label="译文"
          aria-multiline
          onInput={onInput}
          onKeyDown={(event) => {
            // 段落内编辑：回车会被浏览器插成块元素，不在译文文法里
            if (event.key === 'Enter') event.preventDefault();
          }}
          onPaste={(event) => {
            event.preventDefault();
            const text = event.clipboardData.getData('text/plain').replace(/\s*\n\s*/g, ' ');
            document.execCommand('insertText', false, text);
          }}
        />
      )}
      {!valid && (
        <p className="sp-error">
          {missing.length > 0 && `缺少受保护内容 ${missing.map((n) => `#${n}`).join('、')}`}
          {missing.length > 0 && extra.length > 0 && '；'}
          {extra.length > 0 && `多出受保护内容 ${extra.map((n) => `#${n}`).join('、')}`}
          。撤销修改后重试。
        </p>
      )}
      <h3>单块排版</h3>
      <div className="sp-style-grid">
        <label>
          <span>字号比例</span>
          <NumberField
            value={style.font_scale}
            min={0.5}
            max={2}
            step={0.05}
            onChange={(font_scale) => setStyle({ ...style, font_scale })}
          />
        </label>
        <label>
          <span>行距</span>
          <NumberField
            value={style.line_height}
            min={1}
            max={3}
            step={0.05}
            onChange={(line_height) => setStyle({ ...style, line_height })}
          />
        </label>
        <label>
          <span>字体</span>
          <select
            value={style.font_family ?? ''}
            onChange={(event) =>
              setStyle({ ...style, font_family: (event.target.value || undefined) as FontFamily | undefined })
            }
          >
            <option value="">默认</option>
            {(Object.keys(FAMILY_LABEL) as FontFamily[]).map((f) => (
              <option key={f} value={f}>
                {FAMILY_LABEL[f]}
              </option>
            ))}
          </select>
        </label>
        <label>
          <span>对齐</span>
          <select
            value={style.align ?? ''}
            onChange={(event) =>
              setStyle({ ...style, align: (event.target.value || undefined) as BlockAlign | undefined })
            }
          >
            <option value="">默认</option>
            {(Object.keys(ALIGN_LABEL) as BlockAlign[]).map((a) => (
              <option key={a} value={a}>
                {ALIGN_LABEL[a]}
              </option>
            ))}
          </select>
        </label>
      </div>
    </div>
  );
}

/** 空 = 默认（沿用整篇设置）；越界 / 非数字不提交。 */
function NumberField({
  value,
  min,
  max,
  step,
  onChange,
}: {
  value: number | undefined;
  min: number;
  max: number;
  step: number;
  onChange: (value: number | undefined) => void;
}): JSX.Element {
  const [text, setText] = useState(value === undefined ? '' : String(value));
  return (
    <input
      type="number"
      placeholder="默认"
      min={min}
      max={max}
      step={step}
      value={text}
      onChange={(event) => {
        setText(event.target.value);
        const n = event.target.valueAsNumber;
        if (event.target.value === '') onChange(undefined);
        else if (Number.isFinite(n) && n >= min && n <= max) onChange(n);
      }}
    />
  );
}
