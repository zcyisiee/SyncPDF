/* PP-DocLayoutV3 bbox viewer  (multi-document)
   ------------------------------------------------------------------
   Reads data/index.json for the list of processed PDFs, shows a picker when
   there is more than one, and renders the selected document: each page as an
   image with the detected bboxes painted on top as absolutely-positioned
   elements. Overlays are built lazily and torn down once their page leaves the
   viewport, so a 50-page document keeps only a couple of overlays in the DOM.

   Styling knobs that must stay inherited (never set per element, or the :hover
   rule loses the specificity fight): --pad, --fill-a, --hover-fill-a,
   --line-a, --line-w. They live on :root; each box only carries --c-rgb.
   ------------------------------------------------------------------ */

'use strict';

const BASE_WIDTH = 900;          // page width in px at 100% zoom
const LARGE_AREA_RATIO = 0.6;    // "整页大框" area threshold

const state = {
  docs: [],
  docId: null,
  base: 'data/',
  zoom: 1,
  fit: true,
  threshold: 0.5,
  showOrder: false,
  showLabels: false,
  dimOthers: true,
  hideFilled: false,
  hidden: new Set(),
  current: 0,
  total: 0,
};

let doc = null;                  // current layout.json payload
let pageViews = [];
let byIndex = new Map();
let liveObserver = null;
let currentObserver = null;

const $ = (id) => document.getElementById(id);
const scroller = $('scroller');
const pagesEl = $('pages');
const statusEl = $('status');
const chip = $('hoverChip');

/* ------------------------------------------------------------------ utils */
function debounceRAF(fn) {
  let queued = false;
  return () => {
    if (queued) return;
    queued = true;
    requestAnimationFrame(() => { queued = false; fn(); });
  };
}

