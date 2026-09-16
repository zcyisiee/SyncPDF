/* 翻译视图：逐段 源文/译文 对照 + 模型调用列表。

数据：
- snapshots/parse/selection.json     → 选中/跳过段落（源文）
- snapshots/translate/texts/*.json   → 逐段 target（最终 text_version 为准）
- events: call_started/call_finished（translate 阶段，含 prompt/stdout/stderr
  artifact 引用）、missing_ids、text_version、stage_finished
- artifacts/translate/*.md           → 完整 prompt / 译文全文
*/
'use strict';

const $ = (sel, root) => (root || document).querySelector(sel);

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

const PAGE = 120;  // 对照列表分页大小（「加载更多」）

export function createTranslateView(ctx) {
  const state = {
    selection: null,
    targets: new Map(),     // id → {text, flags:Set}
    missing: new Set(),
    shown: 0,
    calls: [],
    versions: [],
    seenSeq: new Set(),   // 已入列事件 seq（mount 重放 + onEvent 去重）
  };

  function consume(ev) {
    if (state.seenSeq.has(ev.seq)) return;
    state.seenSeq.add(ev.seq);
    if (ev.kind === 'call_finished') state.calls.push(ev);
    if (ev.kind === 'missing_ids') {
      for (const id of (ev.data && ev.data.ids) || []) state.missing.add(id);
    }
    if (ev.kind === 'text_version') state.versions.push(ev);
  }

  async function loadTargets() {
    /* 取最后一个 text_version 快照作为写回译文来源（rows[].target）。 */
    const versions = state.versions;
    if (!versions.length) return;
    const last = versions[versions.length - 1];
    const rel = (last.data && last.data.snapshot || '').replace(/^snapshots\//, '');
    if (!rel) return;
    const snap = await ctx.api.snapshot(ctx.run, rel);
    for (const row of (snap && snap.rows) || []) {
      if (row.id) state.targets.set(row.id, row);
    }
    // 相位标记：非首个版本命中的段 → merged/补译
    for (const v of versions.slice(0, -1)) {
      const vRel = (v.data && v.data.snapshot || '').replace(/^snapshots\//, '');
      if (!vRel) continue;
      const s = await ctx.api.snapshot(ctx.run, vRel);
      const phase = (v.data && v.data.phase) || '';
      for (const row of (s && s.rows) || []) {
        const cur = state.targets.get(row.id);
        if (!cur) continue;
        if (phase === 'merged' || phase === 'retry' || phase === 'retranslated') {
          cur.flags = cur.flags || new Set();
          cur.flags.add('merged');
        }
      }
    }
  }

  function renderList() {
    const pane = ctx.textPane;
    pane.replaceChildren();
    const rows = (state.selection && state.selection.selected) || [];
    if (!rows.length) {
      pane.append(el('div', 'empty', '无翻译选择快照（selection.json 缺失或未采集）。'));
      return;
    }
    const frag = document.createDocumentFragment();
    const visible = rows.slice(0, state.shown);
    for (const row of visible) {
      const target = state.targets.get(row.id) || {};
      const line = el('div', 'seg-row');
      line.dataset.eid = row.id;
      line.append(el('span', 'seg-id mono', String(row.id)));
      line.append(el('div', 'seg-src', row.source || ''));
      line.append(el('div', 'seg-tgt', target.target || '（无写回记录）'));
      const flags = [];
      if (state.missing.has(row.id)) flags.push(['retry', 'warn']);
      if (target.flags && target.flags.has('merged')) flags.push(['merged', 'ok']);
      if (target.matched === false) flags.push(['unmatched', 'bad']);
      if (!state.targets.has(row.id)) flags.push(['no-target', 'muted']);
      if (flags.length) {
        const holder = el('div', 'seg-flags');
        for (const [name, tone] of flags) holder.append(el('span', `badge ${tone}`, name));
        line.append(holder);
      }
      line.onclick = () => ctx.goto('layout', (row.page || 0) + 1, row.id);
      frag.append(line);
    }
    if (state.shown < rows.length) {
      const more = el('button', null, `加载更多（${state.shown}/${rows.length}）`);
      more.style.margin = '12px 0';
      more.onclick = () => { state.shown += PAGE; renderList(); };
      frag.append(more);
    }
    pane.append(frag);
  }

  function renderPanel() {
    const head = ctx.panelHead;
    const body = ctx.panelBody;
    head.replaceChildren(el('h3', null, '翻译证据'));
    body.replaceChildren();

    const summary = el('section');
    const sel = state.selection || {};
    const kv = el('dl', 'kv');
    const add = (k, v) => kv.append(el('dt', null, k), el('dd', null, String(v)));
    add('选中段落', (sel.selected || []).length);
    add('跳过段落', (sel.skipped || []).length);
    add('缺失补译', state.missing.size);
    add('版本数', state.versions.length);
    summary.append(kv);
    body.append(summary);

    const skipped = (sel.skipped || []);
    if (skipped.length) {
      const sec = el('section');
      sec.append(el('h4', null, `跳过（${skipped.length}）`));
      const table = el('table', 'mini');
      table.append(el('tr', null));
      const headRow = table.firstChild;
      for (const h of ['id', 'label', '原因']) headRow.append(el('th', null, h));
      for (const row of skipped.slice(0, 60)) {
        const tr = el('tr');
        tr.append(el('td', 'mono', row.id), el('td', null, row.layout_label || ''), el('td', null, row.reason || ''));
        table.append(tr);
      }
      sec.append(table);
      body.append(sec);
    }

    const callSec = el('section');
    callSec.append(el('h4', null, `模型调用（${state.calls.length}）`));
    if (!state.calls.length) callSec.append(el('div', 'empty', '无调用记录（或仅导入/离线）。'));
    for (const call of state.calls) {
      const d = call.data || {};
      const item = el('details', 'call-item');
      const summary = el('summary');
      const status = d.status === 'ok' ? 'ok' : (d.status || '?');
      summary.textContent = `#${call.seq} ${d.origin || 'call'} · ${status} · ${d.seconds != null ? `${d.seconds}s` : '–'}`;
      item.append(summary);
      const bodyEl = el('div', 'call-body');
      const kv2 = el('dl', 'kv');
      const add2 = (k, v) => { if (v != null) kv2.append(el('dt', null, k), el('dd', 'mono', String(v))); };
      add2('origin', d.origin);
      add2('status', d.status);
      add2('exit', d.returncode);
      add2('seconds', d.seconds);
      if (d.error) add2('error', d.error);
      bodyEl.append(kv2);
      for (const [label, rel] of [['prompt', d.prompt], ['stdout', d.stdout], ['stderr', d.stderr]]) {
        if (!rel) continue;
        const a = el('a', null, label);
        a.href = ctx.api.artifactUrl(ctx.run, rel);
        a.target = '_blank';
        bodyEl.append(a, document.createTextNode(' '));
      }
      item.append(bodyEl);
      callSec.append(item);
    }
    body.append(callSec);

    const verSec = el('section');
    verSec.append(el('h4', null, '文本版本'));
    for (const v of state.versions) {
      const d = v.data || {};
      const row = el('div', 'ctl');
      row.append(
        el('span', 'badge muted', d.phase || '?'),
        el('span', null, `${d.rows ?? '–'} 行`),
      );
      if (d.artifact) {
        const a = el('a', null, 'md');
        a.href = ctx.api.artifactUrl(ctx.run, d.artifact);
        a.target = '_blank';
        row.append(a);
      }
      verSec.append(row);
    }
    body.append(verSec);
  }

  async function mount() {
    /* 视图实例跨 tab 复用：重进时先清空累积态，避免事件重复入列。 */
    state.targets = new Map();
    state.missing = new Set();
    state.calls = [];
    state.versions = [];
    state.seenSeq.clear();
    state.selection = await ctx.api.snapshot(ctx.run, 'parse/selection.json');
    for (const ev of ctx.events) {
      if (ev.stage !== 'translate') continue;
      consume(ev);
    }
    await loadTargets();
    state.shown = PAGE;
    renderList();
    renderPanel();
    /* parse-only 调试归档没有 translate 阶段：行级「无写回记录」属预期，状态栏
       给中性说明，避免误读为写回失败。selection 是 parse 阶段证据，run 连
       parse 都没跑时同样按「未采集」处理。 */
    const ranParse = ctx.events.some((e) => e.stage === 'parse');
    const ranTranslate = ctx.events.some((e) => e.stage === 'translate');
    if (!state.selection) {
      ctx.setStageStatus('缺少 selection 快照', ranParse ? 'error' : 'muted');
    } else if (!ranTranslate) {
      ctx.setStageStatus('该 run 未运行 translate 阶段（如 parse-only 调试归档），段落无写回记录属预期', 'muted');
    } else {
      ctx.setStageStatus('');
    }
  }

  function onEvent(ev) {
    if (ev.stage !== 'translate' || state.seenSeq.has(ev.seq)) return;
    consume(ev);
    if (ev.kind === 'call_finished') renderPanel();
    if (ev.kind === 'missing_ids') { renderList(); renderPanel(); }
    if (ev.kind === 'text_version') {
      loadTargets().then(renderList);
      renderPanel();
    }
  }

  return { mount, onEvent, onSelect: () => {}, id: 'translate' };
}
