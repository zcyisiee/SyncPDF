/**
 * 译文单元 HTML（引擎 `parse_unit_html` 的受限文法）↔ 编辑器片段。
 *
 * 文法：`<p id="Pxx-yyy">` 内只有文本、`<span data-style="n">`（不嵌套）、`<br>`、
 * `{{KEEP_n}}`（受保护原子：公式、引用、链接等）。编辑器把它摊平成带样式号的片段序列，
 * 保存时按相邻同样式分组还原；`KEEP` 只能整体删除 / 移动，不能改写。
 */

export type Piece =
  | { kind: 'text'; text: string; style: number | null }
  | { kind: 'keep'; n: number; style: number | null }
  | { kind: 'br'; style: number | null };

const ENTITIES: Record<string, string> = { '&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&#39;': "'" };
const TOKEN = /<span data-style="(\d+)">|<\/span>|<br\s*\/?>|\{\{KEEP_([1-9]\d*)\}\}/g;

/** 解析失败（不是本文法）返回 null。 */
export function parseUnit(html: string): { id: string; pieces: Piece[] } | null {
  const whole = /^\s*<p id="([^"]+)">([\s\S]*)<\/p>\s*$/.exec(html);
  if (whole === null) return null;
  const [, id, body] = whole;
  const pieces: Piece[] = [];
  let style: number | null = null;
  let last = 0;
  const pushText = (raw: string): void => {
    if (raw === '') return;
    const text = raw.replace(/&(?:amp|lt|gt|quot|#39);/g, (e) => ENTITIES[e]);
    pieces.push({ kind: 'text', text, style });
  };
  for (const match of body.matchAll(TOKEN)) {
    const before = body.slice(last, match.index);
    if (before.includes('<')) return null;
    pushText(before);
    last = match.index + match[0].length;
    if (match[1] !== undefined) {
      if (style !== null) return null;
      style = Number(match[1]);
    } else if (match[0] === '</span>') {
      if (style === null) return null;
      style = null;
    } else if (match[2] !== undefined) {
      pieces.push({ kind: 'keep', n: Number(match[2]), style });
    } else {
      pieces.push({ kind: 'br', style });
    }
  }
  const rest = body.slice(last);
  if (rest.includes('<') || style !== null) return null;
  pushText(rest);
  return { id, pieces };
}

const escape = (text: string): string => text.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');

export function serializeUnit(id: string, pieces: Piece[]): string {
  let out = `<p id="${id}">`;
  let open: number | null = null;
  for (const piece of pieces) {
    if (piece.kind === 'text' && piece.text === '') continue;
    if (piece.style !== open) {
      if (open !== null) out += '</span>';
      if (piece.style !== null) out += `<span data-style="${piece.style}">`;
      open = piece.style;
    }
    out += piece.kind === 'text' ? escape(piece.text) : piece.kind === 'keep' ? `{{KEEP_${piece.n}}}` : '<br>';
  }
  if (open !== null) out += '</span>';
  return `${out}</p>`;
}

/** 原子号出现次数（排序后的列表），用于判断胶囊是否缺失 / 重复。 */
export function keepsOf(pieces: Piece[]): number[] {
  return pieces.flatMap((p) => (p.kind === 'keep' ? [p.n] : [])).sort((a, b) => a - b);
}

/** 相对 `expected`，`actual` 缺了哪些原子（多重集差）。 */
export function missingKeeps(expected: number[], actual: number[]): number[] {
  const left = [...actual];
  return expected.filter((n) => {
    const i = left.indexOf(n);
    if (i < 0) return true;
    left.splice(i, 1);
    return false;
  });
}
