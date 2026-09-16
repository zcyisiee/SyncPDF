/* bdt debug 工作台入口：运行选择、视图切换、事件增量轮询、深链、工作台宽度。

只读：不发起任何写请求。视图：
- layout    识别（源 PDF + layout/段落/字符框）
- translate 翻译（逐段对照 + 调用列表；主区切换为文本面板）
- compile   编译（最终 PDF + 多层几何 + 候选树）
- checks    检查（verdict/lint/link 问题定位）
- events    事件时间线
*/
'use strict';

import { api } from './api.js';
import { createPager } from './pager.js';
import { createLayoutView } from './views/layout.js';
import { createTranslateView } from './views/translate.js';
import { createCompileView } from './views/compile.js';
import { createCheckView } from './views/check.js';
import { createEventsView } from './views/events.js';

const $ = (sel) => document.querySelector(sel);

const POLL_MS = 1000;
const HEARTBEAT_MS = 15000;
const MANIFEST_REFRESH_MS = 3000;

const dom = {
  runSelect: $('#runSelect'),
  runStatus: $('#runStatus'),
  issueCount: $('#issueCount'),
  pageInput: $('#pageInput'),
  pageTotal: $('#pageTotal'),
  prevPage: $('#prevPage'),
  nextPage: $('#nextPage'),
  netState: $('#netState'),
  panelToggle: $('#panelToggle'),
  stage: $('#stage'),
  stageStatus: $('#stageStatus'),
  pages: $('#pages'),
  textPane: $('#textPane'),
  splitter: $('#splitter'),
  workbench: $('#workbench'),
  panelHead: $('#panelHead'),
  panelBody: $('#panelBody'),
  hoverChip: $('#hoverChip'),
  hitList: $('#hitList'),
};

const state = {
  runs: [],
  run: null,
  manifest: null,
  events: [],
  lastSeq: 0,
  activeView: 'layout',
  currentPage: 0,
  selectedEntity: null,
  pendingDeepLink: null,   // {view, page, entity}
  mounting: false,
  mountSerial: 0,
};

/* ---------------------------------------------------------------- ctx */
const ctx = {
  api,
  pager: null,
  get events() { return state.events; },
  get run() { return state.run; },
  get manifest() { return state.manifest; },
  get activeView() { return state.activeView; },
  get currentPage() { return state.currentPage; },
  get selectedEntity() { return state.selectedEntity; },
  panelHead: dom.panelHead,
  panelBody: dom.panelBody,
  textPane: dom.textPane,
  setStageStatus,
  setIssueCount,
  select,
  goto,
};

function setStageStatus(message, tone) {
  if (!message) {
    dom.stageStatus.hidden = true;
    dom.stageStatus.textContent = '';
    return;
  }
  dom.stageStatus.hidden = false;
  dom.stageStatus.textContent = message;
  dom.stageStatus.className = `stage-status${tone === 'error' ? ' error' : ''}`;
}

function setIssueCount(n) {
  dom.issueCount.textContent = n ? `${n} 个问题` : '';
}

function select(id) {
  state.selectedEntity = id || null;
  if (ctx.pager) ctx.pager.selectById(id);
  const view = views[state.activeView];
  if (view && view.onSelect) view.onSelect(id);
}

async function goto(view, page, entity) {
  state.pendingDeepLink = { view, page, entity };
  if (view && view !== state.activeView) {
    await activate(view);
  } else {
    applyDeepLink();
  }
}

function applyDeepLink() {
  const link = state.pendingDeepLink;
  state.pendingDeepLink = null;
  if (!link) return;
  if (link.page && ctx.pager) ctx.pager.scrollToPage(link.page - 1);
  if (link.entity) {
    state.selectedEntity = link.entity;
    if (ctx.pager) ctx.pager.selectById(link.entity);
    const view = views[state.activeView];
    if (view && view.onSelect) view.onSelect(link.entity);
  }
}

/* ---------------------------------------------------------------- views */
const views = {};
let pager;

const PAGE_VIEWS = new Set(['layout', 'compile', 'checks']);
const TEXT_VIEWS = new Set(['translate', 'events']);

function viewOf(name) {
  if (!views[name]) {
    if (name === 'layout') views[name] = createLayoutView(ctx);
    else if (name === 'translate') views[name] = createTranslateView(ctx);
    else if (name === 'compile') views[name] = createCompileView(ctx);
    else if (name === 'checks') views[name] = createCheckView(ctx);
    else if (name === 'events') views[name] = createEventsView(ctx);
    else return null;
  }
  return views[name];
}

