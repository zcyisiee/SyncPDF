/* 检查视图：reviewer 结论 + 布局 lint + 链接审计，按页/段落定位。

数据：
- artifacts/check/review_verdict.json / layout_lint.json / link_audit.json
- snapshots/build/typesetting_geometry.json（段落 → 页/框 定位）
- 页面叠加 artifacts/build/mono.pdf
*/
'use strict';

const $ = (sel, root) => (root || document).querySelector(sel);

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

function verdictTone(verdict) {
  const v = (verdict || '').toLowerCase();
  if (v === 'pass' || v === 'ok' || v === 'accepted') return 'ok';
  if (v === 'fail' || v === 'rejected') return 'bad';
  return 'warn';
}

export function createCheckView(ctx) {
  const state = {
    verdict: null,
    lint: null,
    links: null,
    paragraphs: new Map(),   // id → paragraph entry（含 page/框）
    byPage: new Map(),       // page_index → overlay boxes
    issues: [],
    filter: 'all',
  };

  function monoPdf(manifest) {
    const artifacts = Object.keys((manifest && manifest.artifacts) || {});
    for (const name of ['artifacts/build/mono.pdf', 'artifacts/build/dual.pdf']) {
      if (artifacts.includes(name)) return name.replace(/^artifacts\//, '');
    }
    return null;
  }

  /* ---------------------------------------------------------- issues */
  function collectIssues() {
    state.issues = [];
    const push = (source, severity, message, ref) => {
      state.issues.push({ source, severity, message: String(message), ref });
    };
    /* 产物用 sev: P1/P2（P1=缺陷 → error，P2/P3 → warning）。 */
    const sevOf = (it, fallback) => {
      if (it.severity) return it.severity;
      const sev = String(it.sev || '').toUpperCase();
      if (sev === 'P1') return 'error';
      if (sev === 'P2' || sev === 'P3') return 'warning';
      return fallback;
    };
    const msgOf = (it) => {
      if (it.message) return it.message;
      const code = it.code || it.rule;
      if (!code) return JSON.stringify(it).slice(0, 200);
      return it.hint ? `${code}：${it.hint}` : code;
    };

    const lint = state.lint;
    if (lint) {
      const items = lint.findings || lint.issues || lint.violations || [];
      for (const it of items) {
        push('lint', sevOf(it, 'warning'), msgOf(it), {
          page: it.page, id: it.id || it.paragraph || it.debug_id,
        });
      }
    }
    const links = state.links;
    if (links) {
      for (const it of links.findings || links.issues || links.missing || []) {
        push('link', sevOf(it, 'warning'), msgOf(it), {
          page: it.page, id: it.id || it.paragraph,
        });
      }
      const broken = links.broken || links.unresolved || links.invalid_destinations;
      if (Array.isArray(broken)) {
        for (const it of broken) {
          const target = typeof it === 'string' ? it : (it.target || it.destination || it.id || '');
          push('link', 'error', `断链/无效目标 ${target}`, { page: it.page, id: it.id });
        }
      }
    }
    const verdict = state.verdict;
    if (verdict) {
      for (const it of verdict.blockers || []) {
        push('review', sevOf(it, 'error'), msgOf(it), { page: it.page, id: it.id || it.paragraph });
      }
      for (const it of verdict.warnings || verdict.issues || verdict.findings || []) {
        push('review', sevOf(it, 'warning'), msgOf(it), { page: it.page, id: it.id || it.paragraph });
      }
    }
  }

  /* ---------------------------------------------------------- overlay */
  function ensureBoxes() {
    state.byPage.clear();
    for (const issue of state.issues) {
      if (state.filter !== 'all' && issue.source !== state.filter) continue;
      const ref = issue.ref || {};
      const para = ref.id ? state.paragraphs.get(ref.id) : null;
      const pageIdx = (ref.page || (para && para.page) || 0) - 1;
      if (pageIdx < 0) continue;
      const raw = para && (para.rendered_box || para.layout_box || para.src_box);
      if (!raw) continue;
      const pageH = state.heights.get(pageIdx) || 0;
      let [x0, y0, x1, y1] = raw;
      [y0, y1] = [pageH - y1, pageH - y0];
      const color = issue.severity === 'error' ? 'hsl(0 75% 45%)' : 'hsl(30 90% 40%)';
      const list = state.byPage.get(pageIdx) || [];
      list.push({
        id: ref.id || `issue-${state.issues.indexOf(issue)}`,
        box: { x0, y0, x1, y1 },
        label: `${issue.source} ${issue.severity}`,
        kind: 'issue',
        color,
        preview: issue.message.slice(0, 80),
        data: issue,
      });
      state.byPage.set(pageIdx, list);
    }
  }

  /* ---------------------------------------------------------- panel */
  function renderPanel() {
    const head = ctx.panelHead;
    const body = ctx.panelBody;
    head.replaceChildren(el('h3', null, '检查结果'));
    body.replaceChildren();

    const sumSec = el('section');
    if (state.verdict) {
      const tone = verdictTone(state.verdict.verdict || state.verdict.status);
      const badge = el('span', `badge ${tone}`, state.verdict.verdict || state.verdict.status || 'unknown');
      sumSec.append(badge, document.createTextNode(' '));
      if (state.verdict.summary) {
        sumSec.append(el('div', 'muted', state.verdict.summary));
      }
    }
    const kv = el('dl', 'kv');
    const add = (k, v) => kv.append(el('dt', null, k), el('dd', null, String(v)));
    add('问题总数', state.issues.length);
    add('lint', state.issues.filter((i) => i.source === 'lint').length);
    add('链接', state.issues.filter((i) => i.source === 'link').length);
    add('review', state.issues.filter((i) => i.source === 'review').length);
    sumSec.append(kv);
    body.append(sumSec);

    const filterSec = el('section');
    filterSec.append(el('h4', null, '来源筛选'));
    const holder = el('div');
    for (const key of ['all', 'lint', 'link', 'review']) {
      const b = el('button', `seg${state.filter === key ? ' on' : ''}`,
        { all: '全部', lint: 'lint', link: '链接', review: 'review' }[key]);
      b.onclick = () => { state.filter = key; ensureBoxes(); ctx.pager.refresh(); renderPanel(); };
      holder.append(b);
    }
    filterSec.append(holder);
    body.append(filterSec);

    const listSec = el('section');
    listSec.append(el('h4', null, `问题（${state.issues.length}）`));
    if (!state.issues.length) listSec.append(el('div', 'empty', '没有记录的问题。'));
    const list = el('div', 'list');
    state.issues.forEach((issue, idx) => {
      const item = el('div', 'item');
      item.append(
        el('div', 'item-head', `[${issue.source}] ${issue.severity}`),
        el('div', 'item-body', issue.message),
      );
      item.onclick = () => {
        const ref = issue.ref || {};
        const para = ref.id ? state.paragraphs.get(ref.id) : null;
        const page = ref.page || (para && para.page);
        if (page) ctx.pager.scrollToPage(page - 1);
        if (ref.id) ctx.select(ref.id);
        list.querySelectorAll('.item').forEach((n) => n.classList.remove('on'));
        item.classList.add('on');
        renderDetail(issue, idx);
      };
      list.append(item);
    });
    listSec.append(list);
    body.append(listSec);

    const detail = el('section');
    detail.id = 'check-detail';
    body.append(detail);
  }

  function renderDetail(issue) {
    const sec = $('#check-detail', ctx.panelBody);
    if (!sec || !issue) return;
    sec.replaceChildren(el('h4', null, '问题详情'));
    const kv = el('dl', 'kv');
    const add = (k, v) => { if (v != null) kv.append(el('dt', null, k), el('dd', 'mono', String(v))); };
    add('source', issue.source);
    add('severity', issue.severity);
    add('page', issue.ref && issue.ref.page);
    add('id', issue.ref && issue.ref.id);
    sec.append(kv);
    sec.append(el('pre', 'raw', JSON.stringify(issue, null, 2)));
  }

  /* ---------------------------------------------------------- mount */
  async function mount() {
    const [verdict, lint, links, geometry, frames] = await Promise.all([
      ctx.api.artifactJson(ctx.run, 'artifacts/check/review_verdict.json'),
      ctx.api.artifactJson(ctx.run, 'artifacts/check/layout_lint.json'),
      ctx.api.artifactJson(ctx.run, 'artifacts/check/link_audit.json'),
      ctx.api.snapshot(ctx.run, 'build/typesetting_geometry.json'),
      ctx.api.snapshot(ctx.run, 'parse/page-frames.json'),
    ]);
    state.verdict = verdict;
    state.lint = lint;
    state.links = links;
    state.heights = new Map();
    const pageInfo = (geometry && geometry.page_info) || [];
    const maxPage = Math.max(
      pageInfo.length,
      frames && frames.frames ? frames.frames.length : 0,
      1,
    );
    const pages = [];
    for (let i = 0; i < maxPage; i += 1) {
      const frame = frames && frames.frames && frames.frames[i];
      const crop = (pageInfo[i] || {}).cropbox;
      const height = (frame && frame.height) || (crop ? crop[3] - crop[1] : 792);
      state.heights.set(i, height);
      pages.push({ index: i, width: (frame && frame.width) || (crop ? crop[2] - crop[0] : 612), height });
    }
    for (const p of (geometry && geometry.paragraphs) || []) {
      if (p.id) state.paragraphs.set(p.id, p);
    }
    collectIssues();
    ensureBoxes();
    ctx.pager.setDocument(ctx.run, monoPdf(ctx.manifest), pages);
    ctx.pager.setOverlayProvider((idx) => state.byPage.get(idx) || []);
    renderPanel();
    if (ctx.setIssueCount) ctx.setIssueCount(state.issues.length);
    if (!verdict && !lint && !links) {
      ctx.setStageStatus('未找到 check 产物', 'error');
    } else {
      ctx.setStageStatus('');
    }
  }

  function onEvent(ev) {
    if (ev.stage === 'check' && ev.kind === 'stage_finished') mount();
  }

  function onSelect(id) {
    const idx = state.issues.findIndex((i) => (i.ref || {}).id === id || `issue-${state.issues.indexOf(i)}` === id);
    if (idx >= 0) renderDetail(state.issues[idx], idx);
  }

  return { mount, onEvent, onSelect, id: 'check' };
}
