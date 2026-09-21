import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClientProvider } from '@tanstack/react-query';
import { createQueryClient } from '../src/app/App';
import { queryKeys } from '../src/lib/queries';
import type { StageStateResponse } from '../src/api/types';
import { beforeEach, describe, expect, it } from 'vitest';

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
describe('工作台（中栏预览 + 右栏检查器）', () => {
  it('视图栏已删除：预览区占满整行，右侧面板三个 tab 常驻（段落/事件流/归档）', async () => {
    mockDetail();
    renderWithQuery(<WorkbenchScreen did={DID} view="progress" />);

    await screen.findByText(DID);
    // 旧版的「视图导航」栏不再存在（二级视图合并成同一个工作台）
    expect(screen.queryByRole('navigation', { name: '视图导航' })).toBeNull();
    expect(screen.queryByRole('link', { name: '识别' })).toBeNull();

    // 右侧面板：一行头部 = 3 个 tab + 已选中段落位（未选中时为空）
    const tablist = screen.getByRole('tablist', { name: '右侧面板' });
    for (const label of ['段落', '事件流', '归档']) {
      expect(within(tablist).getByRole('tab', { name: label })).toBeInTheDocument();
    }
    expect(screen.getByRole('tab', { name: '段落' })).toHaveAttribute('aria-selected', 'true');
    expect(document.querySelector('[data-od-id="selected-paragraph-id"]')).toBeNull();
  });

  it('文档名 + 阶段徽标住工具条最左，任务控制住右栏操作区（临时文档头行已删除，无真实 running 不带脉冲）', async () => {
    mockApiFetch({
      [`/api/v1/documents/${DID}`]: () => jsonResponse(DETAIL),
      [`/api/v1/documents/${DID}/artifacts`]: () => jsonResponse([]),
      '/api/v1/profiles': () => jsonResponse([]),
      [`/api/v1/documents/${DID}/stage-state`]: () => jsonResponse(STAGE_STATE),
      [`/api/v1/documents/${DID}/events?after_seq=0&limit=2000`]: () => jsonResponse(EVENTS_PAGE),
    });
    renderWithQuery(<WorkbenchScreen did={DID} view="progress" />);

    // 中栏只有工具条 + 画布：T2 的临时文档头行已被溶解
    expect(document.querySelector('[data-od-id="doc-header"]')).toBeNull();
    const toolbar = screen.getByRole('toolbar', { name: '预览工具条' });
    const title = document.querySelector('[data-od-id="toolbar-title"]') as HTMLElement;
    // 标题是 did（详情里 title=null）
    expect(title.textContent).toBe(DID);
    // 详情到达前 `stage_summary` 还是空 → 徽标先说「未运行」；等「已完成」出现说明 doc 到了
    expect(await within(toolbar).findByText('已完成')).toBeInTheDocument();
    expect(document.querySelector('[data-od-id="toolbar-status"]')).not.toBeNull();
    expect(document.querySelectorAll('.pulse-dot')).toHaveLength(0);
    // 任务控制收进右栏底部操作区（所有 tab 都常驻的唯一入口）
    const actionbar = document.querySelector('[data-od-id="action-bar"]') as HTMLElement;
    expect(await within(actionbar).findByRole('button', { name: '开始翻译' })).toBeInTheDocument();
    expect(
      actionbar.querySelector('[data-od-id="job-controls"] [data-od-id="start-job-submit"]'),
    ).not.toBeNull();
    expect(actionbar.querySelector('[data-od-id="compile-full"]')).not.toBeNull();
    // 草稿还没到 → 编译全文 不可点（拿不到 `base_revision`）
    expect(within(actionbar).getByRole('button', { name: '编译全文' })).toBeDisabled();
    // 旧外壳（顶栏/图标栏/时间线）彻底不在了
    expect(screen.queryByRole('banner')).toBeNull();
    expect(document.querySelector('[data-od-id="app-topbar"]')).toBeNull();
    expect(document.querySelector('[data-od-id="icon-rail"]')).toBeNull();
    expect(document.querySelector('[data-od-id="timeline"]')).toBeNull();
  });

  it('操作区：文档详情未到时给占位（不拿半个 DocumentDetail 硬渲染）', async () => {
    mockApiFetch({
      // 详情永远不 resolve（首屏加载中）：其余路由照常
      [`/api/v1/documents/${DID}`]: () => new Promise<Response>(() => {}),
      [`/api/v1/documents/${DID}/stage-state`]: () => jsonResponse(STAGE_STATE),
      [`/api/v1/documents/${DID}/events?after_seq=0&limit=2000`]: () => jsonResponse(EVENTS_PAGE),
    });
    renderWithQuery(<WorkbenchScreen did={DID} view="progress" />);

    const actionbar = document.querySelector('[data-od-id="action-bar"]') as HTMLElement;
    expect(within(actionbar).getByText('读取文档…')).toBeInTheDocument();
    expect(actionbar.querySelector('[data-od-id="job-controls"]')).toBeNull();
    // 工具条：标题回退到 did，状态徽标还挂得上（`stage_summary` undefined → 未运行）
    expect(document.querySelector('[data-od-id="toolbar-title"]')?.textContent).toBe(DID);
    expect(document.querySelector('[data-od-id="toolbar-status"]')?.textContent).toBe('未运行');
  });

  it('一条分隔条（右栏）+ 中栏预览占满其余宽度（时间线分隔条已删除）', () => {
    mockDetail();
    renderWithQuery(<WorkbenchScreen did={DID} view="progress" />);

    const separators = screen.getAllByRole('separator');
    expect(separators).toHaveLength(1);
    const gutter = screen.getByRole('separator', { name: /右侧面板宽度/ });
    expect(gutter).toHaveAttribute('aria-valuenow', '340');
    expect(document.querySelector('[data-od-id="gutter-inspector"]')).not.toBeNull();
    expect(document.querySelector('[data-od-id="gutter-timeline"]')).toBeNull();
    expect(document.querySelector('[data-od-id="gutter-nav"]')).toBeNull();
  });

  it('栏宽写进 CSS 变量（--inspw），键盘可调', async () => {
    mockDetail();
    renderWithQuery(<WorkbenchScreen did={DID} view="progress" />);
    const grid = document.querySelector('[data-od-id="workbench"]') as HTMLElement;
    expect(grid.style.getPropertyValue('--inspw')).toBe('340px');

    // 右侧面板分隔条：ArrowLeft = 分隔条左移 = 面板变宽
    fireEvent.keyDown(screen.getByRole('separator', { name: /右侧面板宽度/ }), {
      key: 'ArrowLeft',
    });
    await waitFor(() => expect(grid.style.getPropertyValue('--inspw')).toBe('356px'));
    expect(uiStore.getState().inspectorWidth).toBe(356);
    expect(window.localStorage.getItem('ieet.inspw')).toBe('356');
    expect(screen.getByRole('separator', { name: /右侧面板宽度/ })).toHaveAttribute(
      'aria-valuenow',
      '356',
    );
  });

  it('预览区接 W05 真预览：工具条 + 无产物占位卡', async () => {
    mockDetailAndArtifacts();
    renderWithQuery(<WorkbenchScreen did={DID} view="progress" />);
    expect(await screen.findByText('无产物 PDF')).toBeInTheDocument();
    expect(document.querySelector('[data-od-id="preview-toolbar"]')).not.toBeNull();
    expect(document.querySelector('[data-od-id="preview-no-pdf"]')).not.toBeNull();
    expect(screen.getByRole('group', { name: '预览模式' })).toBeInTheDocument();
    // 右侧面板默认 tab = 段落；事件流是常驻 tab，切过去能看到
    expect(screen.getByRole('tab', { name: '段落' })).toHaveAttribute('aria-selected', 'true');
    expect(document.querySelector('[data-od-id="event-stream"]')).toBeNull();
    fireEvent.click(screen.getByRole('tab', { name: '事件流' }));
    await waitFor(() =>
      expect(document.querySelector('[data-od-id="event-stream"]')).not.toBeNull(),
    );
  });

  it('工作台（旧 translate 链接也落到这里）的右侧面板：段落编辑器 + 事件流 + 归档（W10/W12）', async () => {
    mockDetailAndArtifacts([], mockVersions());
    renderWithQuery(<WorkbenchScreen did={DID} view="progress" />);
    await screen.findByText('无产物 PDF');
    // 默认 tab = 段落（未选中段 → 空态提示）
    expect(document.querySelector('[data-od-id="paragraph-editor-empty"]')).not.toBeNull();
    expect(screen.getByText(/点击预览里的原文框查看该段/)).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: '段落' })).toHaveAttribute('aria-selected', 'true');
    // 事件流退为次要 tab：不选它就不挂事件流面板
    expect(document.querySelector('[data-od-id="event-stream"]')).toBeNull();
    fireEvent.click(screen.getByRole('tab', { name: '事件流' }));
    await waitFor(() =>
      expect(document.querySelector('[data-od-id="event-stream"]')).not.toBeNull(),
    );
    // 归档也是这个面板的 tab：切过去直接看到导出/下载入口 + 摘要 + 完整版本列表
    fireEvent.click(screen.getByRole('tab', { name: '归档' }));
    await waitFor(() =>
      expect(document.querySelector('[data-od-id="archive-summary-count"]')?.textContent).toBe(
        '2 个版本',
      ),
    );
    const archiveTab = document.querySelector('[data-od-id="archive-tab"]') as HTMLElement;
    expect(archiveTab).not.toBeNull();
    expect(
      await screen.findByRole('button', { name: /导出最新 PDF/ }),
    ).toBeInTheDocument();
    expect(archiveTab.querySelector('[data-od-id="download-button"]')).not.toBeNull();
    expect(archiveTab.querySelectorAll('[data-od-id="archive-row"]')).toHaveLength(2);
    expect(document.querySelector('[data-od-id="archive-summary-all"] a')).toHaveAttribute(
      'href',
      `#/d/${DID}/archive`,
    );
    // 中栏还是预览区（不再变成版本列表）
    expect(document.querySelector('[data-od-id="preview-toolbar"]')).not.toBeNull();
    expect(document.querySelector('[data-od-id="archive-panel"]')).toBeNull();
  });

  it('view=archive 仍解析，但中栏恒为预览区；右栏默认选中归档 tab（版本+导出入口）', async () => {
    mockDetailAndArtifacts([], mockVersions());
    renderWithQuery(<WorkbenchScreen did={DID} view="archive" />);

    // 中栏不再换成版本列表：预览区在（无产物 → 占位卡），archive-panel 分支已删
    expect(await screen.findByText('无产物 PDF')).toBeInTheDocument();
    expect(document.querySelector('[data-od-id="archive-panel"]')).toBeNull();
    expect(document.querySelector('[data-od-id="preview-toolbar"]')).not.toBeNull();

    // 右栏：归档 tab 默认选中，摘要 + 版本列表都在这里
    expect(screen.getByRole('tab', { name: '归档' })).toHaveAttribute('aria-selected', 'true');
    const archiveTab = document.querySelector('[data-od-id="archive-tab"]') as HTMLElement;
    expect(archiveTab).not.toBeNull();
    expect(archiveTab.querySelector('[data-od-id="archive-summary"]')).not.toBeNull();
    expect(archiveTab.querySelector('[data-od-id="archive-summary-all"] a')).toHaveAttribute(
      'href',
      `#/d/${DID}/archive`,
    );
    await waitFor(() =>
      expect(archiveTab.querySelectorAll('[data-od-id="archive-row"]')).toHaveLength(2),
    );
    expect(
      archiveTab
        .querySelectorAll('[data-od-id="archive-row"]')[1]
        .querySelector('[data-od-id="archive-row-download"]')
        ?.getAttribute('href'),
    ).toBe(`/api/v1/documents/${DID}/versions/2/pdf`);
    // 事件流退为次要 tab：不选它就不挂面板
    expect(document.querySelector('[data-od-id="event-stream"]')).toBeNull();
    fireEvent.click(screen.getByRole('tab', { name: '事件流' }));
    await waitFor(() =>
      expect(document.querySelector('[data-od-id="event-stream"]')).not.toBeNull(),
    );
  });

  it('归档 tab 的空态：从来没编译成功过 → 「还没有全量编译版本」（不是错误，也不冒充有版本）', async () => {
    mockDetailAndArtifacts(
      [],
      mockVersions({ did: DID, current_revision: 0, stale: false, items: [] }),
    );
    renderWithQuery(<WorkbenchScreen did={DID} view="archive" />);

    expect(await screen.findByText('还没有全量编译版本')).toBeInTheDocument();
    expect(document.querySelector('[data-od-id="archive-tab"]')).not.toBeNull();
    expect(document.querySelectorAll('[data-od-id="archive-row"]')).toHaveLength(0);
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

  it('操作区（W08）：无活动 job + 有产物 → 「开始翻译」可用；工作台不再有配置表单', async () => {
    mockWorkbench({ [`/api/v1/documents/${DID}/jobs`]: () => jsonResponse([]) });
    renderWithQuery(<WorkbenchScreen did={DID} view="progress" />);

    // 任务控制收进右栏底部操作区；翻译配置（模型/思考/dual/词表/审校）在设置屏，不在工作台重复展示
    const actionbar = document.querySelector('[data-od-id="action-bar"]') as HTMLElement;
    const submit = await within(actionbar).findByRole('button', { name: '开始翻译' });
    await waitFor(() => expect(submit).toBeEnabled());
    expect(screen.queryByLabelText('起点阶段')).toBeNull();
    expect(screen.queryByLabelText(/页码范围/)).toBeNull();
    expect(screen.queryByRole('button', { name: '取消' })).toBeNull();
  });

  it('操作区（W08）：有活动 job → 徽标 + 取消按钮替换「开始翻译」', async () => {
    mockWorkbench({
      [`/api/v1/documents/${DID}/jobs`]: () =>
        jsonResponse([makeJob({ did: DID, status: 'running', profile: 'echo-t' })]),
    });
    renderWithQuery(<WorkbenchScreen did={DID} view="progress" />);

    const actionbar = document.querySelector('[data-od-id="action-bar"]') as HTMLElement;
    expect(await within(actionbar).findByRole('button', { name: '取消' })).toBeInTheDocument();
    expect(document.querySelector('[data-od-id="active-job-status"]')).toHaveAttribute('data-status', 'running');
    expect(within(actionbar).queryByRole('button', { name: '开始翻译' })).toBeNull();
  });

  it('操作区（W08）：最近一次失败 → 徽标 + 重试（同参数再发一个新 job）', async () => {
    const fetchMock = mockWorkbench({
      [`/api/v1/documents/${DID}/jobs`]: () =>
        jsonResponse([
          makeJob({
            did: DID, job_id: 'j_fail', status: 'failed', error_code: 'translator_crash',
            error_message: '模型超时', profile: 'echo-t', dual: true, use_glossary: true,
          }),
        ]),
      [`POST /api/v1/documents/${DID}/jobs`]: () =>
        jsonResponse({ job_id: 'j_new', status: 'queued', action: 'run' }, 202),
    });
    renderWithQuery(<WorkbenchScreen did={DID} view="progress" />);

    const actionbar = document.querySelector('[data-od-id="action-bar"]') as HTMLElement;
    expect(await within(actionbar).findByRole('button', { name: '重试' })).toBeInTheDocument();
    fireEvent.click(within(actionbar).getByRole('button', { name: '重试' }));
    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(
          (call) => String(call[0]).endsWith(`/documents/${DID}/jobs`) && call[1]?.method === 'POST',
        ),
      ).toBe(true),
    );
    const submit = fetchMock.mock.calls.find(
      (call) => String(call[0]).endsWith(`/documents/${DID}/jobs`) && call[1]?.method === 'POST',
    );
    expect(JSON.parse(String(submit?.[1]?.body))).toMatchObject({
      action: 'run', from: 'translate', profile: 'echo-t', dual: true, use_glossary: true,
    });
  });

  it('操作区右侧「编译全文」：按当前草稿 revision 发 scope=full 的 compile job，活动 job 期间禁用', async () => {
    const fetchMock = mockWorkbench({
      [`/api/v1/documents/${DID}/jobs`]: () => jsonResponse([]),
      [`/api/v1/documents/${DID}/draft`]: () =>
        jsonResponse({ revision: 7, updated_at: null, paragraphs: {} }),
      [`POST /api/v1/documents/${DID}/jobs`]: () =>
        jsonResponse({ job_id: 'j_compile', status: 'queued', action: 'compile' }, 202),
    });
    renderWithQuery(<WorkbenchScreen did={DID} view="progress" />);

    const actionbar = document.querySelector('[data-od-id="action-bar"]') as HTMLElement;
    const compileFull = await within(actionbar).findByRole('button', { name: '编译全文' });
    await waitFor(() => expect(compileFull).toBeEnabled());
    expect(compileFull).toHaveAttribute('title', expect.stringContaining('scope=full'));
    fireEvent.click(compileFull);
    const submit = await waitFor(() => {
      const call = fetchMock.mock.calls.find(
        (item) => String(item[0]).endsWith(`/documents/${DID}/jobs`) && item[1]?.method === 'POST',
      );
      expect(call).toBeDefined();
      return call as [RequestInfo | URL, RequestInit];
    });
    // 草稿 revision 就是编译的 base：不是段落级编译、不带 from/pages
    expect(JSON.parse(String(submit[1].body))).toMatchObject({
      action: 'compile', scope: 'full', base_revision: 7,
    });
  });

  it('操作区右侧「编译全文」：有活动 job 时禁用（服务端会 409 document_busy）', async () => {
    mockWorkbench({
      [`/api/v1/documents/${DID}/jobs`]: () =>
        jsonResponse([makeJob({ did: DID, status: 'running', profile: 'echo-t' })]),
      [`/api/v1/documents/${DID}/draft`]: () =>
        jsonResponse({ revision: 7, updated_at: null, paragraphs: {} }),
    });
    renderWithQuery(<WorkbenchScreen did={DID} view="progress" />);

    const actionbar = document.querySelector('[data-od-id="action-bar"]') as HTMLElement;
    // 任务控制自己说「取消」；编译全文不可点，不产生必然 409 的请求
    expect(await within(actionbar).findByRole('button', { name: '取消' })).toBeInTheDocument();
    await waitFor(() =>
      expect(within(actionbar).getByRole('button', { name: '编译全文' })).toBeDisabled(),
    );
  });

  it('操作区的「编译全文」提交失败：行内红字给可读原因（不静默）', async () => {
    mockWorkbench({
      [`/api/v1/documents/${DID}/jobs`]: () => jsonResponse([]),
      [`/api/v1/documents/${DID}/draft`]: () =>
        jsonResponse({ revision: 3, updated_at: null, paragraphs: {} }),
      [`POST /api/v1/documents/${DID}/jobs`]: () =>
        jsonResponse({ error: { code: 'document_busy', message: '这个文档已有任务在跑' } }, 409),
    });
    renderWithQuery(<WorkbenchScreen did={DID} view="progress" />);

    const compileFull = await screen.findByRole('button', { name: '编译全文' });
    await waitFor(() => expect(compileFull).toBeEnabled());
    fireEvent.click(compileFull);
    const error = await waitFor(() => {
      const node = document.querySelector('[data-od-id="compile-full-error"]');
      expect(node).not.toBeNull();
      return node as HTMLElement;
    });
    expect(error.textContent).toContain('这个文档已有任务在跑');
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
    const badge = document.querySelector('[data-od-id="toolbar-status"]') as HTMLElement;
    // 基线：全 ok → 工具条徽标「已完成」（不靠假占位）
    await waitFor(() => expect(within(badge).getByText('已完成')).toBeInTheDocument());
    const submit = await screen.findByRole('button', { name: '开始翻译' });
    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.click(submit);

    // 提交后立即进入排队：操作区的 job 徽标说「排队中」（不等慢查询返回/旧归档）
    await waitFor(() =>
      expect(document.querySelector('[data-od-id="active-job-status"]')).toHaveAttribute(
        'data-status',
        'queued',
      ),
    );
    expect(screen.getByText('排队中')).toBeInTheDocument();
    // 工具条徽标只读 `stage_summary`（这里一直全 ok）：任务状态由操作区承载，两处不抢同一句话
    expect(within(badge).getByText('已完成')).toBeInTheDocument();
    // 事件面板在段落 tab 下不挂载（要看事件流就切 tab）
    expect(document.querySelectorAll('[data-od-id="event-row"]')).toHaveLength(0);

    // 慢查询返回时列表乱序也必须选活动 job，而不是上一条成功记录
    await act(async () => { resolveJobs(jsonResponse([oldJob, newJob])); });
    await waitFor(() =>
      expect(document.querySelector('[data-od-id="active-job-status"]')).toHaveAttribute(
        'data-status',
        'running',
      ),
    );
    // job 的 `from_stage` 进徽标文案（stage-state 还没落盘时，阶段名只能来自 job 记录）
    await waitFor(() => expect(screen.getByText('运行中 · 翻译')).toBeInTheDocument());

    // 新 run 的 stage-state 落盘：job 仍是活动源（徽标不被旧 run 的阶段覆写）
    stageState = {
      ...STAGE_STATE, run_id: 'new-run',
      stages: STAGE_STATE.stages.map((item) => item.stage === 'translate'
        ? { ...item, started_at: startedAt, finished_at: '2026-10-01T00:00:01.000Z', duration_s: 1 }
        : item.stage === 'apply' ? { ...item, status: 'running', started_at: '2026-10-01T00:00:01.000Z' } : item),
    };
    await act(async () => { await client.invalidateQueries({ queryKey: queryKeys.stageState(DID) }); });
    expect(screen.getByText('运行中 · 翻译')).toBeInTheDocument();
    expect(document.querySelector('[data-od-id="active-job-status"]')).toHaveAttribute(
      'data-status',
      'running',
    );
  });

  it('view=archive：任务控制在右栏操作区（所有视图一致）；中栏仍是预览区', async () => {
    mockApiFetch({
      [`/api/v1/documents/${DID}`]: () => jsonResponse(DETAIL),
      [`/api/v1/documents/${DID}/artifacts`]: () => jsonResponse([]),
      [`/api/v1/documents/${DID}/jobs`]: () => jsonResponse([makeJob({ did: DID })]),
      [`/api/v1/documents/${DID}/stage-state`]: () => jsonResponse(STAGE_STATE),
      [`/api/v1/documents/${DID}/events?after_seq=0&limit=2000`]: () => jsonResponse(EVENTS_PAGE),
      ...mockVersions(),
    });
    renderWithQuery(<WorkbenchScreen did={DID} view="archive" />);
    await screen.findByText('无产物 PDF');
    // 旧版「进度视图才有 job 面板」的区分已删除：预览区没有 job 面板，任务控制在右栏操作区
    expect(document.querySelector('[data-od-id="job-panel-rail"]')).toBeNull();
    expect(
      document.querySelector('[data-od-id="action-bar"] [data-od-id="job-controls"]'),
    ).not.toBeNull();
    expect(document.querySelector('[data-od-id="doc-header"]')).toBeNull();
    // 归档 tab 里能看到版本行（旧链接带来的 archive 视图只是默认选中该 tab）
    const archiveTab = document.querySelector('[data-od-id="archive-tab"]') as HTMLElement;
    await waitFor(() =>
      expect(archiveTab.querySelectorAll('[data-od-id="archive-row"]')).toHaveLength(2),
    );
  });
});
