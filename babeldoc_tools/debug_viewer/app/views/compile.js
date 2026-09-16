/* 编译视图：最终中文 PDF + 多层框（源框/排版框/墨迹框/贴片框）+ 候选树。

数据：
- snapshots/build/typesetting_geometry.json → 逐段 src_box/layout_box/rendered_box
- artifacts/build/mono.pdf                 → 最终 PDF
- artifacts/build/latex_bbox_report.json   → 逐段决策（stamp_box/expanded_pt/…）
- events: candidate_evaluated / candidate_selected / compile_reuse /
  compile_fallback / compile_expand / cache_* / latex_* / call_finished(build)
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

/* 坐标约定：typesetting_geometry 的 src/layout/rendered_box 是 IL 坐标
   （左下原点，y 向上）；stamp_box 是 pymupdf 页面坐标（左上原点，y 向下）。
   overlay 统一左上原点 → IL 框按页高翻转。 */
const IL_LAYERS = new Set(['src_box', 'layout_box', 'rendered_box']);
const LAYERS = [
  ['src_box', '源框', 'hsl(210 80% 45%)', 'dashed'],
  ['layout_box', '排版框', 'hsl(150 70% 35%)', ''],
  ['rendered_box', '墨迹框', 'hsl(280 60% 45%)', 'dotted thin'],
  ['stamp_box', '贴片框', 'hsl(0 75% 45%)', 'thin'],
];

