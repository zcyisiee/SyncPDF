/* PP-DocLayoutV3 bbox viewer
   ------------------------------------------------------------------
   Loads layout.json, renders each PDF page as an image and paints the detected
   bboxes as absolutely-positioned elements on top. Overlays are built lazily and
   torn down once their page leaves the viewport, so a 44-page document keeps
   only a couple of overlays in the DOM at any moment.

   Styling knobs that must stay inherited (not set per element, or the :hover
   rule loses the specificity fight): --pad, --fill-a, --hover-fill-a,
   --line-a, --line-w. They all live on :root; each box only carries --c-rgb.
   ------------------------------------------------------------------ */

'use strict';

const DATA_BASE = 'data/';
const BASE_WIDTH = 900;          // page width in px at 100% zoom
const LARGE_AREA_RATIO = 0.6;    // "整页大框" area threshold

const state = {
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

let doc = null;
const pageViews = [];
const byIndex = new Map();

const $ = (id) => document.getElementById(id);
const scroller = $('scroller');
const pagesEl = $('pages');
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

const colorOf = (label) => (doc && doc.palette && doc.palette[label]) || '#475569';

function isVisible(box, page) {
  if (state.hidden.has(box.label)) return false;
  if (box.score < state.threshold) return false;
  if (state.hideFilled) {
    const area = (box.x1 - box.x0) * (box.y1 - box.y0);
    if (area / (page.width * page.height) > LARGE_AREA_RATIO) return false;
  }
  return true;
}

/* ------------------------------------------------------------------ page scaffolding */
function buildPages() {
  const frag = document.createDocumentFragment();
  doc.pages.forEach((entry, idx) => {
    const root = document.createElement('section');
    root.className = 'page';
    root.id = `page-${entry.page}`;

    const tag = document.createElement('div');
    tag.className = 'page-tag';
    tag.textContent = entry.page;

    const img = document.createElement('img');
    img.loading = 'lazy';
    img.decoding = 'async';
    img.alt = `第 ${entry.page} 页`;
    img.src = DATA_BASE + entry.image;
    img.style.aspectRatio = `${entry.width} / ${entry.height}`;

    const overlay = document.createElement('div');
    overlay.className = 'overlay';

    root.append(tag, img, overlay);
    frag.append(root);

    const view = { idx, entry, root, overlay, built: false };
    pageViews.push(view);
    byIndex.set(entry.index, idx);
  });
  pagesEl.append(frag);
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
      t.textContent = b.label;
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

/* ------------------------------------------------------------------ hover */
function showChip(b) {
  chip.replaceChildren();
  const head = document.createElement('div');
  head.className = 'hc-head';
  const sw = document.createElement('span');
  sw.className = 'hc-swatch';
  sw.style.background = colorOf(b.label);
  const name = document.createElement('span');
  name.textContent = b.label;
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

/* ------------------------------------------------------------------ observers */
const liveObserver = new IntersectionObserver((entries) => {
  for (const e of entries) {
    const view = pageViews[Number(e.target.dataset.idx)];
    if (e.isIntersecting) buildOverlay(view);
    else teardownOverlay(view);
  }
}, { root: scroller, rootMargin: '120% 0px 120% 0px' });

const currentObserver = new IntersectionObserver((entries) => {
  for (const e of entries) {
    if (e.isIntersecting) setCurrent(Number(e.target.dataset.idx), false);
  }
}, { root: scroller, rootMargin: '-48% 0px -48% 0px' });

/* ------------------------------------------------------------------ controls */
function setCurrent(i, scroll) {
  state.current = Math.max(0, Math.min(state.total - 1, i));
  $('pageInput').value = state.current + 1;
  pageViews.forEach((v, n) => v.root.classList.toggle('is-current', n === state.current));
  if (scroll) {
    pageViews[state.current].root.scrollIntoView({ behavior: 'smooth', block: 'start' });
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
    nm.textContent = label;

    const ct = document.createElement('span');
    ct.className = 'legend-count';
    ct.textContent = counts[label];

    item.append(sw, nm, ct);
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

/* ------------------------------------------------------------------ meta */
function renderMeta() {
  const m = doc.meta || {};
  const total = doc.pages.reduce((a, p) => a + p.boxes.length, 0);
  const bits = [
    `<b>${m.pdf || ''}</b>`,
    `${doc.pages.length} 页 · ${total} 框`,
    `${m.ms_per_page_median ?? '?'} ms/页`,
    `设备 <b>${m.device || '?'}</b>`,
    `阈值 ${m.threshold}`,
  ];
  $('meta').innerHTML = bits.map((b) => `<span>${b}</span>`).join('<span>·</span>');
  $('pageTotal').textContent = doc.pages.length;
  $('pageInput').max = doc.pages.length;

  const links = [];
  if (m.annotated_pdf) links.push(`<a href="${DATA_BASE}${m.annotated_pdf}" download>下载带框 PDF</a>`);
  links.push(`<a href="${DATA_BASE}layout.json" target="_blank">layout.json</a>`);
  links.push(`<a href="${DATA_BASE}run.log" target="_blank">运行日志</a>`);
  $('links').innerHTML = links.join(' &nbsp;·&nbsp; ');
}

/* ------------------------------------------------------------------ boot */
function fail(title, detail) {
  const el = $('loading');
  el.className = 'error';
  el.innerHTML = `<p><strong>${title}</strong></p><p>${detail || ''}</p>`;
}

async function boot() {
  let res;
  try {
    res = await fetch(`${DATA_BASE}layout.json`, { cache: 'no-store' });
  } catch (err) {
    return fail('无法读取 <code>layout.json</code>', String(err));
  }
  if (!res.ok) {
    return fail(`无法读取 <code>layout.json</code>（HTTP ${res.status}）`,
      '请先运行 <code>python scripts/run_layout.py --pdf &lt;文件.pdf&gt;</code>，'
      + '再用 <code>python scripts/serve.py --data &lt;输出目录&gt;</code> 打开本页。');
  }
  doc = await res.json();
  state.total = doc.pages.length;

  buildPages();
  buildLegend();
  wireControls();
  renderMeta();
  fitWidth();

  for (const view of pageViews) {
    view.root.dataset.idx = String(view.idx);
    view.overlay.dataset.idx = String(view.idx);
    view.overlay.addEventListener('mouseover', onOverlayOver);
    view.overlay.addEventListener('mouseout', onBoxOut);
    view.overlay.addEventListener('mousemove', onOverlayMove);
    view.overlay.addEventListener('mouseleave', onOverlayOut);
    liveObserver.observe(view.root);
    currentObserver.observe(view.root);
  }

  $('loading').remove();
  setCurrent(0, false);
}

document.addEventListener('DOMContentLoaded', boot);
