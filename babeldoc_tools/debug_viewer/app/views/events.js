/* 事件视图：pipeline 时间轴（阶段泳道瀑布 + 耗时归因 + 原始事件）。

数据：ctx.events（main.js 增量轮询累积，按 seq 递增）。三类来源合成一张瀑布图：

- **阶段窗口**：``stage_started`` → ``stage_finished``/``stage_error``，横向铺满时间轴；
- **计时任务**：``call_started``/``call_finished``（子进程）与 ``span_started``/
  ``span_finished``（远端等待等非子进程工作）成对配对，按真实起止时刻定位成条；
- **点事件**：其余事件（``layout_parsed`` / ``candidate_evaluated`` …）画成活动密度刻度。

耗时归因把计时任务按来源（``origin``）或阶段汇总，直接回答"等待翻译占了多久、
等待 MinerU 占了多久"。并行任务同时给出累计（相加）与墙钟（区间并集）两个口径。

主区渲染在 textPane（全宽）；过滤与选中详情在工作台面板。
*/
'use strict';

const STAGES = ['parse', 'translate', 'apply', 'build', 'check', 'review', 'report'];
const STAGE_LABEL = {
  parse: '识别', translate: '翻译', apply: '写回', build: '编译',
  check: '检查', review: '审查', report: '报告',
};
const STAGE_COLOR = {
  parse: 'hsl(210 80% 45%)',
  translate: 'hsl(280 60% 45%)',
  apply: 'hsl(330 70% 45%)',
  build: 'hsl(150 70% 35%)',
  check: 'hsl(35 85% 45%)',
  review: 'hsl(190 70% 38%)',
  report: 'hsl(220 12% 46%)',
};

/* 来源名 → 人话标签。未知来源原样显示（不猜）。 */
const ORIGIN_LABEL = {
  'translator.whole': '等待翻译模型（整篇）',
  'translator.retry': '等待翻译模型（补译）',
  reviewer: '等待审查模型',
  xelatex: 'xelatex 编译',
  'mineru.request_upload_urls': 'MinerU 申请上传地址',
  'mineru.upload': 'MinerU 上传 PDF',
  'mineru.poll': '等待 MinerU 解析（轮询）',
  'mineru.download': '下载 MinerU 结果',
  'mineru.parse_zip': '解包 MinerU 结果（本地）',
  'mineru.cache': 'MinerU 布局缓存命中（本地）',
};

function el(tag, cls, text) {
  const node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text != null) node.textContent = text;
  return node;
}

/* 事件时间戳（ISO8601 带毫秒与偏移）→ epoch ms；无法解析返回 NaN。 */
function ts(at) {
  if (!at) return NaN;
  const value = Date.parse(String(at));
  return Number.isFinite(value) ? value : NaN;
}

function fmtDur(sec) {
  if (!Number.isFinite(sec) || sec < 0) return '–';
  if (sec < 0.001) return '0ms';
  if (sec < 1) return `${Math.round(sec * 1000)}ms`;
  if (sec < 10) return `${sec.toFixed(2)}s`;
  if (sec < 60) return `${sec.toFixed(1)}s`;
  const m = Math.floor(sec / 60);
  const s = Math.round(sec % 60);
  if (m < 60) return s ? `${m}m${String(s).padStart(2, '0')}s` : `${m}m`;
  const h = Math.floor(m / 60);
  return `${h}h${String(m % 60).padStart(2, '0')}m`;
}

function fmtClock(ms) {
  if (!Number.isFinite(ms)) return '–';
  return new Date(ms).toLocaleTimeString('zh-CN', { hour12: false });
}

function fmtPct(x) {
  if (!Number.isFinite(x)) return '–';
  if (x > 0 && x < 0.1) return '<0.1%';
  return `${x.toFixed(x < 10 ? 1 : 0)}%`;
}

function brief(data, limit = 160) {
  if (data == null) return '';
  try {
    const text = JSON.stringify(data);
    return text.length > limit ? `${text.slice(0, limit)}…` : text;
  } catch (err) {
    return String(data).slice(0, limit);
  }
}

