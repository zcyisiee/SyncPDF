import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClientProvider } from '@tanstack/react-query';
import { createQueryClient } from '../src/app/App';
import { queryKeys } from '../src/lib/queries';
import type { StageStateResponse } from '../src/api/types';
import { beforeEach, describe, expect, it } from 'vitest';

import { STAGE_LABELS, STAGE_NAMES } from '../src/lib/humanize';
import { WorkbenchScreen } from '../src/screens/WorkbenchScreen';
import { uiStore } from '../src/stores/ui';
import { jsonResponse, makeJob, mockApiFetch, renderWithQuery, resetUiStore } from './helpers';

const DID = 'ccs3764-dyn';
const RUN_ID = '20260916T132829Z-000183';

/** 7 阶段全 ok（真 fixture `tmp/ccs3764-dyn` 的形状：manifest 给前 5 段，run_state 给后 2 段）。 */
const STAGE_STATE: StageStateResponse = {
  did: DID,
  run_id: RUN_ID,
  stages: ([
    ['parse', 15.83],
    ['translate', 248.93],
    ['apply', 4.07],
    ['build', 23.85],
    ['check', 25.23],
    ['review', 0.32],
    ['report', 0],
  ] as const).map(([stage, duration]) => ({
    stage,
    status: 'ok',
    ok: true,
    started_at: '2026-09-16T13:28:29.000Z',
    finished_at: '2026-09-16T13:28:29.000Z',
    duration_s: duration,
    timing_source: 'manifest',
  })),
};

/** 归档事件（legacy replay：只到 check 的 stage_finished，与真 fixture 一致）。 */
const EVENTS_PAGE = {
  run_id: RUN_ID,
  events: [
    { seq: 1, at: '2026-09-16T13:28:29.373+00:00', stage: 'parse', kind: 'stage_started', data: { replayed: true } },
    { seq: 2, at: '2026-09-16T13:28:29.386+00:00', stage: 'parse', kind: 'stage_finished', data: { status: 'ok' } },
    { seq: 3, at: '2026-09-16T13:28:29.390+00:00', stage: 'translate', kind: 'text_version', data: { rows: 206 } },
    { seq: 4, at: '2026-09-16T13:28:29.391+00:00', stage: 'translate', kind: 'call_started', data: { attempt: 1 } },
    { seq: 5, at: '2026-09-16T13:28:29.400+00:00', stage: 'translate', kind: 'cache_miss', data: { key: 'p-05' } },
    { seq: 6, at: '2026-09-16T13:28:29.410+00:00', stage: 'translate', kind: 'stage_finished', data: { status: 'ok' } },
    { seq: 7, at: '2026-09-16T13:28:29.420+00:00', stage: 'apply', kind: 'stage_started', data: {} },
    { seq: 8, at: '2026-09-16T13:28:29.430+00:00', stage: 'apply', kind: 'stage_finished', data: { status: 'ok' } },
    { seq: 9, at: '2026-09-16T13:28:29.440+00:00', stage: 'build', kind: 'compile_requests', data: { count: 3 } },
    { seq: 10, at: '2026-09-16T13:28:29.450+00:00', stage: 'build', kind: 'stage_finished', data: { status: 'ok' } },
    { seq: 11, at: '2026-09-16T13:28:29.460+00:00', stage: 'check', kind: 'stage_started', data: {} },
    { seq: 12, at: '2026-09-16T13:28:29.470+00:00', stage: 'check', kind: 'stage_finished', data: { status: 'ok' } },
  ],
  next_after_seq: 12,
  has_more: false,
};

const DETAIL = {
  did: DID,
  title: null,
  pages: 21,
  paragraph_count: 420,
  translated_count: 206,
  stage_summary: {
    parse: 'ok',
    translate: 'ok',
    apply: 'ok',
    build: 'ok',
    check: 'ok',
    review: 'ok',
    report: 'ok',
  },
  updated_at: '2026-09-16T13:28:29.000Z',
  pdf: { source: null, outputs: [] },
  config: null,
  quality: {
    check: { verdict: 'pass', blockers: [], warnings: [], reasons: [], at: null },
    reviewer: { status: 'pass', fix_rounds: {}, at: null },
    pipeline_ok: true,
  },
  compile: { status: 'none', revision: 0, stale: false, artifact: null },
  available: {
    run_state: true,
    anchors: true,
    translated: true,
    geometry: true,
    parse_snapshot: true,
    review_verdict: true,
    layout_lint: true,
    link_audit: true,
  },
};

