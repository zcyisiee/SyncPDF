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
    targets: new Map(),     // id → 模型返回行（raw/merged）
    writeback: new Map(),   // id → 最终 canonical 写回文本（apply 阶段）
    unmatched: [],          // 未按 id 对齐的模型返回
    missing: new Set(),
    shown: 0,
    calls: [],
    versions: [],
    applyValidations: [],   // apply_validation 事件（真实链路的写回证据）
    seenSeq: new Set(),   // 已入列事件 seq（mount 重放 + onEvent 去重）
  };

  function consume(ev) {
    if (state.seenSeq.has(ev.seq)) return;
    state.seenSeq.add(ev.seq);
    const d = ev.data || {};
    if (ev.kind === 'call_finished') state.calls.push(ev);
    if (ev.kind === 'missing_ids') {
      for (const id of d.ids || []) state.missing.add(id);
    }
    if (ev.kind === 'text_version') state.versions.push(ev);
    if (ev.kind === 'canonical_writeback' && d.id) {
      state.writeback.set(d.id, d.target);
    }
    if (ev.kind === 'apply_validation') state.applyValidations.push(ev);
  }

  async function collectUnmatched(snap, phase) {
    for (const item of (snap && snap.unmatched) || []) {
      if (item && item.text) state.unmatched.push({ ...item, phase });
    }
  }

  async function loadTargets() {
    /* 取最后一个 text_version 快照作为模型返回来源（rows[].target）。 */
    const versions = state.versions;
    if (!versions.length) return;
    for (const v of versions) {
      const rel = ((v.data && v.data.snapshot) || '').replace(/^snapshots\//, '');
      if (!rel) continue;
      const snap = await ctx.api.snapshot(ctx.run, rel);
      await collectUnmatched(snap, (v.data && v.data.phase) || '');
      const isLast = v === versions[versions.length - 1];
      if (!isLast) {
        // 相位标记：非首个版本命中的段 → merged/补译
        const phase = (v.data && v.data.phase) || '';
        for (const row of (snap && snap.rows) || []) {
          const cur = state.targets.get(row.id);
          if (!cur) continue;
          if (phase === 'merged' || phase === 'retry' || phase === 'retranslated') {
            cur.flags = cur.flags || new Set();
            cur.flags.add('merged');
          }
        }
        continue;
      }
      for (const row of (snap && snap.rows) || []) {
        if (row.id) state.targets.set(row.id, row);
      }
    }
  }

  /** 最终 canonical 写回：优先 apply_validation 快照（真实链路），其次
      canonical_writeback 事件，最后 translated.jsonl 产物。 */
  async function loadWriteback() {
    const last = state.applyValidations[state.applyValidations.length - 1];
    if (last) {
      const rel = ((last.data && last.data.snapshot) || '').replace(/^snapshots\//, '');
      if (rel) {
        try {
          const snap = await ctx.api.snapshot(ctx.run, rel);
          for (const entry of (snap && snap.entries) || []) {
            if (entry && entry.id) state.writeback.set(entry.id, entry.target);
          }
        } catch (err) { /* 快照不可用：保留事件/产物来源 */ }
      }
    }
    const artifacts = Object.keys((ctx.manifest && ctx.manifest.artifacts) || {});
    const jsonl = 'artifacts/apply/translated.jsonl';
    if (!artifacts.includes(jsonl)) return;
    try {
      const text = await ctx.api.artifactText(ctx.run, jsonl);
      for (const line of (text || '').split('\n')) {
        const trimmed = line.trim();
        if (!trimmed) continue;
        let row = null;
        try { row = JSON.parse(trimmed); } catch (err) { continue; }
        if (row && row.id && row.target != null) state.writeback.set(row.id, row.target);
      }
    } catch (err) { /* 产物不可用：不阻塞视图 */ }
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
      const canonical = state.writeback.get(row.id);
      const line = el('div', 'seg-row');
      line.dataset.eid = row.id;
      line.append(el('span', 'seg-id mono', String(row.id)));
      line.append(el('div', 'seg-src', row.source || ''));
      line.append(el('div', 'seg-tgt', canonical != null ? canonical : (target.target || '（无写回记录）')));
      const flags = [];
      if (canonical != null) flags.push(['写回', 'ok']);
      else if (target.target) flags.push(['raw', 'muted']);
      if (state.missing.has(row.id)) flags.push(['retry', 'warn']);
      if (target.flags && target.flags.has('merged')) flags.push(['merged', 'ok']);
      if (target.matched === false) flags.push(['unmatched', 'bad']);
      if (!state.targets.has(row.id) && canonical == null) flags.push(['no-target', 'muted']);
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
    /* 未按 id 对齐的模型返回：保留在未匹配区域，不静默丢弃。 */
    const unmatched = state.unmatched.filter((u) => u && u.text);
    if (unmatched.length) {
      const sec = el('section', 'unmatched-region');
      sec.id = 'unmatched';
      sec.append(el('h4', null, `未匹配返回（${unmatched.length}）`));
      for (const item of unmatched) {
        sec.append(el('div', 'empty', item.reason || 'no_paragraph_id'));
        sec.append(el('pre', 'block', String(item.text)));
      }
      frag.append(sec);
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
    state.writeback = new Map();
    state.unmatched = [];
    state.missing = new Set();
    state.calls = [];
    state.versions = [];
    state.applyValidations = [];
    state.seenSeq.clear();
    state.selection = await ctx.api.snapshot(ctx.run, 'parse/selection.json');
    for (const ev of ctx.events) {
      if (ev.stage !== 'translate' && ev.stage !== 'apply') continue;
      consume(ev);
    }
    await loadWriteback();
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
    if ((ev.stage !== 'translate' && ev.stage !== 'apply') || state.seenSeq.has(ev.seq)) return;
    consume(ev);
    if (ev.kind === 'call_finished') renderPanel();
    if (ev.kind === 'missing_ids') { renderList(); renderPanel(); }
    if (ev.kind === 'text_version') {
      loadTargets().then(renderList);
      renderPanel();
    }
    if (ev.kind === 'apply_validation' || ev.kind === 'canonical_writeback') {
      loadWriteback().then(renderList);
    }
  }

  return { mount, onEvent, onSelect: () => {}, id: 'translate' };
}
