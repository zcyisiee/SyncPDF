/* 事件视图：全阶段时间线，按 stage/kind 过滤，增量追加，点击跳转关联实体。

数据：ctx.events（main.js 增量轮询累积）。列表渲染在主区 textPane（全宽），
过滤控件在工作台面板。 */
'use strict';

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

function brief(data) {
  if (data == null) return '';
  try {
    const text = JSON.stringify(data);
    return text.length > 160 ? `${text.slice(0, 160)}…` : text;
  } catch (err) {
    return String(data).slice(0, 160);
  }
}

/* 从事件 data 猜出可跳转的 {view, page, entity}。 */
function linkTarget(ev) {
  const d = ev.data || {};
  if (!d || typeof d !== 'object') return null;
  const entity = d.id || d.key || d.paragraph_id || d.entity_id;
  const page = d.page_index != null ? d.page_index + 1
    : d.pdf_page_index != null ? d.pdf_page_index + 1
    : d.page;
  if (ev.stage === 'parse' || ev.stage === 'apply') {
    return entity || page ? { view: 'layout', page, entity } : null;
  }
  if (ev.stage === 'translate') {
    return entity ? { view: 'layout', page, entity } : null;
  }
  if (ev.stage === 'build') {
    return entity || page ? { view: 'compile', page, entity: d.key || entity } : null;
  }
  if (ev.stage === 'check') {
    return entity || page ? { view: 'checks', page, entity } : null;
  }
  return null;
}

export function createEventsView(ctx) {
  const state = {
    stage: 'all',
    kindFilter: '',
    rendered: 0,       // ctx.events 已渲染到的下标
    autoScroll: true,
    list: null,        // .ev-list 容器（在 textPane 内）
  };

  function visible(ev) {
    if (state.stage !== 'all' && ev.stage !== state.stage) return false;
    if (state.kindFilter && !(ev.kind || '').includes(state.kindFilter)) return false;
    return true;
  }

  function rowEl(ev) {
    const row = el('div', 'ev-row');
    row.append(
      el('span', 'ev-seq', `#${ev.seq}`),
      el('span', 'ev-stage', ev.stage || ''),
      el('span', 'ev-kind', ev.kind || ''),
      el('span', 'ev-data', brief(ev.data)),
    );
    row.title = `${ev.at || ''} ${ev.stage || ''}/${ev.kind || ''}\n${brief(ev.data)}`;
    const target = linkTarget(ev);
    if (target) {
      row.style.cursor = 'pointer';
      row.onclick = () => ctx.goto(target.view, target.page, target.entity);
    }
    return row;
  }

  function renderPanel() {
    ctx.panelHead.replaceChildren(el('h3', null, `事件流（${ctx.events.length}）`));
    const body = ctx.panelBody;
    body.replaceChildren();
    const ctl = el('section');
    ctl.append(el('h4', null, '过滤'));
    const row = el('div', 'ctl');
    const stageSel = document.createElement('select');
    const stages = ['all', ...new Set(ctx.events.map((e) => e.stage).filter(Boolean))];
    for (const s of stages) {
      const opt = document.createElement('option');
      opt.value = s;
      opt.textContent = s === 'all' ? '全部阶段' : s;
      if (s === state.stage) opt.selected = true;
      stageSel.append(opt);
    }
    stageSel.onchange = () => { state.stage = stageSel.value; renderList(false); };
    const kindInput = document.createElement('input');
    kindInput.placeholder = 'kind 包含…';
    kindInput.value = state.kindFilter;
    kindInput.oninput = () => { state.kindFilter = kindInput.value.trim(); renderList(false); };
    row.append(stageSel, kindInput);
    const auto = el('label', 'ctl');
    const autoBox = document.createElement('input');
    autoBox.type = 'checkbox';
    autoBox.checked = state.autoScroll;
    autoBox.onchange = () => { state.autoScroll = autoBox.checked; };
    auto.append(autoBox, el('span', null, '跟随最新'));
    ctl.append(row, auto);
    body.append(ctl);
  }

  function renderList(append) {
    const pane = ctx.textPane;
    if (!append || !state.list || !pane.contains(state.list)) {
      pane.replaceChildren();
      state.list = el('div', 'ev-list');
      pane.append(state.list);
      state.rendered = 0;
    }
    const frag = document.createDocumentFragment();
    const total = ctx.events.length;
    for (let i = state.rendered; i < total; i += 1) {
      const ev = ctx.events[i];
      if (!visible(ev)) continue;
      frag.append(rowEl(ev));
    }
    state.rendered = total;
    state.list.append(frag);
    if (state.autoScroll && state.list.lastElementChild) {
      state.list.lastElementChild.scrollIntoView({ block: 'end' });
    }
  }

  function mount() {
    ctx.pager.clear();
    renderPanel();
    renderList(false);
    ctx.setStageStatus('');
  }

  function onEvent() {
    ctx.panelHead.querySelector('h3').textContent = `事件流（${ctx.events.length}）`;
    renderList(true);
  }

  return { mount, onEvent, onSelect: () => {}, id: 'events' };
}