function mockDetail(did = DID, body: unknown = DETAIL, status = 200) {
  return mockApiFetch({
    [`/api/v1/documents/${did}`]: () => jsonResponse(body, status),
    [`/api/v1/documents/${did}/stage-state`]: () => jsonResponse(STAGE_STATE),
    [`/api/v1/documents/${did}/events?after_seq=0&limit=2000`]: () => jsonResponse(EVENTS_PAGE),
  });
}

/** 详情 + 产物清单（预览区要真数据：清单里没有产物 PDF 时落在占位卡上）。 */
function mockDetailAndArtifacts(artifacts: unknown = [], extra: Record<string, unknown> = {}) {
  return mockApiFetch({
    [`/api/v1/documents/${DID}`]: () => jsonResponse(DETAIL),
    [`/api/v1/documents/${DID}/artifacts`]: () => jsonResponse(artifacts),
    [`/api/v1/documents/${DID}/stage-state`]: () => jsonResponse(STAGE_STATE),
    [`/api/v1/documents/${DID}/events?after_seq=0&limit=2000`]: () => jsonResponse(EVENTS_PAGE),
    ...extra,
  });
}

/** 版本归档清单（§3.7）：两版，r3 是当前版本、r2 质量未过。 */
const VERSIONS = {
  did: DID,
  current_revision: 3,
  stale: true,
  items: [
    {
      revision: 3,
      created_at: '2026-09-17T15:54:45.123Z',
      trigger: 'debounce',
      artifact_name: 'paper.mono.pdf',
      bytes: 2048,
      sha256_head: 'a'.repeat(64),
      quality: { check_verdict: 'pass', pipeline_ok: true },
    },
    {
      revision: 2,
      created_at: '2026-09-17T15:20:03.004Z',
      trigger: 'manual',
      artifact_name: 'paper.mono.pdf',
      bytes: 1024,
      sha256_head: 'b'.repeat(64),
      quality: { check_verdict: 'needs_fix', pipeline_ok: false },
    },
  ],
};

function mockVersions(body: unknown = VERSIONS) {
  return { [`/api/v1/documents/${DID}/versions`]: () => jsonResponse(body) };
}

