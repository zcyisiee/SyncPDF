/**
 * @vitest-environment jsdom
 *
 * 块译文编辑器：单元 HTML ↔ 片段 ↔ DOM 往返；受保护原子只能整体增删；
 * 单独编译 / 撤回 / 重新翻译发出的请求。
 */
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ParagraphRecord } from '@shared/library';
import { BlockEditor, readPieces, renderPieces } from '@/inspector/BlockEditor';
import { keepsOf, missingKeeps, parseUnit, serializeUnit } from '@/inspector/unitHtml';

afterEach(cleanup);

const HTML = '<p id="P02-007">我们用 <span data-style="1">{{KEEP_1}} 模型</span>训练 {{KEEP_2}}&amp;<br>评估 {{KEEP_2}}</p>';

const paragraph = (html: string | null): ParagraphRecord => ({
  paragraph_id: 'P02-007',
  page: 2,
  status: 'typeset',
  boxes: [],
  coord_system: 'pdf_user',
  translated_html: html,
  kind: 'text',
  source_text: 'We train …',
  source_bbox: { x0: 0, y0: 0, x1: 1, y1: 1 },
});

describe('unitHtml', () => {
  it('解析与序列化往返（样式段、原子、换行、实体）', () => {
    const unit = parseUnit(HTML);
    expect(unit?.id).toBe('P02-007');
    expect(unit?.pieces).toEqual([
      { kind: 'text', text: '我们用 ', style: null },
      { kind: 'keep', n: 1, style: 1 },
      { kind: 'text', text: ' 模型', style: 1 },
      { kind: 'text', text: '训练 ', style: null },
      { kind: 'keep', n: 2, style: null },
      { kind: 'text', text: '&', style: null },
      { kind: 'br', style: null },
      { kind: 'text', text: '评估 ', style: null },
      { kind: 'keep', n: 2, style: null },
    ]);
    expect(serializeUnit('P02-007', unit!.pieces)).toBe(HTML);
    expect(keepsOf(unit!.pieces)).toEqual([1, 2, 2]);
  });

  it('不在文法里的 HTML 返回 null', () => {
    expect(parseUnit('<p>无 id</p>')).toBeNull();
    expect(parseUnit('<p id="P01-001"><b>粗</b></p>')).toBeNull();
    expect(parseUnit('<p id="P01-001"><span data-style="1"><span data-style="2">x</span></span></p>')).toBeNull();
    expect(parseUnit('<p id="P01-001"></span></p>')).toBeNull();
  });

  it('原子差按多重集计算', () => {
    expect(missingKeeps([1, 2, 2], [2, 1])).toEqual([2]);
    expect(missingKeeps([1], [1, 1])).toEqual([]);
    expect(missingKeeps([1], [1, 1].slice(0, 0))).toEqual([1]);
  });

  it('片段 → DOM → 片段：原子是不可编辑胶囊，样式段合并为一个 span', () => {
    const root = document.createElement('div');
    const pieces = parseUnit(HTML)!.pieces;
    renderPieces(root, pieces);
    const capsules = root.querySelectorAll('.sp-capsule');
    expect(capsules).toHaveLength(3);
    expect(capsules[0].getAttribute('contenteditable')).toBe('false');
    expect(root.querySelectorAll('[data-style]')).toHaveLength(1);
    expect(serializeUnit('P02-007', readPieces(root))).toBe(HTML);
  });
});

