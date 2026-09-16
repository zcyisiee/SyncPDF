/* PDF 页查看器骨架：懒渲染页图 + 可视区挂载/卸载 overlay + 命中选择。

数据模型：
- pages: [{index(0-based), width, height}]
- box: {id, box:{x0,y0,x1,y1} 或 [x0,y0,x1,y1], label, color, layer, cls, title, data}
  坐标为左上原点 PDF point，按页面宽高换算成百分比定位。

overlay 只挂载可视页（IntersectionObserver rootMargin 100%）；离开视口拆 DOM，
``data-mounted`` 供测试断言懒挂载行为。
*/
'use strict';

export function labelColor(label) {
  /* label → 稳定 HSL 色（FNV-1a 散列到色相）。 */
  let h = 2166136261;
  const text = String(label || 'unknown');
  for (let i = 0; i < text.length; i += 1) {
    h ^= text.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  const hue = (h >>> 0) % 360;
  return `hsl(${hue} 68% 42%)`;
}

export function hslToRgbTriple(hsl) {
  /* ``hsl(h s% l%)`` → "r,g,b"（CSS 变量 --c-rgb 用）。 */
  const m = /hsl\((\d+)\s+(\d+)%\s+(\d+)%\)/.exec(hsl);
  if (!m) return '17,24,39';
  const h = Number(m[1]) / 360;
  const s = Number(m[2]) / 100;
  const l = Number(m[3]) / 100;
  const q = l < 0.5 ? l * (1 + s) : l + s - l * s;
  const p = 2 * l - q;
  const conv = (t0) => {
    let t = t0;
    if (t < 0) t += 1;
    if (t > 1) t -= 1;
    if (t < 1 / 6) return p + (q - p) * 6 * t;
    if (t < 1 / 2) return q;
    if (t < 2 / 3) return p + (q - p) * (2 / 3 - t) * 6;
    return p;
  };
  return [conv(h + 1 / 3), conv(h), conv(h - 1 / 3)]
    .map((v) => Math.round(v * 255))
    .join(',');
}

function normBox(box) {
  if (!box) return null;
  if (Array.isArray(box)) {
    const [x0, y0, x1, y1] = box.map(Number);
    return { x0, y0, x1, y1 };
  }
  return { x0: Number(box.x0), y0: Number(box.y0), x1: Number(box.x1), y1: Number(box.y1) };
}

function boxArea(box) {
  return Math.max(0, box.x1 - box.x0) * Math.max(0, box.y1 - box.y0);
}

export function createPager(opts) {
  const host = opts.host;               // 页面列表容器（.pages）
  const scroller = opts.scroller;       // 滚动容器
  const chip = opts.chip;               // 悬停 chip
  const hitlist = opts.hitlist;         // 命中列表 popover
  const onSelect = opts.onSelect || (() => {});
  const onCurrent = opts.onCurrent || (() => {});

  const state = {
    run: null,
    pdf: null,                          // artifacts/ 下相对路径
    dpi: opts.dpi || 110,
    pages: [],
    provider: null,                     // (pageIndex) → box[]
    views: [],
    selected: null,
    liveObserver: null,
    currentObserver: null,
  };

  /* ---------------------------------------------------------- teardown */
  function disconnect() {
    if (state.liveObserver) state.liveObserver.disconnect();
    if (state.currentObserver) state.currentObserver.disconnect();
    state.liveObserver = state.currentObserver = null;
  }

  function clear() {
    disconnect();
    host.replaceChildren();
    state.views = [];
    state.selected = null;
    chip.hidden = true;
    hitlist.hidden = true;
  }

  /* ---------------------------------------------------------- build */
  function setDocument(run, pdfRel, pages) {
    state.run = run;
    state.pdf = pdfRel;
    state.pages = pages || [];
    clear();
    const frag = document.createDocumentFragment();
    state.pages.forEach((page, idx) => {
      const root = document.createElement('section');
      root.className = 'page';
      root.dataset.idx = String(idx);
      const tag = document.createElement('div');
      tag.className = 'page-tag';
      tag.textContent = String(idx + 1);
      const img = document.createElement('img');
      img.loading = 'lazy';
      img.decoding = 'async';
      img.alt = `第 ${idx + 1} 页`;
      img.style.aspectRatio = `${page.width || 1} / ${page.height || 1}`;
      const pending = document.createElement('div');
      pending.className = 'img-pending';
      pending.style.aspectRatio = `${page.width || 1} / ${page.height || 1}`;
      pending.textContent = `第 ${idx + 1} 页渲染中…`;
      if (state.pdf) {
        img.src = opts.renderUrl(state.run, state.pdf, idx + 1, state.dpi);
        img.addEventListener('error', () => {
          pending.textContent = `第 ${idx + 1} 页无法渲染（PDF 缺失或渲染失败）`;
          img.replaceWith(pending);
        }, { once: true });
      } else {
        pending.textContent = '该视图无对应 PDF 证据';
        root.append(tag, pending);
        const overlay0 = document.createElement('div');
        overlay0.className = 'overlay';
        overlay0.dataset.idx = String(idx);
        root.append(overlay0);
        frag.append(root);
        state.views.push({ idx, page, root, overlay: overlay0, built: false });
        return;
      }
      const overlay = document.createElement('div');
      overlay.className = 'overlay';
      overlay.dataset.idx = String(idx);
      root.append(tag, img, overlay);
      frag.append(root);
      state.views.push({ idx, page, root, overlay, built: false });
    });
    host.replaceChildren(frag);
    observeAll();
  }

  function setOverlayProvider(provider) {
    state.provider = provider;
    for (const view of state.views) {
      if (view.built) buildOverlay(view, true);
    }
  }

  function boxEl(view, box) {
    const el = document.createElement('div');
    el.className = `box${box.cls ? ` ${box.cls}` : ''}`;
    el.dataset.eid = box.id;
    const color = box.color || labelColor(box.label);
    el.style.setProperty('--c-rgb', hslToRgbTriple(color));
    const w = view.page.width || 1;
    const h = view.page.height || 1;
    el.style.left = `${(box.box.x0 / w) * 100}%`;
    el.style.top = `${(box.box.y0 / h) * 100}%`;
    el.style.width = `${(Math.max(0, box.box.x1 - box.box.x0) / w) * 100}%`;
    el.style.height = `${(Math.max(0, box.box.y1 - box.box.y0) / h) * 100}%`;
    if (box.title) el.title = box.title;
    const tag = document.createElement('span');
    tag.className = 'tag';
    tag.textContent = box.label || box.id;
    // 页内避让：框贴页顶时标签放框内。
    if (box.box.y0 < 18) tag.classList.add('inside');
    el.append(tag);
    return el;
  }

  function buildOverlay(view, force) {
    if (view.built && !force) return;
    view.overlay.replaceChildren();
    if (state.provider) {
      const frag = document.createDocumentFragment();
      for (const box of state.provider(view.idx) || []) {
        const b = { ...box, box: normBox(box.box) };
        if (!b.box) continue;
        const el = boxEl(view, b);
        el._box = b;
        frag.append(el);
      }
      view.overlay.replaceChildren(frag);
    }
    view.built = true;
    view.overlay.dataset.mounted = '1';
    if (state.selected) markSelected(view);
  }

  function teardownOverlay(view) {
    if (!view.built) return;
    view.overlay.replaceChildren();
    view.overlay.removeAttribute('data-hover');
    delete view.overlay.dataset.mounted;
    view.built = false;
  }

  function refresh() {
    for (const view of state.views) {
      if (view.built) {
        view.built = false;
        buildOverlay(view);
      }
    }
  }

  /* ---------------------------------------------------------- observers */
  function observeAll() {
    state.liveObserver = new IntersectionObserver((entries) => {
      for (const e of entries) {
        const view = state.views[Number(e.target.dataset.idx)];
        if (!view) continue;
        if (e.isIntersecting) buildOverlay(view);
        else teardownOverlay(view);
      }
    }, { root: scroller, rootMargin: '100% 0px 100% 0px' });

    state.currentObserver = new IntersectionObserver((entries) => {
      for (const e of entries) {
        if (!e.isIntersecting) continue;
        const idx = Number(e.target.dataset.idx);
        for (const v of state.views) v.root.classList.toggle('is-current', v.idx === idx);
        onCurrent(idx);
      }
    }, { root: scroller, rootMargin: '-45% 0px -45% 0px' });

    for (const view of state.views) {
      view.overlay.addEventListener('mousemove', onMove);
      view.overlay.addEventListener('mouseover', onOver);
      view.overlay.addEventListener('mouseleave', onOut);
      view.overlay.addEventListener('click', onClick);
      state.liveObserver.observe(view.root);
      state.currentObserver.observe(view.root);
    }
  }

  /* ---------------------------------------------------------- hover / hit */
  function boxesAt(view, ev) {
    /* 命中检测：手写包含判定（不按 DOM 命中——嵌套框按面积升序全列出）。 */
    const rect = view.overlay.getBoundingClientRect();
    const x = ((ev.clientX - rect.left) / rect.width) * (view.page.width || 1);
    const y = ((ev.clientY - rect.top) / rect.height) * (view.page.height || 1);
    const hits = [];
    for (const el of view.overlay.querySelectorAll('.box')) {
      const box = el._box;
      if (!box) continue;
      if (x >= box.box.x0 && x <= box.box.x1 && y >= box.box.y0 && y <= box.box.y1) {
        hits.push({ el, box });
      }
    }
    hits.sort((a, b) => boxArea(a.box.box) - boxArea(b.box.box));
    return hits;
  }

  function onOver(ev) {
    const view = state.views[Number(ev.currentTarget.dataset.idx)];
    const hits = boxesAt(view, ev);
    if (!hits.length) return;
    const { el, box } = hits[0];
    showChip(box, ev);
    view.overlay.dataset.hover = '1';
    el.classList.add('is-hovered');
  }

  function onOut(ev) {
    const overlay = ev.currentTarget;
    if (ev.relatedTarget && overlay.contains(ev.relatedTarget)) return;
    chip.hidden = true;
    overlay.removeAttribute('data-hover');
    const prev = overlay.querySelector('.box.is-hovered');
    if (prev) prev.classList.remove('is-hovered');
  }

  function onMove(ev) {
    if (!chip.hidden) {
      const OFFSET = 14;
      const rect = chip.getBoundingClientRect();
      let x = ev.clientX + OFFSET;
      let y = ev.clientY + OFFSET;
      if (x + rect.width > window.innerWidth - 8) x = ev.clientX - OFFSET - rect.width;
      if (y + rect.height > window.innerHeight - 8) y = ev.clientY - OFFSET - rect.height;
      chip.style.left = `${Math.max(8, x)}px`;
      chip.style.top = `${Math.max(8, y)}px`;
    }
  }

  function showChip(box, ev) {
    chip.replaceChildren();
    const head = document.createElement('div');
    head.className = 'hc-head';
    const sw = document.createElement('span');
    sw.className = 'hc-swatch';
    sw.style.background = box.color || labelColor(box.label);
    const name = document.createElement('span');
    name.textContent = box.label || box.kind || 'box';
    head.append(sw, name);
    chip.append(head);
    const sub = document.createElement('div');
    sub.className = 'hc-sub';
    const conf = box.conf != null ? ` · conf ${Number(box.conf).toFixed(2)}` : '';
    sub.textContent = `${box.id}${conf} · ${Math.round(boxArea(box.box))}pt²`;
    chip.append(sub);
    if (box.preview) {
      const text = document.createElement('div');
      text.className = 'hc-text';
      text.textContent = box.preview;
      chip.append(text);
    }
    chip.hidden = false;
    onMove(ev);
  }

  function onClick(ev) {
    const view = state.views[Number(ev.currentTarget.dataset.idx)];
    const hits = boxesAt(view, ev);
    hitlist.hidden = true;
    if (!hits.length) {
      select(null);
      return;
    }
    if (hits.length === 1) {
      select(hits[0].box, view);
      return;
    }
    // 多框相交：命中列表按面积升序（最小框优先）。
    hitlist.replaceChildren();
    const title = document.createElement('div');
    title.className = 'hl-title';
    title.textContent = `${hits.length} 个相交框`;
    hitlist.append(title);
    for (const { box } of hits) {
      const btn = document.createElement('button');
      const sw = document.createElement('span');
      sw.className = 'hl-sw';
      sw.style.background = box.color || labelColor(box.label);
      const nm = document.createElement('span');
      nm.textContent = `${box.label || 'box'} · ${box.id}`;
      const area = document.createElement('span');
      area.className = 'hl-area';
      area.textContent = `${Math.round(boxArea(box.box))}pt²`;
      btn.append(sw, nm, area);
      btn.addEventListener('click', () => {
        hitlist.hidden = true;
        select(box, view);
      });
      hitlist.append(btn);
    }
    hitlist.style.left = `${Math.min(ev.clientX + 8, window.innerWidth - 260)}px`;
    hitlist.style.top = `${Math.min(ev.clientY + 8, window.innerHeight - 200)}px`;
    hitlist.hidden = false;
  }

  /* ---------------------------------------------------------- selection */
  function markSelected(view) {
    for (const el of view.overlay.querySelectorAll('.box')) {
      el.classList.toggle('is-selected', el._box && el._box.id === state.selected);
    }
  }

  function select(box, view) {
    state.selected = box ? box.id : null;
    for (const v of state.views) markSelected(v);
    onSelect(box ? box.id : null, box || null, view ? view.idx : null);
  }

  function selectById(id) {
    /* 按实体 id 选中：找到所在页滚动过去并高亮。 */
    state.selected = id || null;
    for (const view of state.views) markSelected(view);
    if (!id || !state.provider) return;
    for (const view of state.views) {
      for (const raw of state.provider(view.idx) || []) {
        if (raw.id === id) {
          if (!view.built) buildOverlay(view);
          markSelected(view);
          view.root.scrollIntoView({ behavior: 'smooth', block: 'start' });
          onCurrent(view.idx);
          return;
        }
      }
    }
  }

  /* 点击空白处 / Esc 关闭命中列表（overlay 内的点击是打开方，不关）。 */
  document.addEventListener('click', (ev) => {
    if (hitlist.hidden || hitlist.contains(ev.target)) return;
    if (ev.target.closest && ev.target.closest('.overlay')) return;
    hitlist.hidden = true;
  });
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape') hitlist.hidden = true;
  });

  function scrollToPage(idx) {
    const view = state.views[idx];
    if (view) {
      if (!view.built) buildOverlay(view);
      view.root.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
  }

  return {
    setDocument,
    setOverlayProvider,
    refresh,
    selectById,
    scrollToPage,
    clear,
    get pageCount() { return state.pages.length; },
    get selected() { return state.selected; },
    viewFor: (idx) => state.views[idx] || null,
  };
}