beforeEach(() => {
  resetUiStore();
});
describe('工作台壳（三栏 + 时间线真数据 + 事件面板）', () => {
  it('渲染 5 个视图项、视图栏文档头与真实计数', async () => {
    mockDetail();
    renderWithQuery(<WorkbenchScreen did={DID} view="progress" />);

    // 详情到达后视图栏才有计数
    await screen.findByText('206/420');
    const viewRail = screen.getByRole('navigation', { name: '视图导航' });
    const links = within(viewRail).getAllByRole('link');
    expect(links.map((link) => link.getAttribute('href'))).toEqual([
      `#/d/${DID}/progress`,
      `#/d/${DID}/layout`,
      `#/d/${DID}/translate`,
      `#/d/${DID}/check`,
      `#/d/${DID}/archive`,
    ]);
    for (const label of ['进度', '识别', '翻译', '检查', '归档']) {
      expect(within(viewRail).getByText(label)).toBeInTheDocument();
    }
    expect(within(viewRail).getByRole('link', { name: /进度/ })).toHaveAttribute(
      'aria-current',
      'page',
    );
    expect(within(viewRail).getByRole('link', { name: /进度/ })).toHaveAttribute(
      'href',
      `#/d/${DID}/progress`,
    );

    // 文档名（title 为 null 时回落 did）+ 页/段数 + 已译/段数徽标
    expect(within(viewRail).getByRole('heading', { name: DID })).toBeInTheDocument();
    expect(within(viewRail).getByText('21 页')).toBeInTheDocument();
    expect(within(viewRail).getByText('420 段')).toBeInTheDocument();
    expect(within(viewRail).getByText('206/420')).toBeInTheDocument();
  });

  it('顶栏显示当前文档名 + 阶段状态徽标，且无真实 running 时不带脉冲', async () => {
    mockDetail();
    renderWithQuery(<WorkbenchScreen did={DID} view="progress" />);

    // 顶栏 meta 依赖详情查询：先等视图栏里的真实计数出现
    await screen.findByText('206/420');
    const topbar = screen.getByRole('banner');
    expect(within(topbar).getByText(DID)).toBeInTheDocument();
    expect(within(topbar).getByText('已完成')).toBeInTheDocument();
    expect(document.querySelectorAll('.pulse-dot')).toHaveLength(0);
  });

  it('三条分隔条 role=separator + §8.2 范围与默认值', () => {
    mockDetail();
    renderWithQuery(<WorkbenchScreen did={DID} view="progress" />);

    const separators = screen.getAllByRole('separator');
    expect(separators).toHaveLength(3);
    const byName = (name: RegExp) => screen.getByRole('separator', { name });
    expect(byName(/视图栏宽度/)).toHaveAttribute('aria-valuenow', '220');
    expect(byName(/右侧面板宽度/)).toHaveAttribute('aria-valuenow', '360');
    expect(byName(/时间线高度/)).toHaveAttribute('aria-valuenow', '96');
    for (const id of ['viewrail', 'inspector', 'timeline']) {
      expect(document.querySelector(`[data-od-id="gutter-${id}"]`)).not.toBeNull();
    }
  });

  it('三栏宽度写进 CSS 变量（--vrw / --inspw / --tlh），键盘可调', async () => {
    mockDetail();
    renderWithQuery(<WorkbenchScreen did={DID} view="progress" />);
    const grid = document.querySelector('[data-od-id="workbench"]') as HTMLElement;
    expect(grid.style.getPropertyValue('--vrw')).toBe('220px');
    expect(grid.style.getPropertyValue('--inspw')).toBe('360px');
    expect(grid.style.getPropertyValue('--tlh')).toBe('96px');

    // 分隔条在预览区左侧：ArrowRight = 分隔条右移 = 视图栏变宽
    fireEvent.keyDown(screen.getByRole('separator', { name: /视图栏宽度/ }), {
      key: 'ArrowRight',
    });
    await waitFor(() => expect(grid.style.getPropertyValue('--vrw')).toBe('236px'));
    expect(uiStore.getState().viewrailWidth).toBe(236);
    expect(window.localStorage.getItem('ieet.vrw')).toBe('236');
    expect(screen.getByRole('separator', { name: /视图栏宽度/ })).toHaveAttribute(
      'aria-valuenow',
      '236',
    );
  });

  it('右侧面板折叠时 --inspw 归零，分隔条仍可拖回', async () => {
    mockDetail();
    renderWithQuery(<WorkbenchScreen did={DID} view="progress" />);
    await screen.findByText('206/420');
    expect(document.querySelector('[data-od-id="event-stream"]')).not.toBeNull();

    // 折叠态由 store 驱动（生产里由交互触发），store 变更会重渲染订阅组件
    act(() => uiStore.getState().setInspectorCollapsed(true));
    await waitFor(() =>
      expect(document.querySelector('[data-od-id="event-stream"]')).toBeNull(),
    );
    const grid = document.querySelector('[data-od-id="workbench"]') as HTMLElement;
    expect(grid.style.getPropertyValue('--inspw')).toBe('0px');

    // 分隔条在预览区右侧：ArrowLeft = 分隔条左移 = 面板变宽并展开
    fireEvent.keyDown(screen.getByRole('separator', { name: /右侧面板宽度/ }), {
      key: 'ArrowLeft',
    });
    await waitFor(() => expect(grid.style.getPropertyValue('--inspw')).toBe('376px'));
    expect(uiStore.getState().inspectorCollapsed).toBe(false);
    expect(document.querySelector('[data-od-id="event-stream"]')).not.toBeNull();
  });

  it('预览区接 W05 真预览：工具条 + 无产物占位卡', async () => {
    mockDetailAndArtifacts();
    renderWithQuery(<WorkbenchScreen did={DID} view="progress" />);
    expect(await screen.findByText('无产物 PDF')).toBeInTheDocument();
    expect(document.querySelector('[data-od-id="preview-toolbar"]')).not.toBeNull();
    expect(document.querySelector('[data-od-id="preview-no-pdf"]')).not.toBeNull();
    expect(screen.getByRole('group', { name: '预览模式' })).toBeInTheDocument();
    // 右侧面板：进度视图是 W06 事件流（段落属性面板在其它视图）
    expect(document.querySelector('[data-od-id="event-stream"]')).not.toBeNull();
    expect(await screen.findByText('事件流')).toBeInTheDocument();
  });

  it('非进度视图的右侧面板：段落编辑器 + 事件流 + 归档（W10/W12）', async () => {
    mockDetailAndArtifacts([], mockVersions());
    renderWithQuery(<WorkbenchScreen did={DID} view="translate" />);
    await screen.findByText('无产物 PDF');
    // 默认 tab = 段落（未选中段 → 空态提示）
    expect(document.querySelector('[data-od-id="paragraph-editor-empty"]')).not.toBeNull();
    expect(screen.getByText(/点击预览里的段落框查看该段/)).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: '段落' })).toHaveAttribute('aria-selected', 'true');
    // 事件流退为次要 tab：不选它就不挂事件流面板
    expect(document.querySelector('[data-od-id="event-stream"]')).toBeNull();
    fireEvent.click(screen.getByRole('tab', { name: '事件流' }));
    await waitFor(() =>
      expect(document.querySelector('[data-od-id="event-stream"]')).not.toBeNull(),
    );
    // W12 的归档摘要也是这个面板的 tab：切过去能直接看到版本摘要与「查看全部」
    fireEvent.click(screen.getByRole('tab', { name: '归档' }));
    await waitFor(() =>
      expect(document.querySelector('[data-od-id="archive-summary-count"]')?.textContent).toBe(
        '2 个版本',
      ),
    );
    expect(document.querySelector('[data-od-id="archive-summary-all"] a')).toHaveAttribute(
      'href',
      `#/d/${DID}/archive`,
    );
  });

  it('归档视图接真（W12）：版本列表 + 当前版本高亮 + 右侧面板归档摘要', async () => {
    mockDetailAndArtifacts([], mockVersions());
    renderWithQuery(<WorkbenchScreen did={DID} view="archive" />);

    // 预览区换成版本列表（不再是 W04 占位）
    expect(await screen.findByText('共 2 个版本')).toBeInTheDocument();
    expect(document.querySelector('[data-od-id="archive-panel"]')).not.toBeNull();
    expect(document.querySelector('[data-od-id="view-placeholder"]')).toBeNull();
    const items = document.querySelectorAll('[data-od-id="archive-row"]');
    expect(items).toHaveLength(2);
    expect(items[0].getAttribute('data-current')).toBe('true');
    expect(
      items[1].querySelector('[data-od-id="archive-row-download"]')?.getAttribute('href'),
    ).toBe(`/api/v1/documents/${DID}/versions/2/pdf`);

    // 右侧面板：归档摘要在前（默认 tab），带「查看全部」链接
    expect(document.querySelector('[data-od-id="archive-summary"]')).not.toBeNull();
    expect(document.querySelector('[data-od-id="archive-summary-all"] a')).toHaveAttribute(
      'href',
      `#/d/${DID}/archive`,
    );
    expect(screen.getByRole('tab', { name: '归档' })).toHaveAttribute('aria-selected', 'true');
    // 事件流退为次要 tab：不选它就不挂面板
    expect(document.querySelector('[data-od-id="event-stream"]')).toBeNull();
    fireEvent.click(screen.getByRole('tab', { name: '事件流' }));
    await waitFor(() =>
      expect(document.querySelector('[data-od-id="event-stream"]')).not.toBeNull(),
    );

    const viewRail = screen.getByRole('navigation', { name: '视图导航' });
    expect(within(viewRail).getByRole('link', { name: /归档/ })).toHaveAttribute(
      'aria-current',
      'page',
    );
  });

  it('归档视图的空态：从来没编译成功过 → 引导去翻译视图', async () => {
    mockDetailAndArtifacts(
      [],
      mockVersions({ did: DID, current_revision: 0, stale: false, items: [] }),
    );
    renderWithQuery(<WorkbenchScreen did={DID} view="archive" />);

    expect(await screen.findByText('还没有版本归档')).toBeInTheDocument();
    expect(
      document.querySelector('[data-od-id="archive-empty-cta"] a')?.getAttribute('href'),
    ).toBe(`#/d/${DID}/translate`);
  });

  it('时间线是真数据：7 段 + 真实耗时条 + 总用时 chip', async () => {
    mockDetail();
    renderWithQuery(<WorkbenchScreen did={DID} view="progress" />);
    const timeline = document.querySelector('[data-od-id="timeline"]') as HTMLElement;
    expect(timeline).not.toBeNull();
    for (const stage of STAGE_NAMES) {
      expect(within(timeline).getByText(STAGE_LABELS[stage])).toBeInTheDocument();
      expect(timeline.querySelector(`[data-od-id="timeline-stage-${stage}"]`)).not.toBeNull();
    }
    // 等 stage-state 到达：全 ok（不能用「静态占位」那一套）
    await waitFor(() =>
      expect(
        timeline.querySelector('[data-od-id="timeline-stage-parse"]')?.getAttribute('data-state'),
      ).toBe('ok'),
    );
    expect(within(timeline).getByText('15s')).toBeInTheDocument();
    expect(within(timeline).queryByText(/静态占位/)).toBeNull();
    const total = timeline.querySelector('[data-od-id="timeline-total"]');
    expect(total?.textContent).toMatch(/总用时 \d/);
    // 点击段 → 跳到该阶段对应的视图（映射表在 lib/timeline.ts）
    expect(
      timeline.querySelector('[data-od-id="timeline-stage-parse"] a')?.getAttribute('href'),
    ).toBe(`#/d/${DID}/layout`);
    expect(
      timeline.querySelector('[data-od-id="timeline-stage-check"] a')?.getAttribute('href'),
    ).toBe(`#/d/${DID}/check`);
  });

  it('did 不存在：错误卡 + 返回文件库（不留白屏）', async () => {
    mockDetail('nope', { error: { code: 'document_not_found', message: '文档不存在' } }, 404);
    renderWithQuery(<WorkbenchScreen did="nope" view="progress" />);

    const alert = await screen.findByRole('alert');
    expect(within(alert).getByText('文档不存在')).toBeInTheDocument();
    expect(within(alert).getByText(/did: nope/)).toBeInTheDocument();
    expect(within(alert).getByRole('link', { name: '返回文件库' })).toHaveAttribute(
      'href',
      '#/library',
    );
    expect(within(alert).getByRole('button', { name: '重试' })).toBeInTheDocument();
  });

  /** W08 用到的固定 mock：详情 + 有产物的清单 + 指定 job 列表 + profiles。 */
  function mockWorkbench(extra: Record<string, unknown>) {
    return mockApiFetch({
      [`/api/v1/documents/${DID}`]: () => jsonResponse(DETAIL),
      [`/api/v1/documents/${DID}/artifacts`]: () =>
        jsonResponse([
          {
            name: 'source.pdf',
            path: 'source.pdf',
            kind: 'source',
            size: 1024,
            mtime: '2026-09-16T13:28:29.000Z',
          },
        ]),
      '/api/v1/profiles': () =>
        jsonResponse([
          { id: 'echo-t', label: 'Echo T', has_translator: true, has_reviewer: false, builtin: true },
        ]),
      [`/api/v1/documents/${DID}/stage-state`]: () => jsonResponse(STAGE_STATE),
      [`/api/v1/documents/${DID}/events?after_seq=0&limit=2000`]: () => jsonResponse(EVENTS_PAGE),
      ...extra,
    });
  }

  it('进度视图顶部（W08）：无活动 job + 有产物 → StartJobCard（默认 translate）', async () => {
    mockWorkbench({ [`/api/v1/documents/${DID}/jobs`]: () => jsonResponse([]) });
    renderWithQuery(<WorkbenchScreen did={DID} view="progress" />);

    // 开始配置位于左侧视图导航，避免占用 PDF 上方的预览空间。
    expect(document.querySelector('[data-od-id="job-panel-rail"]')).not.toBeNull();
    const submit = await screen.findByRole('button', { name: '开始翻译' });
    expect(submit).toBeEnabled();
    // 已有 parse 产物（stage_state parse ok）→ 默认 translate，不提示 MinerU
    expect(((await screen.findByLabelText('起点阶段')) as HTMLSelectElement).value).toBe('translate');
    expect(screen.queryByRole('button', { name: '取消' })).toBeNull();
  });

  it('进度视图顶部（W08）：有活动 job → ActiveJobCard（取消按钮替换开始卡）', async () => {
    mockWorkbench({
      [`/api/v1/documents/${DID}/jobs`]: () =>
        jsonResponse([makeJob({ did: DID, status: 'running', profile: 'echo-t' })]),
    });
    renderWithQuery(<WorkbenchScreen did={DID} view="progress" />);

    expect(await screen.findByRole('button', { name: '取消' })).toBeInTheDocument();
    expect(document.querySelector('[data-od-id="active-job-card"]')).not.toBeNull();
    expect(screen.queryByRole('button', { name: '开始翻译' })).toBeNull();
  });

  it('重跑立即进入排队，慢查询与旧归档不覆盖新任务，随后接管当前阶段', async () => {
    const oldJob = makeJob({ did: DID, job_id: 'j_old', status: 'succeeded', run_id: RUN_ID });
    const startedAt = '2026-10-01T00:00:00.000Z';
    const newJob = makeJob({
      did: DID, job_id: 'j_new', status: 'running', from_stage: 'translate',
      created_at: startedAt, started_at: startedAt, run_id: 'new-run',
    });
    let submitted = false;
    let resolveJobs!: (response: Response) => void;
    const pendingJobs = new Promise<Response>((resolve) => { resolveJobs = resolve; });
    let stageState: StageStateResponse = STAGE_STATE;
    mockWorkbench({
      [`/api/v1/documents/${DID}/jobs`]: () => submitted ? pendingJobs : jsonResponse([oldJob]),
      [`POST /api/v1/documents/${DID}/jobs`]: () => {
        submitted = true;
        return jsonResponse({ job_id: 'j_new', status: 'queued', action: 'run' }, 202);
      },
      [`/api/v1/documents/${DID}/stage-state`]: () => jsonResponse(stageState),
    });
    const client = createQueryClient();
    render(<QueryClientProvider client={client}><WorkbenchScreen did={DID} view="progress" /></QueryClientProvider>);
    const timeline = document.querySelector('[data-od-id="timeline"]') as HTMLElement;
    await waitFor(() => expect(within(timeline).getByText('已完成')).toBeInTheDocument());
    const submit = await screen.findByRole('button', { name: '开始翻译' });
    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.click(submit);
    await waitFor(() => expect(within(timeline).getByText('排队中')).toBeInTheDocument());
    expect(document.querySelector('[data-od-id="active-job-status"]')).toHaveAttribute('data-status', 'queued');
    expect(document.querySelector('[data-od-id="timeline-stage-parse"]')).toHaveAttribute('data-state', 'ok');
    expect(document.querySelector('[data-od-id="timeline-stage-translate"]')).toHaveAttribute('data-state', 'not_run');
    expect(document.querySelector('[data-od-id="timeline-stage-build"]')).toHaveAttribute('data-state', 'not_run');
    expect(within(timeline).queryByText(/条事件/)).toBeNull();
    expect(document.querySelectorAll('[data-od-id="event-row"]')).toHaveLength(0);

    // Even a temporarily unordered list must pick the active job, not the old success.
    await act(async () => { resolveJobs(jsonResponse([oldJob, newJob])); });
    await waitFor(() => expect(document.querySelector('[data-od-id="active-job-status"]')).toHaveAttribute('data-status', 'running'));
    expect(within(timeline).getByText('翻译中')).toBeInTheDocument();
    expect(document.querySelector('[data-od-id="timeline-stage-translate"]')).toHaveAttribute('data-state', 'live');
    expect(document.querySelector('[data-od-id="timeline-stage-report"]')).toHaveAttribute('data-state', 'not_run');

    stageState = {
      ...STAGE_STATE, run_id: 'new-run',
      stages: STAGE_STATE.stages.map((item) => item.stage === 'translate'
        ? { ...item, started_at: startedAt, finished_at: '2026-10-01T00:00:01.000Z', duration_s: 1 }
        : item.stage === 'apply' ? { ...item, status: 'running', started_at: '2026-10-01T00:00:01.000Z' } : item),
    };
    await act(async () => { await client.invalidateQueries({ queryKey: queryKeys.stageState(DID) }); });
    await waitFor(() => expect(document.querySelector('[data-od-id="timeline-stage-translate"]')).toHaveAttribute('data-state', 'ok'));
    expect(document.querySelector('[data-od-id="timeline-stage-apply"]')).toHaveAttribute('data-state', 'live');
    expect(document.querySelector('[data-od-id="timeline-stage-build"]')).toHaveAttribute('data-state', 'not_run');
  });

  it('非进度视图不显示 job 面板', async () => {
    mockApiFetch({
      [`/api/v1/documents/${DID}`]: () => jsonResponse(DETAIL),
      [`/api/v1/documents/${DID}/artifacts`]: () => jsonResponse([]),
      [`/api/v1/documents/${DID}/jobs`]: () => jsonResponse([makeJob({ did: DID })]),
      [`/api/v1/documents/${DID}/stage-state`]: () => jsonResponse(STAGE_STATE),
      [`/api/v1/documents/${DID}/events?after_seq=0&limit=2000`]: () => jsonResponse(EVENTS_PAGE),
      ...mockVersions(),
    });
    renderWithQuery(<WorkbenchScreen did={DID} view="archive" />);
    await screen.findByText('共 2 个版本');
    expect(document.querySelector('[data-od-id="job-panel"]')).toBeNull();
  });
});