export function createCompileView(ctx) {
  const state = {
    geometry: null,       // typesetting_geometry snapshot
    report: null,         // latex_bbox_report.json
    decisions: new Map(), // debug_id → decision record
    candidates: [],       // candidate_evaluated events
    selected: new Map(),  // key → candidate_selected data
    reuse: new Map(),     // key → compile_reuse data
    expands: new Map(),   // key → compile_expand data
    calls: 0,
    layers: new Set(['src_box', 'layout_box', 'stamp_box']),
    byPage: new Map(),
    heights: new Map(),   // page_index → 页高（pt，供 IL→左上 翻转）
    seenSeq: new Set(),   // 已入列事件 seq（mount 重放 + onEvent 去重）
  };

  function ensureBoxes() {
    state.byPage.clear();
    const paras = (state.geometry && state.geometry.paragraphs) || [];
    for (const p of paras) {
      const pageIdx = (p.page || 1) - 1;
      const pageH = state.heights.get(pageIdx) || 0;
      const list = state.byPage.get(pageIdx) || [];
      const decision = state.decisions.get(p.id) || {};
      for (const [field, label, color, cls] of LAYERS) {
        if (!state.layers.has(field)) continue;
        const raw = field === 'stamp_box' ? decision.stamp_box : p[field];
        if (!raw) continue;
        let [x0, y0, x1, y1] = Array.isArray(raw) ? raw : [raw.x0, raw.y0, raw.x1, raw.y1];
        if (IL_LAYERS.has(field) && pageH) {
          [y0, y1] = [pageH - y1, pageH - y0];
        }
        list.push({
          id: p.id,
          box: { x0, y0, x1, y1 },
          label: `${label} ${p.id}`,
          kind: field,
          cls,
          color,
          layer: field,
          preview: (p.text || '').slice(0, 80),
          data: p,
        });
      }
      state.byPage.set(pageIdx, list);
    }
  }

  function monoPdf(manifest) {
    const artifacts = Object.keys((manifest && manifest.artifacts) || {});
    for (const name of ['artifacts/build/mono.pdf', 'artifacts/build/dual.pdf']) {
      if (artifacts.includes(name)) return name.replace(/^artifacts\//, '');
    }
    return null;
  }

  /* ---------------------------------------------------------- badges */
  function badgesFor(pid) {
    const out = [];
    const decision = state.decisions.get(pid) || {};
    const sel = state.selected.get(pid);
    const cands = state.candidates.filter((e) => (e.data || {}).paragraph_id === pid);
    const chosen = sel && cands.find((e) => (e.data || {}).id === sel.id);
    const chosenData = chosen ? chosen.data || {} : {};
    if (state.reuse.has(pid)) out.push(['缓存复用', 'ok']);
    if (chosenData) {
      if (chosenData.priority === 0) out.push(['初始候选通过', 'ok']);
      if (chosenData.font_size != null && chosenData.font_size_initial != null
          && chosenData.font_size < chosenData.font_size_initial - 0.01) {
        out.push(['调整字号', 'warn']);
      }
      if (chosenData.priority > 0 && chosenData.font_size === chosenData.font_size_initial) {
        out.push(['调整行距', 'warn']);
      }
    }
    if ((decision.expanded_pt || 0) > 0 || state.expands.has(pid)) out.push(['调整框', 'warn']);
    if (decision.reason === 'reverted-link-check') out.push(['回滚', 'bad']);
    else if (decision.reason && decision.reason !== 'applied') out.push(['原生回退', 'muted']);
    if (decision.reason === 'applied' && !out.length) out.push(['初始候选通过', 'ok']);
    return out;
  }

  /* ---------------------------------------------------------- panel */
  function renderPanel() {
    const head = ctx.panelHead;
    const body = ctx.panelBody;
    head.replaceChildren(el('h3', null, '编译证据'));
    body.replaceChildren();

    const sumSec = el('section');
    const kv = el('dl', 'kv');
    const add = (k, v) => kv.append(el('dt', null, k), el('dd', null, String(v)));
    add('候选数', state.candidates.length);
    add('真实编译调用', state.calls);
    add('选中段落', state.selected.size);
    add('缓存/去重复用', state.reuse.size);
    const compile = (state.report && state.report.compile) || {};
    if (compile.batches != null) {
      const rounds = Array.isArray(compile.rounds) ? compile.rounds.length : (compile.rounds ?? '–');
      add('批次/轮次', `${compile.batches}/${rounds}`);
    }
    sumSec.append(kv);
    body.append(sumSec);

    const layerSec = el('section');
    layerSec.append(el('h4', null, '图层'));
    for (const [field, label, color] of LAYERS) {
      const row = el('div', 'legend-item');
      const input = document.createElement('input');
      input.type = 'checkbox';
      input.checked = state.layers.has(field);
      const sw = el('span', 'legend-swatch');
      sw.style.background = color;
      row.append(input, sw, el('span', null, label));
      row.onclick = () => {
        if (state.layers.has(field)) state.layers.delete(field); else state.layers.add(field);
        ensureBoxes(); ctx.pager.refresh(); renderPanel();
      };
      layerSec.append(row);
    }
    body.append(layerSec);

    const detail = el('section');
    detail.id = 'compile-detail';
    body.append(detail);
    renderDetail(null);
  }

  function renderDetail(pid) {
    const sec = $('#compile-detail', ctx.panelBody);
    if (!sec) return;
    sec.replaceChildren(el('h4', null, '选中段落'));
    if (!pid) {
      sec.append(el('div', 'empty', '点击页面框查看候选树与参数。'));
      return;
    }
    const p = ((state.geometry && state.geometry.paragraphs) || []).find((x) => x.id === pid);
    const decision = state.decisions.get(pid) || {};
    const badges = badgesFor(pid);
    if (badges.length) {
      const holder = el('div', 'badges');
      for (const [name, tone] of badges) holder.append(el('span', `badge ${tone}`, name));
      sec.append(holder);
    }
    const kv = el('dl', 'kv');
    const add = (k, v) => { if (v != null) kv.append(el('dt', null, k), el('dd', 'mono', String(v))); };
    add('id', pid);
    if (p) {
      add('label', p.layout_label);
      add('page', p.page);
      add('src 字号', p.src_font_size);
      add('最小/众数字号', `${p.min_font_size ?? '–'}/${p.mode_font_size ?? '–'}`);
      add('行数', p.n_lines);
    }
    add('scale', decision.font_scale);
    add('lead', decision.lead);
    add('attempts', decision.attempts);
    add('expanded_pt', decision.expanded_pt);
    add('判定', decision.reason);
    sec.append(kv);

    // 候选树：按 round → renderer 分组，优先级排序。
    const cands = state.candidates
      .filter((e) => (e.data || {}).paragraph_id === pid)
      .sort((a, b) => ((a.data.round || 0) - (b.data.round || 0)) || ((a.data.priority || 0) - (b.data.priority || 0)));
    const treeSec = el('section');
    treeSec.append(el('h4', null, `候选（${cands.length}）`));
    if (!cands.length) {
      treeSec.append(el('div', 'empty', state.reuse.has(pid) ? '缓存复用，本次无编译候选。' : '无候选记录。'));
    }
    const table = el('table', 'mini');
    const hr = el('tr');
    for (const h of ['#', '轮', '渲染器', '字号', '行距', '状态', '原因']) hr.append(el('th', null, h));
    table.append(hr);
    for (const ev of cands) {
      const d = ev.data || {};
      const tr = el('tr');
      if (state.selected.get(pid) && state.selected.get(pid).id === d.id) tr.style.background = '#eef2ff';
      tr.append(
        el('td', 'mono', String(d.priority ?? '–')),
        el('td', null, String(d.round ?? '–')),
        el('td', null, d.renderer || ''),
        el('td', 'mono', d.font_size != null ? String(d.font_size) : '–'),
        el('td', 'mono', d.lead != null ? String(d.lead) : '–'),
        el('td', null, d.status || ''),
        el('td', null, (d.reason || '').slice(0, 40)),
      );
      if (d.artifacts) {
        const td = el('td');
        for (const [name, rel] of Object.entries(d.artifacts)) {
          if (!rel) continue;
          const a = el('a', null, name);
          a.href = ctx.api.artifactUrl(ctx.run, rel);
          a.target = '_blank';
          td.append(a, document.createTextNode(' '));
        }
        tr.append(td);
      }
      table.append(tr);
    }
    treeSec.append(table);
    sec.append(treeSec);

    const sel = state.selected.get(pid);
    if (sel && sel.pdf_page_index != null && sel.batch_id) {
      const note = el('div', 'empty');
      note.textContent = `批编译证据：batch ${sel.batch_id} · 页索引 ${sel.pdf_page_index}`;
      sec.append(note);
    }
  }

  /* ---------------------------------------------------------- mount */
  async function mount() {
    /* 视图实例跨 tab 复用：重进时先清空累积态，避免事件重复入列。 */
    state.candidates = [];
    state.selected.clear();
    state.reuse.clear();
    state.expands.clear();
    state.calls = 0;
    state.seenSeq.clear();
    const [geometry, report] = await Promise.all([
      ctx.api.snapshot(ctx.run, 'build/typesetting_geometry.json'),
      ctx.api.artifactJson(ctx.run, 'artifacts/build/latex_bbox_report.json'),
    ]);
    state.geometry = geometry;
    state.report = report;
    for (const d of (report && report.decisions) || []) {
      if (d.debug_id) state.decisions.set(d.debug_id, d);
    }
    for (const ev of ctx.events) {
      if (ev.stage !== 'build') continue;
      consume(ev);
    }
    const paras = (geometry && geometry.paragraphs) || [];
    const pageInfo = (geometry && geometry.page_info) || [];
    const maxPage = Math.max(
      pageInfo.length,
      paras.reduce((m, p) => Math.max(m, p.page || 0), 0),
      1,
    );
    const frames = await ctx.api.snapshot(ctx.run, 'parse/page-frames.json');
    const pages = [];
    for (let i = 0; i < maxPage; i += 1) {
      const frame = frames && frames.frames && frames.frames[i];
      const info = pageInfo[i] || {};
      const crop = info.cropbox;
      const height = (frame && frame.height) || (crop ? crop[3] - crop[1] : 792);
      state.heights.set(i, height);
      pages.push({
        index: i,
        width: (frame && frame.width) || (crop ? crop[2] - crop[0] : 612),
        height,
      });
    }
    ensureBoxes();
    ctx.pager.setDocument(ctx.run, monoPdf(ctx.manifest), pages);
    ctx.pager.setOverlayProvider((idx) => state.byPage.get(idx) || []);
    renderPanel();
    /* parse-only 调试归档没有 build 阶段：按「未采集」中性说明，只有跑过
       build 却缺快照才算采集缺失。 */
    if (geometry) {
      ctx.setStageStatus('');
    } else if (ctx.events.some((e) => e.stage === 'build')) {
      ctx.setStageStatus('缺少 typesetting_geometry 快照', 'error');
    } else {
      ctx.setStageStatus('该 run 未运行 build 阶段（如 parse-only 调试归档），无编译证据', 'muted');
    }
  }

  function consume(ev) {
    if (state.seenSeq.has(ev.seq)) return;
    state.seenSeq.add(ev.seq);
    const d = ev.data || {};
    if (ev.kind === 'candidate_evaluated') state.candidates.push(ev);
    if (ev.kind === 'candidate_selected' && d.key) state.selected.set(d.key, d);
    if (ev.kind === 'compile_reuse' && d.key) state.reuse.set(d.key, d);
    if (ev.kind === 'compile_expand' && d.key) state.expands.set(d.key, d);
    if (ev.kind === 'call_finished') state.calls += 1;
  }

  function onEvent(ev) {
    if (ev.stage !== 'build') return;
    consume(ev);
    renderPanel();
    if (ctx.selectedEntity) renderDetail(ctx.selectedEntity);
  }

  function onSelect(id) {
    renderDetail(id);
  }

  return { mount, onEvent, onSelect, id: 'compile' };
}