describe('BlockEditor', () => {
  const setup = (html: string | null, edit?: Parameters<typeof BlockEditor>[0]['edit']) => {
    const onSubmit = vi.fn();
    render(<BlockEditor paragraph={paragraph(html)} edit={edit} onSubmit={onSubmit} />);
    return { onSubmit, editor: screen.queryByRole('textbox'), apply: screen.getByLabelText('单独编译此块（只重排所在页）') };
  };

  it('改文字后应用：发出手改译文，原子与样式段保留', () => {
    const { onSubmit, editor, apply } = setup(HTML);
    expect(apply).toHaveProperty('disabled', true);
    const text = Array.from(editor!.childNodes).find((n) => n.textContent === '训练 ')!;
    text.textContent = '训练了 ';
    fireEvent.input(editor!);
    fireEvent.click(apply);
    expect(onSubmit).toHaveBeenCalledWith([
      {
        type: 'apply_edit',
        paragraph_id: 'P02-007',
        translated_html: HTML.replace('训练 ', '训练了 '),
        style: {},
      },
    ]);
  });

  it('删掉胶囊：提示缺失且不能应用；手打原子文本也算多出', () => {
    const { editor, apply } = setup(HTML);
    editor!.querySelector('.sp-capsule')!.remove();
    fireEvent.input(editor!);
    expect(apply).toHaveProperty('disabled', true);
    expect(screen.getByText(/缺少受保护内容 #1/)).toBeTruthy();

    cleanup();
    const again = setup(HTML);
    again.editor!.append(document.createTextNode('{{KEEP_1}}'));
    fireEvent.input(again.editor!);
    expect(again.apply).toHaveProperty('disabled', true);
    expect(screen.getByText(/多出受保护内容 #1/)).toBeTruthy();
  });

  it('只改排版：保留已有手改译文；未手改时译文为 null', () => {
    const manual = setup(HTML, { paragraph_id: 'P02-007', manual: true, style: { align: 'center' } });
    fireEvent.change(screen.getByLabelText('字号比例'), { target: { value: '0.9' } });
    fireEvent.click(manual.apply);
    expect(manual.onSubmit).toHaveBeenCalledWith([
      { type: 'apply_edit', paragraph_id: 'P02-007', translated_html: HTML, style: { align: 'center', font_scale: 0.9 } },
    ]);

    cleanup();
    const model = setup(HTML);
    fireEvent.change(screen.getByLabelText('字体'), { target: { value: 'sans' } });
    fireEvent.click(model.apply);
    expect(model.onSubmit).toHaveBeenCalledWith([
      { type: 'apply_edit', paragraph_id: 'P02-007', translated_html: null, style: { font_family: 'sans' } },
    ]);
  });

  it('越界字号不生效；恢复模型译文保留排版；重译只发段 ID', () => {
    const { onSubmit, apply } = setup(HTML, { paragraph_id: 'P02-007', manual: true, style: { line_height: 1.5 } });
    fireEvent.change(screen.getByLabelText('字号比例'), { target: { value: '9' } });
    expect(apply).toHaveProperty('disabled', true);
    fireEvent.click(screen.getByLabelText('撤回手改，恢复模型译文'));
    fireEvent.click(screen.getByLabelText('重新翻译此块（绕过缓存重新请求模型）'));
    expect(onSubmit.mock.calls).toEqual([
      [[{ type: 'apply_edit', paragraph_id: 'P02-007', translated_html: null, style: { line_height: 1.5 } }]],
      [[{ type: 'retranslate', paragraph_ids: ['P02-007'] }]],
    ]);
  });

  it('撤回：有未编译的改动时只在本地丢弃，不动已编译的手改', () => {
    const { onSubmit, editor, apply } = setup(HTML, { paragraph_id: 'P02-007', manual: true, style: {} });
    const text = Array.from(editor!.childNodes).find((n) => n.textContent === '训练 ')!;
    text.textContent = '训练了 ';
    fireEvent.input(editor!);
    fireEvent.click(screen.getByLabelText('撤回未编译的修改'));
    expect(onSubmit).not.toHaveBeenCalled();
    expect(editor!.textContent).toContain('训练 ');
    expect(apply).toHaveProperty('disabled', true);
  });

  it('无译文：不渲染编辑器，仍可重译', () => {
    const { editor, onSubmit } = setup(null);
    expect(editor).toBeNull();
    fireEvent.click(screen.getByLabelText('重新翻译此块（绕过缓存重新请求模型）'));
    expect(onSubmit).toHaveBeenCalledOnce();
  });
});
