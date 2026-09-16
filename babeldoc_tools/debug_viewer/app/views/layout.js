/* 识别视图：源 PDF + layout block/span 框 + 段落虚线框（+按需字符层）。

数据：
- snapshots/parse/page-frames.json → 页面几何（宽/高/旋转/裁页映射）
- snapshots/parse/layout.json     → 适配后 layout 区域（含 conf）
- snapshots/parse/paragraphs.json → 段落（debug_id + layout_id 关联）
- snapshots/parse/native-chars.json → 字符层（按页开关，默认关）
- artifacts/parse/alignment.json → 行内公式保护区（默认关，图例可开）
- artifacts/parse/prepared.pdf | input.pdf → 源页面渲染
*/
'use strict';

import { labelColor } from '../pager.js';

const $ = (sel, root) => (root || document).querySelector(sel);

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

export function createLayoutView(ctx) {
  const state = {
    frames: null,
    layout: null,       // {pages: [{page_index, height, entities}]}
    paragraphs: null,   // {entities, relations}
    chars: null,        // native-chars（整篇一次拉取，按页开关渲染）
    selection: null,
    alignment: null,    // artifacts/parse/alignment.json（行内公式保护区）
    hidden: new Set(),
    confMin: 0,
    showParagraphs: true,
    showChars: new Set(),   // 开启字符层的页
    byPage: new Map(),      // page_index → boxes[]
  };

  /* 行内公式保护区：alignment.json 的 protected_inline_math（IL 左下原点坐标）。
     新归档 layout.json 已含这些 formula 区域（同 id，采集在保护 pass 之后）→
     改标签为「行内公式」；旧归档缺这些区域 → 按页高翻转补框。 */
  function applyInlineProtection() {
    const items = (state.alignment && state.alignment.protected_inline_math) || [];
    if (!items.length) return;
    const frames = (state.frames && state.frames.frames) || [];
    for (const item of items) {
      const pi = item.page_index;
      if (pi == null || !Array.isArray(item.box) || item.box.length !== 4) continue;
      const eid = `L${String(pi + 1).padStart(2, '0')}-${String(item.layout_id).padStart(3, '0')}`;
      const list = state.byPage.get(pi) || [];
      const existing = list.find((b) => b.id === eid);
      if (existing) {
        existing.label = '行内公式';
        existing.kind = 'inline_formula';
        continue;
      }
      const height = (frames[pi] && frames[pi].height) || 0;
      if (!height) continue;
      const [x0, y0, x1, y1] = item.box.map(Number);
      list.push({
        id: eid,
        box: { x0, y0: height - y1, x1, y1: height - y0 },
        label: '行内公式',
        kind: 'inline_formula',
        layer: 'layout',
        cls: '',
        color: labelColor('行内公式'),
        preview: '',
      });
      state.byPage.set(pi, list);
    }
  }

  function ensureBoxes() {
    state.byPage.clear();
    const layoutPages = (state.layout && state.layout.pages) || [];
    for (const page of layoutPages) {
      const boxes = [];
      for (const entity of page.entities || []) {
        if (!entity.box) continue;
        const conf = entity.attrs && entity.attrs.conf;
        boxes.push({
          id: entity.id,
          box: entity.box,
          label: entity.label || 'unknown',
          kind: 'layout',
          conf,
          layer: 'block',
          cls: '',
          preview: '',
        });
      }
      state.byPage.set(page.page_index, boxes);
    }
    for (const entity of (state.paragraphs && state.paragraphs.entities) || []) {
      if (!entity.box) continue;
      const list = state.byPage.get(entity.page - 1) || [];
      list.push({
        id: entity.id,
        box: entity.box,
        label: `¶ ${entity.label || 'paragraph'}`,
        kind: 'paragraph',
        layer: 'paragraph',
        cls: 'dashed',
        color: labelColor(`paragraph:${entity.label || 'text'}`),
        preview: (entity.attrs && entity.attrs.unicode || '').slice(0, 120),
        data: entity,
      });
      state.byPage.set(entity.page - 1, list);
    }
    applyInlineProtection();
  }

  function visibleBoxes(pageIndex) {
    const boxes = state.byPage.get(pageIndex) || [];
    const out = [];
    for (const box of boxes) {
      if (box.layer === 'paragraph' && !state.showParagraphs) continue;
      if (box.layer === 'char' && !state.showChars.has(pageIndex)) continue;
      if (state.hidden.has(box.label)) continue;
      if (box.conf != null && box.conf < state.confMin) continue;
      out.push(box);
    }
    if (state.showChars.has(pageIndex) && state.chars) {
      const page = (state.chars.pages || []).find((p) => p.page_index === pageIndex);
      for (const ch of (page && page.chars) || []) {
        if (!ch.box) continue;
        out.push({
          id: ch.id, box: ch.box, label: `char ${ch.u || ''}`,
          kind: 'char', layer: 'char', cls: 'dotted thin',
          color: '#64748b', preview: ch.u,
        });
      }
    }
    return out;
  }

  function sourcePdf(manifest) {
    const artifacts = Object.keys((manifest && manifest.artifacts) || {});
    for (const name of ['artifacts/parse/prepared.pdf', 'artifacts/parse/input.pdf']) {
      if (artifacts.includes(name)) return name.replace(/^artifacts\//, '');
    }
    return null;
  }

  /* ------------------------------------------------------------ panel */
  function renderPanel() {
    const head = ctx.panelHead;
    const body = ctx.panelBody;
    head.replaceChildren(el('h3', null, '识别证据'));
    body.replaceChildren();

    const labels = {};
    for (const boxes of state.byPage.values()) {
      for (const box of boxes) {
        if (box.layer === 'char') continue;
        labels[box.label] = (labels[box.label] || 0) + 1;
      }
    }
    const legendSec = el('section');
    legendSec.append(el('h4', null, '类别 / 图例'));
    const allRow = el('div', 'ctl');
    const allOn = el('button', null, '全选');
    const allOff = el('button', null, '全不选');
    allOn.onclick = () => { state.hidden.clear(); renderPanel(); ctx.pager.refresh(); };
    allOff.onclick = () => {
      Object.keys(labels).forEach((l) => state.hidden.add(l));
      renderPanel(); ctx.pager.refresh();
    };
    allRow.append(allOn, allOff);
    legendSec.append(allRow);
    for (const [label, count] of Object.entries(labels).sort()) {
      const item = el('div', 'legend-item');
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.checked = !state.hidden.has(label);
      const sw = el('span', 'legend-swatch');
      sw.style.background = labelColor(label.replace(/^¶ /, 'paragraph:'));
      item.append(input, sw, el('span', null, label), el('span', 'legend-count', String(count)));
      item.onclick = () => {
        if (state.hidden.has(label)) state.hidden.delete(label);
        else state.hidden.add(label);
        renderPanel(); ctx.pager.refresh();
      };
      legendSec.append(item);
    }
    body.append(legendSec);

    const ctlSec = el('section');
    ctlSec.append(el('h4', null, '显示'));
    const confRow = el('div', 'ctl');
    confRow.append(el('span', null, '置信度 ≥'));
    const conf = document.createElement('input');
    conf.type = 'range'; conf.min = '0'; conf.max = '1'; conf.step = '0.05';
    conf.value = String(state.confMin);
    const confVal = el('span', 'val', state.confMin.toFixed(2));
    conf.oninput = () => { state.confMin = Number(conf.value); confVal.textContent = conf.value; ctx.pager.refresh(); };
    confRow.append(conf, confVal);
    ctlSec.append(confRow);

    const opRow = el('div', 'ctl');
    opRow.append(el('span', null, '填充'));
    const op = document.createElement('input');
    op.type = 'range'; op.min = '0'; op.max = '60'; op.value = '14';
    const opVal = el('span', 'val', '14%');
    op.oninput = () => {
      document.documentElement.style.setProperty('--fill-a', String(op.value / 100));
      opVal.textContent = `${op.value}%`;
    };
    opRow.append(op, opVal);
    ctlSec.append(opRow);

    const labRow = el('div', 'ctl');
    const lab = document.createElement('input');
    lab.type = 'checkbox';
    labRow.append(lab, el('span', null, '常显标签'));
    lab.onchange = () => document.body.classList.toggle('show-labels', lab.checked);
    ctlSec.append(labRow);

    const paraRow = el('div', 'ctl');
    const para = document.createElement('input');
    para.type = 'checkbox'; para.checked = state.showParagraphs;
    para.onchange = () => { state.showParagraphs = para.checked; ctx.pager.refresh(); };
    paraRow.append(para, el('span', null, '段落框（虚线）'));
    ctlSec.append(paraRow);

    const charRow = el('div', 'ctl');
    const char = document.createElement('input');
    char.type = 'checkbox';
    char.disabled = !state.chars;
    charRow.append(char, el('span', null, '字符层（当前页）'));
    char.onchange = async () => {
      const idx = ctx.currentPage;
      if (char.checked) state.showChars.add(idx); else state.showChars.delete(idx);
      ctx.pager.refresh();
    };
    ctlSec.append(charRow);
    body.append(ctlSec);

    const detail = el('section');
    detail.id = 'layout-detail';
    body.append(detail);
    renderDetail(null);
  }

  function renderDetail(box) {
    const sec = $('#layout-detail', ctx.panelBody);
    if (!sec) return;
    sec.replaceChildren(el('h4', null, '选中实体'));
    if (!box) {
      sec.append(el('div', 'empty', '点击页面框查看详情；相交框弹出命中列表。'));
      return;
    }
    const kv = el('dl', 'kv');
    const add = (k, v) => { kv.append(el('dt', null, k), el('dd', null, v == null ? '–' : String(v))); };
    add('id', box.id);
    add('label', box.label);
    if (box.conf != null) add('conf', Number(box.conf).toFixed(3));
    const b = box.box;
    add('box', `[${[b.x0, b.y0, b.x1, b.y1].map((v) => Math.round(v)).join(', ')}] pt`);
    add('layer', box.layer || box.kind);
    const rel = (state.paragraphs && state.paragraphs.relations || []).find(
      (r) => r.from_id === box.id || r.to_id === box.id
    );
    if (rel) add('关联', `${rel.kind}: ${rel.from_id} → ${rel.to_id}（${rel.method}）`);
    sec.append(kv);
    const text = box.preview || (box.data && box.data.attrs && box.data.attrs.unicode) || '';
    if (text) sec.append(el('pre', 'block', text));
  }

  /* ------------------------------------------------------------ mount */
  async function mount() {
    const run = ctx.run;
    const [frames, layout, paragraphs, chars, selection, alignment] = await Promise.all([
      ctx.api.snapshot(run, 'parse/page-frames.json'),
      ctx.api.snapshot(run, 'parse/layout.json'),
      ctx.api.snapshot(run, 'parse/paragraphs.json'),
      ctx.api.snapshot(run, 'parse/native-chars.json'),
      ctx.api.snapshot(run, 'parse/selection.json'),
      ctx.api.artifactJson(run, 'artifacts/parse/alignment.json'),
    ]);
    state.frames = frames;
    state.layout = layout;
    state.paragraphs = paragraphs;
    state.chars = chars;
    state.selection = selection;
    state.alignment = alignment;
    ensureBoxes();
    /* 行内公式保护区数量多且嵌在正文里：默认关，经图例勾选开启。 */
    if (((alignment && alignment.protected_inline_math) || []).length) {
      state.hidden.add('行内公式');
    }
    const pages = (frames && frames.frames) ||
      (layout ? layout.pages.map((p) => ({ index: p.page_index, width: 1, height: p.height || 1 })) : []);
    const normalized = pages.map((f, i) => ({
      index: f.page_index != null ? f.page_index : i,
      width: f.width || 612,
      height: f.height || 792,
    }));
    ctx.pager.setDocument(run, sourcePdf(ctx.manifest), normalized);
    ctx.pager.setOverlayProvider((idx) => visibleBoxes(idx));
    renderPanel();
    ctx.setStageStatus(
      frames ? '' : '缺少 page-frames 快照（旧 run 或采集不完整）', frames ? '' : 'error'
    );
  }

  function onSelect(id, box) {
    /* 深链/跨视图跳转只带 id：回查 byPage 拿完整框数据。 */
    if (!box && id) {
      for (const boxes of state.byPage.values()) {
        const hit = boxes.find((b) => b.id === id);
        if (hit) { box = hit; break; }
      }
    }
    renderDetail(box);
  }

  return { mount, onSelect, id: 'layout' };
}