async function activate(name) {
  if (!viewOf(name)) return;
  state.activeView = name;
  document.querySelectorAll('#viewTabs .tab').forEach((tab) => {
    tab.classList.toggle('active', tab.dataset.view === name);
  });
  dom.pages.hidden = !PAGE_VIEWS.has(name);
  dom.textPane.hidden = !TEXT_VIEWS.has(name);
  state.mounting = true;
  const serial = ++state.mountSerial;
  try {
    await views[name].mount();
    if (serial === state.mountSerial) applyDeepLink();
  } catch (err) {
    if (serial === state.mountSerial) {
      setStageStatus(`视图加载失败：${err.message || err}`, 'error');
    }
  } finally {
    if (serial === state.mountSerial) state.mounting = false;
  }
  updatePageControls();
  updateUrl();
}

/* ---------------------------------------------------------------- run */
/* 深链参数：query 与 hash 都读（bdt 输出的 URL 用 ``?token=…#run=…``）。 */
function linkParams() {
  const query = new URLSearchParams(location.search);
  const hash = new URLSearchParams(location.hash.replace(/^#/, ''));
  return (key) => hash.get(key) ?? query.get(key);
}

function updateUrl() {
  const url = new URL(location.href);
  const hash = new URLSearchParams(url.hash.replace(/^#/, ''));
  hash.set('run', state.run || '');
  hash.set('view', state.activeView);
  hash.set('page', String(state.currentPage + 1));
  if (state.selectedEntity) hash.set('entity', state.selectedEntity);
  else hash.delete('entity');
  url.hash = hash.toString();
  history.replaceState(null, '', url);
}

async function loadRuns(selectRun) {
  const payload = await api.runs();
  state.runs = payload.runs || [];
  dom.runSelect.replaceChildren();
  if (!state.runs.length) {
    const opt = document.createElement('option');
    opt.textContent = '（无 debug run）';
    dom.runSelect.append(opt);
    setStageStatus('该 workdir 下没有 debug 运行归档。用 bdt run/build --debug 产生。', 'error');
    return false;
  }
  for (const run of state.runs) {
    const opt = document.createElement('option');
    opt.value = run.run_id;
    const stageSummary = Object.entries(run.stages || {})
      .map(([k, v]) => `${k}:${v || '?'}`).join(' ');
    opt.textContent = `${run.run_id} · ${run.status || '?'}${stageSummary ? ` · ${stageSummary}` : ''}`;
    dom.runSelect.append(opt);
  }
  const wanted = selectRun
    || linkParams()('run')
    || state.runs[0].run_id;
  const found = state.runs.find((r) => r.run_id === wanted) || state.runs[0];
  state.run = found.run_id;
  dom.runSelect.value = state.run;
  return true;
}

async function loadManifest() {
  try {
    const payload = await api.manifest(state.run);
    state.manifest = payload.manifest || null;
  } catch (err) {
    state.manifest = null;
  }
  const status = (state.manifest && state.manifest.status) || 'incomplete';
  dom.runStatus.textContent = status;
  dom.runStatus.dataset.state = status;
  return status;
}

async function loadEvents() {
  const payload = await api.events(state.run, 0);
  state.events = payload.events || [];
  state.lastSeq = payload.last_seq || 0;
  dispatchEvents(state.events);
}

function dispatchEvents(newEvents) {
  for (const view of Object.values(views)) {
    if (!view || !view.onEvent) continue;
    for (const ev of newEvents) {
      try { view.onEvent(ev); } catch (err) { /* 单视图异常不阻断 */ }
    }
  }
}

async function poll() {
  if (!state.run) return;
  try {
    const payload = await api.events(state.run, state.lastSeq);
    const fresh = payload.events || [];
    state.lastSeq = payload.last_seq || state.lastSeq;
    dom.netState.dataset.state = 'ok';
    if (fresh.length) {
      state.events.push(...fresh);
      dispatchEvents(fresh);
    }
  } catch (err) {
    dom.netState.dataset.state = 'down';
  }
}

async function pollManifest() {
  if (!state.run) return;
  try {
    const payload = await api.runs();
    const runs = payload.runs || [];
    if (runs.length !== state.runs.length
        || runs.some((r, i) => r.run_id !== (state.runs[i] || {}).run_id)) {
      /* 新 run 出现后补进下拉（不重选当前 run）。 */
      state.runs = runs;
      const cur = state.run;
      dom.runSelect.replaceChildren();
      for (const run of runs) {
        const opt = document.createElement('option');
        opt.value = run.run_id;
        const stageSummary = Object.entries(run.stages || {})
          .map(([k, v]) => `${k}:${v || '?'}`).join(' ');
        opt.textContent = `${run.run_id} · ${run.status || '?'}${stageSummary ? ` · ${stageSummary}` : ''}`;
        dom.runSelect.append(opt);
      }
      dom.runSelect.value = cur;
    }
  } catch (err) { /* 服务不可用由事件轮询报错 */ }
  const status = await loadManifest();
  if (status !== 'running') return;   // 完成后不再轮 manifest
}

/* ---------------------------------------------------------------- pager */
function updatePageControls() {
  const total = pager ? pager.pageCount : 0;
  dom.pageTotal.textContent = `/ ${total || '–'}`;
  const show = PAGE_VIEWS.has(state.activeView);
  for (const node of [dom.pageInput, dom.prevPage, dom.nextPage]) {
    node.disabled = !show || !total;
  }
  if (show && total) {
    dom.pageInput.max = String(total);
    dom.pageInput.value = String(state.currentPage + 1);
  }
}

function onCurrentPage(idx) {
  state.currentPage = idx;
  if (document.activeElement !== dom.pageInput) {
    dom.pageInput.value = String(idx + 1);
  }
  updateUrl();
}

dom.prevPage.onclick = () => pager && pager.scrollToPage(Math.max(0, state.currentPage - 1));
dom.nextPage.onclick = () => pager && pager.scrollToPage(
  Math.min(Math.max(0, pager.pageCount - 1), state.currentPage + 1)
);
dom.pageInput.onchange = () => {
  const n = Number(dom.pageInput.value);
  if (pager && n >= 1 && n <= pager.pageCount) pager.scrollToPage(n - 1);
};

dom.runSelect.onchange = () => switchRun(dom.runSelect.value);

/* hash 深链：同一文档内 hash 变化不触发 reload，需手动响应。 */
window.addEventListener('hashchange', () => {
  const get = linkParams();
  const run = get('run');
  const view = get('view');
  if (run && run !== state.run && state.runs.some((r) => r.run_id === run)) {
    switchRun(run).then(() => {
      if (view && view !== state.activeView) activate(view);
    });
  } else if (view && view !== state.activeView && viewOf(view)) {
    activate(view);
  }
});

async function switchRun(runId) {
  if (!runId || runId === state.run) return;
  state.run = runId;
  state.events = [];
  state.lastSeq = 0;
  state.selectedEntity = null;
  for (const key of Object.keys(views)) delete views[key];
  setIssueCount(0);
  await loadManifest();
  await loadEvents();
  await activate(state.activeView);
}

/* ---------------------------------------------------------------- chrome */
document.querySelectorAll('#viewTabs .tab').forEach((tab) => {
  tab.onclick = () => activate(tab.dataset.view);
});

dom.panelToggle.onclick = () => {
  const collapsed = dom.workbench.classList.toggle('collapsed');
  dom.splitter.classList.toggle('hidden', collapsed);
  dom.panelToggle.setAttribute('aria-expanded', String(!collapsed));
};

/* 工作台宽度拖拽 */
(() => {
  let dragging = false;
  dom.splitter.addEventListener('mousedown', (ev) => {
    dragging = true;
    ev.preventDefault();
    document.body.style.userSelect = 'none';
  });
  window.addEventListener('mousemove', (ev) => {
    if (!dragging) return;
    const w = window.innerWidth - ev.clientX;
    const clamped = Math.min(Math.max(240, w), window.innerWidth * 0.7);
    document.documentElement.style.setProperty('--workbench-w', `${clamped}px`);
  });
  window.addEventListener('mouseup', () => {
    dragging = false;
    document.body.style.userSelect = '';
  });
})();

/* ---------------------------------------------------------------- boot */
async function boot() {
  pager = createPager({
    host: dom.pages,
    scroller: dom.stage,
    chip: dom.hoverChip,
    hitlist: dom.hitList,
    renderUrl: api.renderUrl,
    onSelect: (id, box, pageIdx) => {
      state.selectedEntity = id;
      const view = views[state.activeView];
      if (view && view.onSelect) view.onSelect(id, box, pageIdx);
    },
    onCurrent: onCurrentPage,
  });
  ctx.pager = pager;

  const get = linkParams();
  const wantedView = get('view');
  if (wantedView && viewOf(wantedView)) {
    state.activeView = wantedView;
    state.pendingDeepLink = {
      view: wantedView,
      page: Number(get('page')) || null,
      entity: get('entity') || null,
    };
  }

  if (!(await loadRuns())) return;
  await loadManifest();
  await loadEvents();
  await activate(state.activeView);

  setInterval(poll, POLL_MS);
  setInterval(pollManifest, MANIFEST_REFRESH_MS);
  setInterval(() => { api.heartbeat().catch(() => {}); }, HEARTBEAT_MS);
}

boot().catch((err) => {
  setStageStatus(`初始化失败：${err.message || err}`, 'error');
});
