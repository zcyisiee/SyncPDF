import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';

import { STAGE_LABELS, STAGE_NAMES } from '../src/lib/humanize';
import { WorkbenchScreen } from '../src/screens/WorkbenchScreen';
import { uiStore } from '../src/stores/ui';
import { jsonResponse, mockApiFetch, renderWithQuery, resetUiStore } from './helpers';

const DID = 'ccs3764-dyn';

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
  return mockApiFetch({ [`/api/v1/documents/${did}`]: () => jsonResponse(body, status) });
}

/** 详情 + 产物清单（预览区要真数据：清单里没有产物 PDF 时落在占位卡上）。 */
function mockDetailAndArtifacts(artifacts: unknown = []) {
  return mockApiFetch({
    [`/api/v1/documents/${DID}`]: () => jsonResponse(DETAIL),
    [`/api/v1/documents/${DID}/artifacts`]: () => jsonResponse(artifacts),
  });
}

beforeEach(() => {
  resetUiStore();
});

describe('工作台壳（三栏 + 时间线占位）', () => {
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
    expect(document.querySelector('[data-od-id="inspector-placeholder"]')).not.toBeNull();

    // 折叠态由 store 驱动（生产里由交互触发），store 变更会重渲染订阅组件
    act(() => uiStore.getState().setInspectorCollapsed(true));
    await waitFor(() =>
      expect(document.querySelector('[data-od-id="inspector-placeholder"]')).toBeNull(),
    );
    const grid = document.querySelector('[data-od-id="workbench"]') as HTMLElement;
    expect(grid.style.getPropertyValue('--inspw')).toBe('0px');

    // 分隔条在预览区右侧：ArrowLeft = 分隔条左移 = 面板变宽并展开
    fireEvent.keyDown(screen.getByRole('separator', { name: /右侧面板宽度/ }), {
      key: 'ArrowLeft',
    });
    await waitFor(() => expect(grid.style.getPropertyValue('--inspw')).toBe('376px'));
    expect(uiStore.getState().inspectorCollapsed).toBe(false);
    expect(document.querySelector('[data-od-id="inspector-placeholder"]')).not.toBeNull();
  });

  it('预览区接 W05 真预览：工具条 + 无产物占位卡', async () => {
    mockDetailAndArtifacts();
    renderWithQuery(<WorkbenchScreen did={DID} view="progress" />);
    expect(await screen.findByText('无产物 PDF')).toBeInTheDocument();
    expect(document.querySelector('[data-od-id="preview-toolbar"]')).not.toBeNull();
    expect(document.querySelector('[data-od-id="preview-no-pdf"]')).not.toBeNull();
    expect(screen.getByRole('group', { name: '预览模式' })).toBeInTheDocument();
    // 右侧面板：未选中段落时的说明
    expect(document.querySelector('[data-od-id="inspector-placeholder"]')).not.toBeNull();
    expect(screen.getByText(/点击预览里的段落框查看该段/)).toBeInTheDocument();
  });

  it('归档视图仍是占位（W12 接入）', async () => {
    mockDetailAndArtifacts();
    renderWithQuery(<WorkbenchScreen did={DID} view="archive" />);
    expect(await screen.findByText('归档视图待 W12 接入')).toBeInTheDocument();
    expect(document.querySelector('[data-od-id="view-placeholder"]')).not.toBeNull();
    const viewRail = screen.getByRole('navigation', { name: '视图导航' });
    expect(within(viewRail).getByRole('link', { name: /归档/ })).toHaveAttribute(
      'aria-current',
      'page',
    );
  });

  it('时间线占位：7 段名称行 + 未开始灰条，且是静态数据', async () => {
    mockDetail();
    renderWithQuery(<WorkbenchScreen did={DID} view="progress" />);
    const timeline = document.querySelector('[data-od-id="timeline"]') as HTMLElement;
    expect(timeline).not.toBeNull();
    for (const stage of STAGE_NAMES) {
      expect(within(timeline).getByText(STAGE_LABELS[stage])).toBeInTheDocument();
      expect(timeline.querySelector(`[data-od-id="timeline-stage-${stage}"]`)).not.toBeNull();
    }
    expect(within(timeline).getByText(/静态占位 · 真实事件流 W06 接入/)).toBeInTheDocument();
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
});