/* 从事件 data 猜出可跳转的 {view, page, entity}。 */
function linkTarget(ev) {
  const d = (ev && ev.data) || {};
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

/* 区间并集长度（ms）：并行任务的"墙钟"占用。 */
function unionMs(intervals) {
  if (!intervals.length) return 0;
  const sorted = intervals.slice().sort((a, b) => a[0] - b[0]);
  let total = 0;
  let [start, end] = sorted[0];
  for (let i = 1; i < sorted.length; i += 1) {
    const [s, e] = sorted[i];
    if (s > end) {
      total += end - start;
      [start, end] = [s, e];
    } else if (e > end) {
      end = e;
    }
  }
  return total + (end - start);
}

/* 区间图着色：把一条泳道里重叠的条分到不同子行。 */
function packRows(bars) {
  const rows = [];
  const sorted = bars.slice().sort((a, b) => (a.start - b.start) || (a.end - b.end));
  for (const bar of sorted) {
    let placed = false;
    for (const row of rows) {
      if (row[row.length - 1].end <= bar.start) {
        row.push(bar);
        placed = true;
        break;
      }
    }
    if (!placed) rows.push([bar]);
  }
  return rows;
}

/* 时间轴刻度：按总时长挑一个"好看"的步长，目标 6–12 个刻度。 */
function tickStep(spanMs) {
  const candidates = [
    100, 200, 500, 1000, 2000, 5000, 10000, 15000, 30000, 60000,
    120000, 300000, 600000, 900000, 1800000, 3600000,
  ];
  for (const step of candidates) {
    if (spanMs / step <= 12) return step;
  }
  return 3600000;
}

function fmtTick(ms) {
  if (ms < 1000) return `${Math.round(ms)}ms`;
  if (ms < 60000) return `${Math.round(ms / 1000)}s`;
  const m = Math.floor(ms / 60000);
  const s = Math.round((ms % 60000) / 1000);
  return s ? `${m}m${String(s).padStart(2, '0')}s` : `${m}m`;
}

/* ---------------------------------------------------------------- model */
/* 把事件流折叠成：阶段窗口 + 计时条 + 点事件 + 归因。 */
function buildModel(events) {
  const parsed = [];
  for (const ev of events) {
    const t = ts(ev.at);
    if (Number.isFinite(t)) parsed.push({ ev, t });
  }
  const model = {
    parsed, t0: 0, t1: 0, totalMs: 0, stages: [], bars: [], points: [],
    attribOrigin: [], attribStage: [], running: [], eventTotal: events.length,
    missingTime: events.length - parsed.length,
  };
  if (!parsed.length) return model;

  let min = Infinity;
  let max = -Infinity;
  for (const { t } of parsed) {
    if (t < min) min = t;
    if (t > max) max = t;
  }
  model.t0 = min;
  model.t1 = max;
  model.totalMs = Math.max(0, max - min);

  const stageMap = new Map();
  const openTasks = new Map();
  const BAR_KINDS = { call_started: 'call', span_started: 'span' };
  const END_KINDS = { call_finished: 'call', span_finished: 'span' };

  for (const { ev, t } of parsed) {
    const stage = ev.stage || 'other';
    const d = ev.data || {};

    if (ev.kind === 'stage_started' || ev.kind === 'stage_finished' || ev.kind === 'stage_error') {
      const entry = stageMap.get(stage) || { stage, startT: NaN, endT: NaN, events: 0, status: '' };
      if (ev.kind === 'stage_started') {
        entry.startT = Number.isFinite(entry.startT) ? Math.min(entry.startT, t) : t;
      } else {
        entry.endT = Number.isFinite(entry.endT) ? Math.max(entry.endT, t) : t;
        if (ev.kind === 'stage_error') entry.status = 'error';
      }
      stageMap.set(stage, entry);
    }

    const startKind = BAR_KINDS[ev.kind];
    if (startKind) {
      const id = d.call_id || d.span_id || `seq${ev.seq}`;
      openTasks.set(`${startKind}:${id}`, { ev, t });
      continue;
    }
    const endKind = END_KINDS[ev.kind];
    if (endKind) {
      const id = d.call_id || d.span_id || `seq${ev.seq}`;
      const key = `${endKind}:${id}`;
      const opened = openTasks.get(key);
      openTasks.delete(key);
      const seconds = Number(d.seconds);
      const endT = t;
      let startT = opened ? opened.t : NaN;
      if (!Number.isFinite(startT)) {
        startT = Number.isFinite(seconds) ? endT - seconds * 1000 : endT;
      }
      const source = opened ? opened.ev : ev;
      const data = { ...(source.data || {}), ...d };
      model.bars.push({
        key,
        kind: endKind,
        stage: source.stage || stage,
        origin: data.origin || endKind,
        command: data.command && data.command.executable ? data.command.executable : '',
        status: data.status || (data.returncode ? 'error' : 'ok'),
        seconds: Number.isFinite(seconds) ? seconds : (endT - startT) / 1000,
        start: startT,
        end: endT,
        seq: ev.seq,
        data,
        event: ev,
      });
      continue;
    }
    /* 其余事件：活动密度刻度。 */
    model.points.push({ ev, t, stage });
  }

  /* 未闭合的 started（管线正在跑）：画到当前末尾，标注"进行中"。 */
  for (const [key, opened] of openTasks) {
    const d = opened.ev.data || {};
    model.running.push({
      key, kind: key.split(':')[0], stage: opened.ev.stage, origin: d.origin || key,
      command: d.command && d.command.executable ? d.command.executable : '',
      start: opened.t, end: max, seconds: (max - opened.t) / 1000,
      status: 'running', seq: opened.ev.seq, data: d, event: opened.ev,
    });
  }

  /* 阶段窗口：有显式边界的用边界，其余用该阶段事件的跨度兜底。 */
  const stageBars = new Map();
  for (const bar of model.bars) {
    const list = stageBars.get(bar.stage) || [];
    list.push(bar);
    stageBars.set(bar.stage, list);
  }
  for (const { ev, t } of parsed) {
    const stage = ev.stage || 'other';
    if (!stageMap.has(stage)) {
      stageMap.set(stage, { stage, startT: NaN, endT: NaN, events: 0, status: '' });
    }
    const entry = stageMap.get(stage);
    entry.events += 1;
    if (!Number.isFinite(entry.startT) || t < entry.startT) entry.startT = t;
    if (!Number.isFinite(entry.endT) || t > entry.endT) entry.endT = t;
  }
  model.stages = [...stageMap.values()]
    .sort((a, b) => (a.startT - b.startT) || (STAGES.indexOf(a.stage) - STAGES.indexOf(b.stage)))
    .map((entry) => {
      const stage = entry.stage;
      return {
        ...entry,
        label: STAGE_LABEL[stage] || stage,
        color: STAGE_COLOR[stage] || 'var(--muted)',
        dur: Math.max(0, (entry.endT - entry.startT) / 1000),
        started: Number.isFinite(entry.startT) && Number.isFinite(entry.endT),
        points: model.points.filter((p) => p.stage === stage),
        bars: stageBars.get(stage) || [],
      };
    });

  /* 归因：按来源与按阶段各一份。 */
  const allBars = [...model.bars, ...model.running];
  const group = (keyOf, labelOf) => {
    const map = new Map();
    for (const bar of allBars) {
      const key = keyOf(bar);
      const entry = map.get(key) || {
        key, label: labelOf(key), count: 0, sum: 0, intervals: [], stage: bar.stage,
      };
      entry.count += 1;
      entry.sum += bar.seconds;
      entry.intervals.push([bar.start, bar.end]);
      map.set(key, entry);
    }
    return [...map.values()]
      .map((entry) => ({
        ...entry,
        wall: unionMs(entry.intervals) / 1000,
        sumPct: model.totalMs ? ((entry.sum * 1000) / model.totalMs) * 100 : 0,
      }))
      .sort((a, b) => b.sum - a.sum);
  };
  model.attribOrigin = group(
    (bar) => bar.origin,
    (key) => ORIGIN_LABEL[key] || key,
  );
  model.attribStage = group(
    (bar) => bar.stage,
    (key) => STAGE_LABEL[key] || key,
  );
  return model;
}

/* ---------------------------------------------------------------- view */
export function createEventsView(ctx) {
  const state = {
    stage: 'all',        // 泳道过滤（选单阶段 = 时间轴缩放到该阶段）
    minMs: 0,            // 隐藏短于该值的条
    showPoints: true,
    attribBy: 'origin',
    kindFilter: '',
    autoScroll: true,
    selectedKey: null,
    model: null,
    refs: null,
    rawRendered: 0,
    rawList: null,
    rawCount: null,
    pending: false,
  };

  const stageLabel = (name) => STAGE_LABEL[name] || name;
  const colorOf = (name) => STAGE_COLOR[name] || 'var(--muted)';

  /* 当前时间窗口（ms）：选单阶段时缩放到该阶段，否则整轮。 */
  function windowOf(model) {
    if (state.stage !== 'all') {
      const stage = model.stages.find((s) => s.stage === state.stage);
      if (stage && stage.endT > stage.startT) return { t0: stage.startT, t1: stage.endT };
    }
    return { t0: model.t0, t1: model.t1 };
  }

  function visibleBars(model) {
    const { t0, t1 } = windowOf(model);
    const floor = state.minMs || 0;
    return [...model.bars, ...model.running]
      .filter((bar) => state.stage === 'all' || bar.stage === state.stage)
      .filter((bar) => bar.seconds * 1000 >= floor)
      .filter((bar) => bar.end >= t0 && bar.start <= t1);
  }

  /* ---------------------------------------------------------- summary */
  function renderSummary(model) {
    const host = state.refs.summary;
    host.replaceChildren();
    const head = el('div', 'tl-head');
    const total = el('div', 'tl-total');
    total.append(
      el('div', 'tl-total-value', fmtDur(model.totalMs / 1000)),
      el('div', 'tl-total-label', '总耗时'),
    );
    const meta = el('div', 'tl-meta');
    meta.append(
      el('span', null, `${model.stages.length} 个阶段`),
      el('span', null, `${model.eventTotal} 条事件`),
      el('span', null, `${model.bars.length} 个计时任务`),
      el('span', null, `起点 ${fmtClock(model.t0)}`),
    );
    if (model.running.length) {
      meta.append(el('span', 'badge warn', `${model.running.length} 个任务进行中`));
    }
    if (model.missingTime) {
      meta.append(el('span', 'tl-note-inline', `${model.missingTime} 条事件无有效时间戳`));
    }
    head.append(total, meta);
    host.append(head);

    if (model.totalMs > 0) {
      const stack = el('div', 'tl-stack');
      for (const stage of model.stages) {
        if (!(stage.dur > 0)) continue;
        const pct = ((stage.dur * 1000) / model.totalMs) * 100;
        /* 每个跑过的阶段都留一条（极小占比靠 CSS min-width 兜成细线），
           否则 12ms 的报告会整段消失，看不出流程走完了。 */
        const seg = el('span', 'tl-seg');
        seg.style.flex = String(Math.max(pct, 0.12));
        seg.style.background = colorOf(stage.stage);
        seg.title = `${stage.label} ${fmtDur(stage.dur)}（${fmtPct(pct)}）`;
        /* 文字随占比降级：窄段只留阶段名，更窄就只留颜色与 tooltip——
           截成「编译 39.2…」「检…」既读不全又挤占相邻段。 */
        const text = pct >= 12 ? `${stage.label} ${fmtDur(stage.dur)} · ${fmtPct(pct)}`
          : pct >= 6 ? `${stage.label} ${fmtDur(stage.dur)}`
          : pct >= 3 ? stage.label
          : '';
        if (text) {
          const inside = el('span', 'tl-seg-text');
          inside.textContent = text;
          seg.append(inside);
        }
        stack.append(seg);
      }
      host.append(stack);
    }
  }

  /* ---------------------------------------------------------- lanes */
  function barEl(bar, t0, span) {
    const node = el('div', 'tl-bar');
    node.classList.add(bar.kind);
    if (bar.status === 'running') node.classList.add('running');
    if (bar.status === 'error') node.classList.add('error');
    const left = ((bar.start - t0) / span) * 100;
    const width = ((bar.end - bar.start) / span) * 100;
    node.style.left = `${Math.min(Math.max(left, 0), 99.9)}%`;
    node.style.width = `${Math.min(Math.max(width, 0.12), 100)}%`;
    node.style.background = colorOf(bar.stage);
    const source = ORIGIN_LABEL[bar.origin] || bar.origin;
    node.title = [
      `${stageLabel(bar.stage)} · ${bar.kind === 'span' ? '计时片段' : '子进程'}`,
      source,
      bar.command ? `命令 ${bar.command}` : '',
      `${fmtClock(bar.start)} → ${fmtClock(bar.end)}`,
      `耗时 ${fmtDur(bar.seconds)}`,
      bar.status && bar.status !== 'ok' ? `状态 ${bar.status}` : '',
      `#${bar.seq}`,
    ].filter(Boolean).join('\n');
    if (width >= 4.5) {
      const text = bar.command && bar.kind === 'call'
        ? `${bar.command} ${fmtDur(bar.seconds)}`
        : `${source} ${fmtDur(bar.seconds)}`;
      const label = el('span', 'tl-bar-text');
      label.textContent = text.length > 28 ? `${text.slice(0, 28)}…` : text;
      node.append(label);
    }
    if (state.selectedKey === bar.key) node.classList.add('is-selected');
    node.onclick = (ev) => {
      ev.stopPropagation();
      state.selectedKey = state.selectedKey === bar.key ? null : bar.key;
      renderLanes(state.model);
      renderDetail();
    };
    return node;
  }

  function renderLanes(model) {
    const host = state.refs.lanes;
    host.replaceChildren();
    host.style.setProperty('--tl-window', '100%');
    if (!model.parsed.length) {
      host.append(el('div', 'empty', '该 run 没有带时间戳的事件。'));
      return;
    }
    const { t0, t1 } = windowOf(model);
    const span = Math.max(t1 - t0, 1);
    const bars = visibleBars(model);
    const shown = new Map();
    for (const bar of bars) {
      const list = shown.get(bar.stage) || [];
      list.push(bar);
      shown.set(bar.stage, list);
    }

    /* 时间标尺。 */
    const step = tickStep(span);
    host.style.setProperty('--tl-grid', `${(step / span) * 100}%`);
    const ruler = el('div', 'tl-row tl-ruler-row');
    ruler.append(el('div', 'tl-name', ''));
    const rulerTrack = el('div', 'tl-track');
    for (let offset = 0; offset <= span; offset += step) {
      const tick = el('div', 'tl-tick-label');
      tick.style.left = `${Math.min((offset / span) * 100, 100)}%`;
      tick.textContent = `${fmtTick(offset)}`;
      rulerTrack.append(tick);
    }
    ruler.append(rulerTrack);
    host.append(ruler);

    for (const stage of model.stages) {
      if (state.stage !== 'all' && stage.stage !== state.stage) continue;
      const row = el('div', 'tl-row');
      const name = el('div', 'tl-name');
      const dot = el('span', 'tl-dot');
      dot.style.background = colorOf(stage.stage);
      name.append(dot, el('span', 'tl-name-text', stage.label));
      name.append(el('em', 'tl-name-dur', stage.started ? fmtDur(stage.dur) : '未闭合'));
      const track = el('div', 'tl-track');
      if (stage.started && stage.endT > stage.startT) {
        const spanBar = el('div', 'tl-stage-span');
        const left = ((stage.startT - t0) / span) * 100;
        const width = ((stage.endT - stage.startT) / span) * 100;
        spanBar.style.left = `${Math.min(Math.max(left, 0), 99.9)}%`;
        spanBar.style.width = `${Math.min(Math.max(width, 0.1), 100)}%`;
        spanBar.style.background = colorOf(stage.stage);
        track.append(spanBar);
      }
      if (state.showPoints) {
        const dots = el('div', 'tl-dots');
        for (const point of stage.points) {
          if (point.t < t0 || point.t > t1) continue;
          const tick = el('div', 'tl-dot-tick');
          tick.style.left = `${((point.t - t0) / span) * 100}%`;
          tick.title = `${fmtClock(point.t)} ${point.ev.kind}\n${brief(point.ev.data, 120)}`;
          dots.append(tick);
        }
        track.append(dots);
      }
      const stageBars = (shown.get(stage.stage) || []).sort((a, b) => a.start - b.start);
      const rows = packRows(stageBars);
      const MAX_ROWS = 8;
      const stack = el('div', 'tl-rows');
      for (const sub of rows.slice(0, MAX_ROWS)) {
        const subRow = el('div', 'tl-sub');
        for (const bar of sub) subRow.append(barEl(bar, t0, span));
        stack.append(subRow);
      }
      if (rows.length > MAX_ROWS) {
        stack.append(el('div', 'tl-more', `…另有 ${rows.length - MAX_ROWS} 行重叠任务未显示（用"最小耗时"收敛）`));
      }
      track.append(stack);
      row.append(name, track);
      row.onclick = () => {
        state.selectedKey = null;
        state.stage = state.stage === stage.stage ? 'all' : stage.stage;
        renderAll();
      };
      host.append(row);
    }

    const hidden = model.bars.length + model.running.length - bars.length;
    if (hidden > 0) {
      host.append(el('div', 'tl-note', `已按当前过滤隐藏 ${hidden} 个计时任务。`));
    }
  }

  /* ---------------------------------------------------------- attrib */
  function renderAttrib(model) {
    const host = state.refs.attrib;
    host.replaceChildren();
    const groups = state.attribBy === 'origin' ? model.attribOrigin : model.attribStage;
    const head = el('div', 'tl-subhead');
    head.append(el('span', 'tl-subhead-title', '耗时归因'));
    head.append(el('span', 'tl-hint', state.attribBy === 'origin'
      ? '按来源 · 累计 = 各任务相加，墙钟 = 区间并集'
      : '按阶段 · 累计 = 各任务相加，墙钟 = 区间并集'));
    host.append(head);
    if (!groups.length) {
      host.append(el('div', 'empty', '没有计时任务（parse-only 归档或旧版采集格式）。'));
      return;
    }
    const max = Math.max(...groups.map((g) => g.sum), 0.001);
    for (const entry of groups) {
      const row = el('div', 'tl-arow');
      const label = el('div', 'tl-aname');
      label.append(el('span', 'tl-aname-text', entry.label));
      if (entry.count > 1) label.append(el('span', 'tl-acount', `×${entry.count}`));
      const value = el('div', 'tl-avalue');
      value.append(el('span', 'tl-asum', fmtDur(entry.sum)));
      if (entry.count > 1 && entry.wall < entry.sum - 0.05) {
        value.append(el('span', 'tl-awall', `墙钟 ${fmtDur(entry.wall)}`));
      }
      const barWrap = el('div', 'tl-abar');
      const fill = el('div', 'tl-afill');
      fill.style.width = `${(entry.sum / max) * 100}%`;
      fill.style.background = colorOf(entry.stage);
      barWrap.append(fill);
      row.append(label, value, barWrap, el('div', 'tl-apct', fmtPct(entry.sumPct)));
      row.title = `${entry.count} 个任务\n累计 ${fmtDur(entry.sum)}\n墙钟 ${fmtDur(entry.wall)}\n占整轮 ${fmtPct(entry.sumPct)}`;
      host.append(row);
    }
  }

  /* ---------------------------------------------------------- raw list */
  function visibleEvent(ev) {
    if (state.stage !== 'all' && ev.stage !== state.stage) return false;
    if (state.kindFilter && !(ev.kind || '').toLowerCase().includes(state.kindFilter)) return false;
    return true;
  }

  function renderRaw() {
    const list = state.rawList;
    if (!list) return;
    const events = ctx.events;
    if (state.rawRendered > events.length) {
      list.replaceChildren();
      state.rawRendered = 0;
    }
    const frag = document.createDocumentFragment();
    for (let i = state.rawRendered; i < events.length; i += 1) {
      const ev = events[i];
      if (!visibleEvent(ev)) continue;
      const row = el('div', 'ev-row');
      row.append(
        el('span', 'ev-seq', `#${ev.seq}`),
        el('span', 'ev-stage', stageLabel(ev.stage || '')),
        el('span', 'ev-kind', ev.kind || ''),
        el('span', 'ev-data', brief(ev.data)),
      );
      row.title = `${ev.at || ''} ${ev.stage || ''}/${ev.kind || ''}\n${brief(ev.data)}`;
      const target = linkTarget(ev);
      if (target) {
        row.classList.add('has-link');
        const go = el('button', 'ev-go', '跳转');
        go.onclick = (e) => {
          e.stopPropagation();
          ctx.goto(target.view, target.page, target.entity);
        };
        row.append(go);
      }
      frag.append(row);
    }
    state.rawRendered = events.length;
    list.append(frag);
    if (state.autoScroll && list.lastElementChild) {
      list.lastElementChild.scrollIntoView({ block: 'end' });
    }
  }

  function mountRaw() {
    const pane = ctx.textPane;
    const details = el('details', 'tl-raw');
    const summary = el('summary');
    summary.append(el('span', 'tl-raw-title', '原始事件流'));
    const count = el('span', 'tl-raw-count', `${ctx.events.length} 条`);
    summary.append(count);
    const tools = el('div', 'tl-raw-tools');
    const kind = document.createElement('input');
    kind.placeholder = 'kind 包含…';
    kind.value = state.kindFilter;
    kind.oninput = () => {
      state.kindFilter = kind.value.trim().toLowerCase();
      state.rawRendered = 0;
      list.replaceChildren();
      renderRaw();
    };
    kind.onclick = (e) => e.stopPropagation();
    const autoLabel = el('label', 'ctl');
    const auto = document.createElement('input');
    auto.type = 'checkbox';
    auto.checked = state.autoScroll;
    auto.onchange = () => { state.autoScroll = auto.checked; };
    autoLabel.append(auto, el('span', null, '跟随最新'));
    autoLabel.onclick = (e) => e.stopPropagation();
    tools.append(kind, autoLabel);
    const list = el('div', 'ev-list');
    details.append(summary, tools, list);
    details.ontoggle = () => {
      if (details.open) {
        state.rawRendered = 0;
        list.replaceChildren();
        renderRaw();
      }
    };
    pane.append(details);
    state.rawList = list;
    state.rawCount = count;
  }

  /* ---------------------------------------------------------- detail */
  function buildDetail() {
    const sec = el('section');
    sec.id = 'tl-detail';
    sec.append(el('h4', null, '选中任务'));
    const model = state.model;
    const all = model ? [...model.bars, ...model.running] : [];
    const bar = all.find((b) => b.key === state.selectedKey);
    if (!bar) {
      sec.append(el('div', 'empty', '点击时间轴上的条查看起止时刻、来源与原始数据。'));
      const stage = model && state.stage !== 'all'
        ? model.stages.find((s) => s.stage === state.stage)
        : null;
      if (stage) {
        const kv = el('dl', 'kv');
        kv.append(el('dt', null, '阶段'), el('dd', null, stage.label));
        kv.append(el('dt', null, '窗口'), el('dd', 'mono', `${fmtClock(stage.startT)} → ${fmtClock(stage.endT)}`));
        kv.append(el('dt', null, '耗时'), el('dd', 'mono', fmtDur(stage.dur)));
        kv.append(el('dt', null, '事件'), el('dd', 'mono', String(stage.events)));
        kv.append(el('dt', null, '计时任务'), el('dd', 'mono', String(stage.bars.length)));
        sec.append(kv);
      }
      return sec;
    }
    const badges = el('div', 'badges');
    const stageBadge = el('span', 'badge', stageLabel(bar.stage));
    stageBadge.style.borderColor = colorOf(bar.stage);
    stageBadge.style.color = colorOf(bar.stage);
    badges.append(stageBadge);
    badges.append(el('span', 'badge muted', bar.kind === 'span' ? '计时片段' : '子进程'));
    if (bar.status && bar.status !== 'ok') {
      badges.append(el('span', `badge ${bar.status === 'running' ? 'warn' : 'bad'}`, bar.status));
    }
    sec.append(badges);

    const kv = el('dl', 'kv');
    const add = (k, v, mono) => {
      if (v == null || v === '') return;
      kv.append(el('dt', null, k), el('dd', mono ? 'mono' : null, String(v)));
    };
    add('来源', ORIGIN_LABEL[bar.origin] || bar.origin);
    if (ORIGIN_LABEL[bar.origin]) add('原始 origin', bar.origin, true);
    add('命令', bar.command, true);
    add('开始', fmtClock(bar.start), true);
    add('结束', fmtClock(bar.end), true);
    add('耗时', fmtDur(bar.seconds), true);
    const d = bar.data || {};
    if (d.returncode != null) add('退出码', d.returncode, true);
    if (d.timeout != null) add('超时上限', `${d.timeout}s`, true);
    if (d.batch_id) add('batch', d.batch_id, true);
    if (d.pages != null) add('页数', d.pages, true);
    if (d.segments != null) add('段数', d.segments, true);
    if (d.round != null) add('轮次', d.round, true);
    if (d.bytes != null) add('字节', d.bytes, true);
    if (d.cache_file) add('缓存文件', d.cache_file, true);
    add('事件 seq', `#${bar.seq}`, true);
    sec.append(kv);

    if (d.error) sec.append(el('pre', 'block', String(d.error)));
    const target = linkTarget(bar.event);
    if (target) {
      const go = el('button', 'seg', '跳到关联实体');
      go.onclick = () => ctx.goto(target.view, target.page, target.entity);
      sec.append(go);
    }
    const raw = el('details', 'tl-raw-detail');
    raw.append(el('summary', null, '原始事件 data'));
    raw.append(el('pre', 'raw', brief(d, 4000)));
    sec.append(raw);
    return sec;
  }

  /* 详情单独写进预留容器：重画详情不重建过滤控件（保持滑块/下拉的焦点）。 */
  function renderDetail() {
    if (state.refs && state.refs.detailHolder) {
      state.refs.detailHolder.replaceChildren(buildDetail());
    }
  }

  /* ---------------------------------------------------------- panel */
  /* 控件只建一次：live run 每秒重画，重建控件会关掉正打开的下拉、
     打断正在拖的滑块。之后只就地更新取值。 */
  function mountPanel() {
    ctx.panelHead.replaceChildren(el('h3', null, '时间轴'));
    const body = ctx.panelBody;
    body.replaceChildren();
    const refs = state.refs;

    const ctl = el('section');
    ctl.append(el('h4', null, '视图'));

    const row1 = el('div', 'ctl');
    const stageSel = document.createElement('select');
    refs.stageSel = stageSel;
    stageSel.onchange = () => { state.stage = stageSel.value; renderLanes(state.model); renderDetail(); renderOverview(); };
    row1.append(stageSel);
    ctl.append(row1);

    const row2 = el('div', 'ctl');
    const minInput = document.createElement('input');
    minInput.type = 'range';
    minInput.min = '0';
    minInput.max = '3000';
    minInput.step = '50';
    minInput.value = String(state.minMs);
    const minVal = el('span', 'val', '不限');
    refs.minVal = minVal;
    minInput.oninput = () => {
      state.minMs = Number(minInput.value);
      minVal.textContent = state.minMs ? `${state.minMs}ms` : '不限';
      renderLanes(state.model);
    };
    row2.append(el('span', null, '最小耗时'), minInput, minVal);
    ctl.append(row2);

    const row3 = el('div', 'ctl');
    const pts = el('label', 'ctl');
    const ptsBox = document.createElement('input');
    ptsBox.type = 'checkbox';
    ptsBox.checked = state.showPoints;
    ptsBox.onchange = () => { state.showPoints = ptsBox.checked; renderLanes(state.model); };
    pts.append(ptsBox, el('span', null, '点事件'));
    const attribSel = document.createElement('select');
    for (const [value, text] of [['origin', '归因：按来源'], ['stage', '归因：按阶段']]) {
      const opt = document.createElement('option');
      opt.value = value;
      opt.textContent = text;
      if (value === state.attribBy) opt.selected = true;
      attribSel.append(opt);
    }
    attribSel.onchange = () => {
      state.attribBy = attribSel.value;
      renderAttrib(state.model);
      renderPanelTail();
    };
    row3.append(pts, attribSel);
    ctl.append(row3);
    body.append(ctl);

    const sum = el('section');
    sum.append(el('h4', null, '概览'));
    const kv = el('dl', 'kv');
    refs.overviewKv = kv;
    sum.append(kv);
    body.append(sum);

    const topSec = el('section');
    body.append(topSec);
    refs.topHolder = topSec;

    refs.detailHolder = el('div');
    body.append(refs.detailHolder);
  }

  function renderOverview() {
    const model = state.model;
    const kv = state.refs.overviewKv;
    if (!kv || !model) return;
    kv.replaceChildren();
    const add = (k, v) => kv.append(el('dt', null, k), el('dd', 'mono', String(v)));
    add('总耗时', fmtDur(model.totalMs / 1000));
    add('起点', fmtClock(model.t0));
    add('终点', fmtClock(model.t1));
    add('事件', model.eventTotal);
    add('计时任务', model.bars.length);
    if (model.running.length) add('进行中', model.running.length);
  }

  /* 阶段下拉：选项集合或耗时变化时才重建 option。 */
  function renderStageOptions() {
    const model = state.model;
    const sel = state.refs.stageSel;
    if (!sel || !model) return;
    const keys = ['all', ...model.stages.map((s) => s.stage)];
    const signature = keys.join(',');
    if (state.stageSig !== signature) {
      state.stageSig = signature;
      sel.replaceChildren();
      for (const value of keys) {
        const opt = document.createElement('option');
        opt.value = value;
        sel.append(opt);
      }
    }
    for (const opt of sel.options) {
      const stage = model.stages.find((s) => s.stage === opt.value);
      opt.textContent = opt.value === 'all'
        ? '全部阶段（整轮时间轴）'
        : `${STAGE_LABEL[opt.value] || opt.value}${stage ? ` · ${fmtDur(stage.dur)}` : ''}`;
    }
    if (sel.value !== state.stage) sel.value = state.stage;
    if (state.refs.minVal) {
      state.refs.minVal.textContent = state.minMs ? `${state.minMs}ms` : '不限';
    }
  }

  function renderPanelTail() {
    const model = state.model;
    const holder = state.refs.topHolder;
    if (!holder || !model) return;
    holder.replaceChildren();
    const groups = state.attribBy === 'origin' ? model.attribOrigin : model.attribStage;
    if (!groups.length) return;
    holder.append(el('h4', null, '最耗时'));
    const list = el('div', 'list');
    for (const entry of groups.slice(0, 5)) {
      const item = el('div', 'item');
      item.append(el('div', 'item-head', entry.label));
      item.append(el('div', 'item-body', `${fmtDur(entry.sum)} · ${fmtPct(entry.sumPct)}${entry.count > 1 ? ` · ×${entry.count}` : ''}`));
      list.append(item);
    }
    holder.append(list);
  }

  /* ---------------------------------------------------------- render */
  function renderAll() {
    if (!state.refs) return;
    state.model = buildModel(ctx.events);
    renderSummary(state.model);
    renderLanes(state.model);
    renderAttrib(state.model);
    renderStageOptions();
    renderOverview();
    renderPanelTail();
    renderDetail();
    if (state.rawCount) state.rawCount.textContent = `${ctx.events.length} 条`;
  }

  function mount() {
    ctx.pager.clear();
    ctx.setStageStatus('');
    const pane = ctx.textPane;
    pane.replaceChildren();
    const root = el('div', 'tl');
    const summary = el('section', 'tl-summary');
    const lanes = el('section', 'tl-lanes');
    const attrib = el('section', 'tl-attrib');
    root.append(summary, lanes, attrib);
    pane.append(root);
    state.refs = {
      summary, lanes, attrib,
      stageSel: null, minVal: null, overviewKv: null, topHolder: null, detailHolder: null,
    };
    state.stageSig = null;
    state.selectedKey = null;
    state.rawRendered = 0;
    state.rawList = null;
    state.rawCount = null;
    mountRaw();
    mountPanel();
    renderAll();
  }

  function onEvent() {
    /* 增量事件按帧合并重画（切换 run 时 main.js 会重建视图实例）。 */
    if (state.pending) return;
    state.pending = true;
    const flush = () => {
      state.pending = false;
      if (!state.refs) return;
      renderAll();
      if (state.rawList && state.rawList.closest('details').open) renderRaw();
    };
    if (typeof requestAnimationFrame === 'function') requestAnimationFrame(flush);
    else flush();
  }

  return { mount, onEvent, onSelect: () => {}, id: 'events' };
}