function hexToRgb(hex) {
  const h = String(hex).replace('#', '');
  const full = h.length === 3 ? h.split('').map((c) => c + c).join('') : h;
  const n = parseInt(full, 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

const colorOf = (label) => (doc && doc.palette && doc.palette[label]) || '#111827';

/* Official Chinese meaning (``content`` = 目录, not 正文内容). The English label alone is
   misleading, so the legend and the hover chip always show both. */
const zhOf = (label) => (doc && doc.labels_zh && doc.labels_zh[label]) || '';

function isVisible(box, page) {
  if (state.hidden.has(box.label)) return false;
  if (box.score < state.threshold) return false;
  if (state.hideFilled) {
    const area = (box.x1 - box.x0) * (box.y1 - box.y0);
    if (area / (page.width * page.height) > LARGE_AREA_RATIO) return false;
  }
  return true;
}

function setStatus(kind, html) {
  statusEl.hidden = kind === 'none';
  statusEl.className = kind === 'error' ? 'error' : 'loading';
  if (html !== undefined) statusEl.innerHTML = html;
}

/* ------------------------------------------------------------------ document picker */
function renderDocTabs() {
  const tabs = $('docTabs');
  const total = state.docs.length;
  const multi = state.docs.some((d) => d.id !== '') && total > 1;
  tabs.hidden = !multi;
  if (!multi) {
    tabs.replaceChildren();
    return;
  }
  const frag = document.createDocumentFragment();
  for (const d of state.docs) {
    const btn = document.createElement('button');
    btn.className = 'doc-tab' + (d.id === state.docId ? ' active' : '');
    btn.setAttribute('role', 'tab');
    btn.setAttribute('aria-selected', String(d.id === state.docId));
    btn.title = `${d.name} — ${d.pages} 页 / ${d.boxes} 框`
      + (d.device ? ` · ${d.device}` : '');
    const nm = document.createElement('span');
    nm.className = 'doc-name';
    nm.textContent = d.name;
    btn.append(nm);
    if (d.pages) {
      const n = document.createElement('span');
      n.className = 'doc-n';
      n.textContent = `${d.pages}p`;
      btn.append(n);
    }
    btn.onclick = () => { if (d.id !== state.docId) loadDoc(d.id); };
    frag.append(btn);
  }
  tabs.replaceChildren(frag);
}

/* ------------------------------------------------------------------ teardown / load */
function teardown() {
  if (liveObserver) liveObserver.disconnect();
  if (currentObserver) currentObserver.disconnect();
  liveObserver = currentObserver = null;
  pagesEl.replaceChildren();
  pageViews = [];
  byIndex.clear();
  chip.hidden = true;
  chip.removeAttribute('style');
  doc = null;
}

function buildPages() {
  const frag = document.createDocumentFragment();
  doc.pages.forEach((entry, idx) => {
    const root = document.createElement('section');
    root.className = 'page';
    root.id = `page-${entry.page}`;
    root.dataset.idx = String(idx);      // read by the IntersectionObserver callbacks

    const tag = document.createElement('div');
    tag.className = 'page-tag';
    tag.textContent = entry.page;

    const img = document.createElement('img');
    img.loading = 'lazy';
    img.decoding = 'async';
    img.alt = `${state.docs.find((d) => d.id === state.docId)?.name || ''} 第 ${entry.page} 页`;
    img.src = state.base + entry.image;
    img.style.aspectRatio = `${entry.width} / ${entry.height}`;

    const overlay = document.createElement('div');
    overlay.className = 'overlay';
    overlay.dataset.idx = String(idx);

    root.append(tag, img, overlay);
    frag.append(root);

    pageViews.push({ idx, entry, root, overlay, built: false });
    byIndex.set(entry.index, idx);
  });
  pagesEl.replaceChildren(frag);
}

/* ------------------------------------------------------------------ overlays */
function buildOverlay(view) {
  if (view.built) return;
  const { entry, overlay } = view;
  const frag = document.createDocumentFragment();

  entry.boxes.forEach((b, bi) => {
    if (!isVisible(b, entry)) return;
    const el = document.createElement('div');
    const [r, g, bl] = hexToRgb(colorOf(b.label));
    el.className = 'box';
    el.dataset.bi = String(bi);
    el.style.setProperty('--c-rgb', `${r},${g},${bl}`);
    el.style.left = `${(b.x0 / entry.width) * 100}%`;
    el.style.top = `${(b.y0 / entry.height) * 100}%`;
    el.style.width = `${((b.x1 - b.x0) / entry.width) * 100}%`;
    el.style.height = `${((b.y1 - b.y0) / entry.height) * 100}%`;

    if (state.showLabels) {
      const t = document.createElement('span');
      t.className = 'tag';
      t.textContent = zhOf(b.label) || b.label;
      el.append(t);
    }
    if (state.showOrder) {
      const o = document.createElement('span');
      o.className = 'ord';
      o.textContent = b.order;
      el.append(o);
    }
    frag.append(el);
  });

  overlay.replaceChildren(frag);
  view.built = true;
  overlay.dataset.count = String(overlay.childElementCount);
}

function teardownOverlay(view) {
  if (!view.built) return;
  view.overlay.replaceChildren();
  view.overlay.removeAttribute('data-hover');
  view.built = false;
}

function refreshOverlays() {
  for (const view of pageViews) {
    if (!view.built) continue;
    view.built = false;
    view.overlay.replaceChildren();
    view.overlay.removeAttribute('data-hover');
    buildOverlay(view);
  }
}
const refreshOverlaysSoon = debounceRAF(refreshOverlays);

function observeAll() {
  liveObserver = new IntersectionObserver((entries) => {
    for (const e of entries) {
      const view = pageViews[Number(e.target.dataset.idx)];
      if (!view) continue;
      if (e.isIntersecting) buildOverlay(view);
      else teardownOverlay(view);
    }
  }, { root: scroller, rootMargin: '120% 0px 120% 0px' });

  currentObserver = new IntersectionObserver((entries) => {
    for (const e of entries) {
      const idx = Number(e.target.dataset.idx);
      if (e.isIntersecting && pageViews[idx]) setCurrent(idx, false);
    }
  }, { root: scroller, rootMargin: '-48% 0px -48% 0px' });

  for (const view of pageViews) {
    view.overlay.addEventListener('mouseover', onOverlayOver);
    view.overlay.addEventListener('mouseout', onBoxOut);
    view.overlay.addEventListener('mousemove', onOverlayMove);
    view.overlay.addEventListener('mouseleave', onOverlayOut);
    liveObserver.observe(view.root);
    currentObserver.observe(view.root);
  }
}

/* ------------------------------------------------------------------ hover */
function showChip(b) {
  chip.replaceChildren();
  const head = document.createElement('div');
  head.className = 'hc-head';
  const sw = document.createElement('span');
  sw.className = 'hc-swatch';
  sw.style.background = colorOf(b.label);
  const name = document.createElement('span');
  const zh = zhOf(b.label);
  if (zh) {
    name.textContent = `${zh} `;
    const en = document.createElement('span');
    en.className = 'hc-en';
    en.textContent = b.label;
    name.append(en);
  } else {
    name.textContent = b.label;
  }
  head.append(sw, name);

  const sub = document.createElement('div');
  sub.className = 'hc-sub';
  sub.textContent = `score ${b.score.toFixed(3)} · 阅读序 #${b.order} · `
    + `${Math.round(b.x1 - b.x0)}×${Math.round(b.y1 - b.y0)} px`;

  chip.append(head, sub);
  chip.hidden = false;
}

function placeChip(ev) {
  const OFFSET = 16;
  const rect = chip.getBoundingClientRect();
  let x = ev.clientX + OFFSET;
  let y = ev.clientY + OFFSET;
  if (x + rect.width > window.innerWidth - 8) x = ev.clientX - OFFSET - rect.width;
  if (y + rect.height > window.innerHeight - 8) y = ev.clientY - OFFSET - rect.height;
  chip.style.left = `${Math.max(8, x)}px`;
  chip.style.top = `${Math.max(8, y)}px`;
}

function boxFromEvent(ev) {
  const el = ev.target.closest('.box');
  if (!el) return null;
  const view = pageViews[Number(ev.currentTarget.dataset.idx)];
  if (!view) return null;
  return { el, view, box: view.entry.boxes[Number(el.dataset.bi)] };
}

function onOverlayOver(ev) {
  const hit = boxFromEvent(ev);
  if (!hit || !hit.box) return;
  showChip(hit.box);
  if (state.dimOthers) {
    hit.view.overlay.dataset.hover = '1';
    hit.el.classList.add('is-hovered');
  }
}

function onOverlayOut(ev) {
  const overlay = ev.currentTarget;
  if (ev.relatedTarget && overlay.contains(ev.relatedTarget)) return;
  chip.hidden = true;
  overlay.removeAttribute('data-hover');
  const prev = overlay.querySelector('.box.is-hovered');
  if (prev) prev.classList.remove('is-hovered');
}

function onBoxOut(ev) {
  const hit = boxFromEvent(ev);
  if (!hit) return;
  if (ev.relatedTarget && hit.el.contains(ev.relatedTarget)) return;
  hit.el.classList.remove('is-hovered');
  if (!hit.view.overlay.querySelector('.box.is-hovered')) {
    hit.view.overlay.removeAttribute('data-hover');
  }
}

function onOverlayMove(ev) {
  if (!chip.hidden) placeChip(ev);
}

/* ------------------------------------------------------------------ navigation */
function writeHash() {
  if (state.docId === null) return;
  const p = new URLSearchParams();
  if (state.docId) p.set('doc', state.docId);
  p.set('page', String(state.current + 1));
  try { history.replaceState(null, '', `#${p.toString()}`); } catch (err) { /* ignore */ }
}

function readHash() {
  const p = new URLSearchParams(location.hash.replace(/^#/, ''));
  return { doc: p.get('doc'), page: Number(p.get('page')) || 0 };
}

function setCurrent(i, scroll) {
  if (!Number.isFinite(i) || !pageViews.length) return;
  state.current = Math.max(0, Math.min(state.total - 1, i));
  $('pageInput').value = state.current + 1;
  pageViews.forEach((v, n) => v.root.classList.toggle('is-current', n === state.current));
  if (scroll) {
    pageViews[state.current].root.scrollIntoView({ behavior: 'smooth', block: 'start' });
    writeHash();
  }
}

function setZoom(z, { fit = false } = {}) {
  state.fit = fit;
  state.zoom = Math.max(0.25, Math.min(4, z));
  document.documentElement.style.setProperty(
    '--page-w', `${Math.round(BASE_WIDTH * state.zoom)}px`);
  $('zoomVal').textContent = `${Math.round(state.zoom * 100)}%`;
}

function fitWidth() {
  const avail = scroller.clientWidth - 48 - 46;   // scroller padding + page-number gutter
  setZoom(avail / BASE_WIDTH, { fit: true });
}

/* ------------------------------------------------------------------ legend / meta */
function buildLegend() {
  const counts = doc.class_counts || {};
  const frag = document.createDocumentFragment();
  for (const label of Object.keys(counts)) {
    const item = document.createElement('div');
    item.className = 'legend-item';
    item.dataset.label = label;

    const sw = document.createElement('span');
    sw.className = 'legend-swatch';
    sw.style.background = colorOf(label);

    const nm = document.createElement('span');
    nm.className = 'legend-name';
    const zh = document.createElement('span');
    zh.className = 'zh';
    zh.textContent = zhOf(label) || label;
    nm.append(zh);
    if (zhOf(label)) {
      const en = document.createElement('span');
      en.className = 'en';
      en.textContent = label;
      nm.append(en);
    }

    const ct = document.createElement('span');
    ct.className = 'legend-count';
    ct.textContent = counts[label];

    item.append(sw, nm, ct);
    item.title = `${zhOf(label) ? `${zhOf(label)} ` : ''}${label} — ${counts[label]} 个`;
    item.addEventListener('click', () => {
      if (state.hidden.has(label)) state.hidden.delete(label);
      else state.hidden.add(label);
      item.classList.toggle('off', state.hidden.has(label));
      refreshOverlaysSoon();
    });
    frag.append(item);
  }
  $('legendList').replaceChildren(frag);
  $('legendCount').textContent = `${Object.keys(counts).length}`;
}

function renderMeta() {
  const m = doc.meta || {};
  const total = doc.pages.reduce((a, p) => a + p.boxes.length, 0);
  const bits = [
    `${doc.pages.length} 页 · ${total} 框`,
    `${m.ms_per_page_median ?? '?'} ms/页`,
    `设备 <b>${m.device || '?'}</b>`,
    `阈值 ${m.threshold}`,
  ];
  $('meta').innerHTML = bits.map((b) => `<span>${b}</span>`).join('<span>·</span>');
  $('pageTotal').textContent = doc.pages.length;
  $('pageInput').max = doc.pages.length;

  const links = [];
  if (m.annotated_pdf) links.push(`<a href="${state.base}${m.annotated_pdf}" download>下载带框 PDF</a>`);
  links.push(`<a href="${state.base}layout.json" target="_blank">layout.json</a>`);
  links.push(`<a href="${state.base}run.log" target="_blank">运行日志</a>`);
  $('links').innerHTML = links.join(' &nbsp;·&nbsp; ');
}

/* ------------------------------------------------------------------ controls */
function bindRange(id, labelId, fmt, apply) {
  const el = $(id);
  const lab = $(labelId);
  const update = () => {
    const v = Number(el.value);
    apply(v);
    lab.textContent = fmt(v);
  };
  el.addEventListener('input', update);
  update();
}

function bindCheck(id, apply) {
  const el = $(id);
  el.addEventListener('change', () => apply(el.checked));
  apply(el.checked);
}

function wireControls() {
  $('prev').onclick = () => setCurrent(state.current - 1, true);
  $('next').onclick = () => setCurrent(state.current + 1, true);
  $('pageInput').onchange = (e) => setCurrent(Number(e.target.value) - 1, true);

  $('zoomIn').onclick = () => setZoom(state.zoom * 1.15);
  $('zoomOut').onclick = () => setZoom(state.zoom / 1.15);
  $('zoomFit').onclick = fitWidth;

  $('togglePanel').onclick = (e) => {
    const panel = $('panel');
    panel.hidden = !panel.hidden;
    e.currentTarget.setAttribute('aria-expanded', String(!panel.hidden));
  };

  bindRange('threshold', 'thVal', (v) => v.toFixed(2), (v) => {
    state.threshold = v;
    refreshOverlaysSoon();
  });
  bindRange('pad', 'padVal', String, (v) => {
    document.documentElement.style.setProperty('--pad', `${v}px`);
  });
  bindRange('fillAlpha', 'fillVal', (v) => `${v}%`, (v) => {
    document.documentElement.style.setProperty('--fill-a', String(v / 100));
  });
  bindRange('hoverAlpha', 'hoverVal', (v) => `${v}%`, (v) => {
    document.documentElement.style.setProperty('--hover-fill-a', String(v / 100));
  });

  bindCheck('showOrder', (v) => {
    state.showOrder = v;
    document.body.classList.toggle('show-order', v);
    refreshOverlays();
  });
  bindCheck('showLabels', (v) => {
    state.showLabels = v;
    document.body.classList.toggle('show-labels', v);
    refreshOverlays();
  });
  bindCheck('dimOthers', (v) => { state.dimOthers = v; });
  bindCheck('hideFilled', (v) => { state.hideFilled = v; refreshOverlays(); });

  $('allOn').onclick = () => {
    state.hidden.clear();
    document.querySelectorAll('.legend-item').forEach((el) => el.classList.remove('off'));
    refreshOverlays();
  };
  $('allOff').onclick = () => {
    document.querySelectorAll('.legend-item').forEach((el) => {
      state.hidden.add(el.dataset.label);
      el.classList.add('off');
    });
    refreshOverlays();
  };

  const legend = $('legend');
  $('legendToggle').onclick = (e) => {
    const collapsed = legend.classList.toggle('collapsed');
    e.currentTarget.textContent = collapsed ? '▸' : '▾';
    e.currentTarget.setAttribute('aria-expanded', String(!collapsed));
  };
  try {
    if (localStorage.getItem('doclayout.legend') === 'collapsed') $('legendToggle').click();
  } catch (err) { /* private mode */ }
  legend.addEventListener('click', (e) => {
    if (e.target.closest('#legendToggle')) {
      try {
        localStorage.setItem('doclayout.legend',
          legend.classList.contains('collapsed') ? 'collapsed' : 'open');
      } catch (err) { /* private mode */ }
    }
  });

  document.addEventListener('keydown', (e) => {
    if (e.target.tagName === 'INPUT' || e.metaKey || e.ctrlKey) return;
    switch (e.key) {
      case 'ArrowRight': case 'PageDown':
        setCurrent(state.current + 1, true); e.preventDefault(); break;
      case 'ArrowLeft': case 'PageUp':
        setCurrent(state.current - 1, true); e.preventDefault(); break;
      case '+': case '=': setZoom(state.zoom * 1.15); break;
      case '-': case '_': setZoom(state.zoom / 1.15); break;
      case 'o': $('showOrder').click(); break;
      case 'l': $('showLabels').click(); break;
      default: break;
    }
  });

  let resizeTimer = null;
  window.addEventListener('resize', () => {
    clearTimeout(resizeTimer);
    resizeTimer = setTimeout(() => { if (state.fit) fitWidth(); }, 150);
  });
}

/* ------------------------------------------------------------------ boot */
async function loadIndex() {
  try {
    const res = await fetch('data/index.json', { cache: 'no-store' });
    if (res.ok) {
      const payload = await res.json();
      if (Array.isArray(payload.docs) && payload.docs.length) return payload;
    }
  } catch (err) { /* fall through to single-document mode */ }
  return { docs: [{ id: '', name: '当前文档', base: 'data/', pages: null, boxes: null }] };
}

async function loadDoc(id, page) {
  const info = state.docs.find((d) => d.id === id) || state.docs[0];
  state.docId = info.id;
  state.base = info.base || 'data/';
  state.hidden = new Set();
  state.current = 0;
  teardown();
  renderDocTabs();
  try { localStorage.setItem('doclayout.doc', info.id); } catch (err) { /* private mode */ }
  setStatus('loading', `正在加载 <b>${info.name}</b> …`);

  let res;
  try {
    res = await fetch(`${state.base}layout.json`, { cache: 'no-store' });
  } catch (err) {
    return setStatus('error',
      `<p><strong>无法读取 layout.json</strong></p><p>${String(err)}</p>`);
  }
  if (!res.ok) {
    return setStatus('error', '<p><strong>无法读取 layout.json</strong> '
      + `（HTTP ${res.status}）</p><p>请先运行 `
      + '<code>python scripts/run_layout.py --pdf &lt;文件.pdf&gt;</code>。</p>');
  }
  doc = await res.json();
  if (!Array.isArray(doc.pages) || !doc.pages.length) {
    return setStatus('error', '<p><strong>该文档没有可用页面</strong></p>');
  }

  state.total = doc.pages.length;
  buildPages();
  buildLegend();
  renderMeta();
  observeAll();
  fitWidth();
  setStatus('none');

  const start = Math.max(0, Math.min(state.total - 1, (page || 1) - 1));
  setCurrent(start, false);
  scroller.scrollTop = 0;
  if (start > 0) {
    requestAnimationFrame(() => {
      pageViews[start].root.scrollIntoView({ block: 'start' });
      setCurrent(start, false);
    });
  }
  writeHash();
}

async function boot() {
  const index = await loadIndex();
  state.docs = index.docs;
  renderDocTabs();
  const hash = readHash();
  let wanted = hash.doc;
  if (!wanted || !state.docs.some((d) => d.id === wanted)) {
    try { wanted = localStorage.getItem('doclayout.doc'); } catch (err) { wanted = null; }
  }
  if (!wanted || !state.docs.some((d) => d.id === wanted)) wanted = state.docs[0].id;
  await loadDoc(wanted, hash.page);
  window.addEventListener('hashchange', () => {
    const h = readHash();
    if (h.doc && h.doc !== state.docId && state.docs.some((d) => d.id === h.doc)) {
      loadDoc(h.doc, h.page);
    } else if (h.page && h.page - 1 !== state.current) {
      setCurrent(h.page - 1, false);
      pageViews[state.current]?.root.scrollIntoView({ block: 'start' });
    }
  });
}

document.addEventListener('DOMContentLoaded', () => {
  wireControls();
  boot();
});
