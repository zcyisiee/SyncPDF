/**
 * 版本归档（api.md §3.7，W12）：判定表（`lib/versions.ts`）+ 归档 tab 渲染
 * （`VersionList` 的版本卡 + `InspectorPanel` 的导出/下载入口与摘要）。
 *
 * 断言口径与后端/契约一致：
 * - 清单**新 → 旧**；`current_revision` 那一版标「当前版本」（不是"清单最新行"）；
 * - `stale=true` → 摘要里的显式提示条（草稿有未编译修改），绝不把旧版本说成最新；
 * - 质量徽标**复用 W10 判定表**：`pipeline_ok=true` 绿、`needs_fix` 黄，且**不**禁用下载；
 * - 下载 href = `/api/v1/documents/{did}/versions/<r>/pdf`，落盘名 = `<名去 .pdf>.r<r>.pdf`；
 * - 从没编译成功过 → 「还没有全量编译版本」，不是错误；
 * - 归档 tab 是**版本与导出的唯一入口**：中栏工具条上没有下载槽（用户决策 E）。
 */
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { VersionItem, VersionsResponse } from '../src/api/types';
import { ArchiveSummary } from '../src/components/archive/ArchiveSummary';
import { VersionList } from '../src/components/archive/VersionList';
import { InspectorPanel } from '../src/components/shell/InspectorPanel';
import { qualityBadge } from '../src/lib/download';
import {
  archiveHash,
  archiveSummary,
  triggerLabel,
  versionQualityBadge,
  versionPdfUrl,
  versionRows,
} from '../src/lib/versions';
import { jsonResponse, makeEventFeed, mockApiFetch, renderWithQuery } from './helpers';

const DID = 'paper';
const VERSIONS_PATH = `/api/v1/documents/${DID}/versions`;
const COMPILE = { status: 'ok', revision: 3, stale: false, artifact: null };

function item(overrides: Partial<VersionItem> = {}): VersionItem {
  return {
    revision: 3,
    created_at: '2026-09-17T15:54:45.123Z',
    trigger: 'debounce',
    artifact_name: 'paper.mono.pdf',
    bytes: 2048,
    sha256_head: 'a'.repeat(64),
    quality: { check_verdict: 'pass', pipeline_ok: true },
    ...overrides,
  };
}

const RESPONSE: VersionsResponse = {
  did: DID,
  current_revision: 3,
  stale: false,
  items: [
    item({ revision: 3, trigger: 'debounce' }),
    item({
      revision: 2,
      created_at: '2026-09-17T15:20:03.004Z',
      trigger: 'manual',
      bytes: 1024,
      quality: { check_verdict: 'needs_fix', pipeline_ok: false },
    }),
  ],
};

/** 归档 tab 里那两行版本卡（`data-od-id="archive-row"`，新 → 旧）。 */
function rows(scope: Document | HTMLElement = document) {
  return Array.from(scope.querySelectorAll('[data-od-id="archive-row"]'));
}

/** 详情：`compile` 指向 r3（当前可下载的那一版），质量通过。 */
const DETAIL = {
  did: DID,
  title: null,
  pages: 21,
  paragraph_count: 420,
  translated_count: 206,
  stage_summary: {},
  updated_at: '2026-09-17T15:54:45.123Z',
  pdf: { source: null, outputs: [] },
  config: null,
  quality: {
    check: { verdict: 'pass', blockers: [], warnings: [] },
    reviewer: { status: 'pass', fix_rounds: {} },
    pipeline_ok: true,
  },
  compile: {
    status: 'ok',
    revision: 3,
    stale: false,
    artifact: { name: 'paper.mono.pdf', revision: 3, size: 2048 },
  },
  available: {},
};

// --------------------------------------------------------------------------- #
// 判定表（纯函数）
// --------------------------------------------------------------------------- #
describe('lib/versions 判定表', () => {
  it('triggerLabel：两个已知取值给中文 + tooltip，未知值原样显示', () => {
    expect(triggerLabel('debounce').text).toBe('自动');
    expect(triggerLabel('manual').text).toBe('手动');
    expect(triggerLabel('debounce').title).toContain('防抖');
    expect(triggerLabel(null).text).toBe('—');
    expect(triggerLabel('cron').text).toBe('cron');
  });

  it('versionPdfUrl / archiveHash 是唯二的 URL 拼装处', () => {
    expect(versionPdfUrl('paper x', 7)).toBe('/api/v1/documents/paper%20x/versions/7/pdf');
    expect(archiveHash('paper x')).toBe('#/d/paper%20x/archive');
  });

  it('versionQualityBadge 复用 W10 的 qualityBadge（同输入 → 同输出）', () => {
    const pass = { check_verdict: 'pass', pipeline_ok: true };
    const needsFix = { check_verdict: 'needs_fix', pipeline_ok: false };
    expect(versionQualityBadge(pass)).toEqual(
      qualityBadge({ check: { verdict: 'pass' }, pipeline_ok: true }),
    );
    expect(versionQualityBadge(needsFix)).toEqual(
      qualityBadge({ check: { verdict: 'needs_fix' }, pipeline_ok: false }),
    );
    expect(versionQualityBadge(needsFix).tone).toBe('run');
    expect(versionQualityBadge(needsFix).label).toBe('检查未通过');
  });

  it('versionRows：保持服务端顺序、标出当前版本、给下载名', () => {
    const built = versionRows(DID, RESPONSE);
    expect(built.map((row) => row.revision)).toEqual([3, 2]);
    expect(built.map((row) => row.current)).toEqual([true, false]);
    expect(built[1].href).toBe(`/api/v1/documents/${DID}/versions/2/pdf`);
    expect(built[1].fileName).toBe('paper.mono.r2.pdf');
    expect(built[0].sizeLabel).toBe('2 KB');
    // 没有清单（还在加载/失败）→ 空数组，不猜
    expect(versionRows(DID, undefined)).toEqual([]);
  });

  it('archiveSummary：条数 / 当前版本 / stale / 最新一版', () => {
    expect(archiveSummary(DID, RESPONSE)).toMatchObject({
      count: 2,
      currentRevision: 3,
      stale: false,
      empty: false,
    });
    expect(archiveSummary(DID, { ...RESPONSE, stale: true }).stale).toBe(true);
    expect(archiveSummary(DID, { did: DID, current_revision: 0, stale: false, items: [] })).toMatchObject(
      { count: 0, latest: null, empty: true },
    );
  });
});

// --------------------------------------------------------------------------- #
// 版本列表
// --------------------------------------------------------------------------- #
function mockVersions(body: unknown = RESPONSE, status = 200) {
  return mockApiFetch({ [VERSIONS_PATH]: () => jsonResponse(body, status) });
}

describe('VersionList（版本列表 + 当前高亮 + 下载）', () => {
  it('列出全部版本（新 → 旧），当前版本高亮并带「当前版本」标记', async () => {
    mockVersions();
    renderWithQuery(<VersionList did={DID} compile={COMPILE} />);

    await screen.findByText('下载 r3');
    const items = rows();
    expect(items.map((row) => row.getAttribute('data-revision'))).toEqual(['3', '2']);
    expect(items.map((row) => row.getAttribute('data-current'))).toEqual(['true', 'false']);
    // 当前那一行有显式标记，旧版本没有（不许把旧版本说成最新）
    expect(items[0].textContent).toContain('当前版本');
    expect(items[1].textContent).not.toContain('当前版本');
    // 时间（绝对时刻在 title 里）+ 大小 + 触发原因
    expect(items[0].querySelector('[data-od-id="archive-row-time"]')?.getAttribute('title')).toBe(
      '2026-09-17T15:54:45.123Z',
    );
    expect(items[0].querySelector('[data-od-id="archive-row-trigger"]')?.textContent).toBe('自动');
    expect(items[1].querySelector('[data-od-id="archive-row-trigger"]')?.textContent).toBe('手动');
    expect(items[1].querySelector('[data-od-id="archive-row-size"]')?.textContent).toBe('1 KB');
  });

  it('质量徽标复用 W10 判定表，且 needs_fix 不禁用下载（只记录不门禁）', async () => {
    mockVersions();
    renderWithQuery(<VersionList did={DID} compile={COMPILE} />);
    await screen.findByText('下载 r3');

    const items = rows();
    expect(items[0].querySelector('[data-od-id="archive-row-quality"]')?.textContent).toBe(
      '检查通过',
    );
    expect(items[1].querySelector('[data-od-id="archive-row-quality"]')?.textContent).toBe(
      '检查未通过',
    );
    const needsFixDownload = items[1].querySelector('[data-od-id="archive-row-download"]');
    expect(needsFixDownload).not.toBeNull();
    expect(needsFixDownload?.getAttribute('href')).toBe(
      `/api/v1/documents/${DID}/versions/2/pdf`,
    );
    expect(needsFixDownload?.getAttribute('download')).toBe('paper.mono.r2.pdf');
  });

  it('stale 提示不由列表重复：列表只列版本，提示归摘要', async () => {
    mockVersions({ ...RESPONSE, stale: true });
    renderWithQuery(<VersionList did={DID} compile={{ ...COMPILE, stale: true }} />);
    await screen.findByText('下载 r3');
    expect(document.querySelector('[data-od-id="archive-stale"]')).toBeNull();
  });

  it('空态：从没编译成功过 → 「还没有全量编译版本」（不是错误，也不给跳转 CTA）', async () => {
    mockVersions({ did: DID, current_revision: 0, stale: false, items: [] });
    renderWithQuery(<VersionList did={DID} compile={null} />);

    expect(await screen.findByText('还没有全量编译版本')).toBeInTheDocument();
    expect(document.querySelector('[data-od-id="archive-empty"]')).not.toBeNull();
    expect(rows()).toEqual([]);
    expect(document.querySelector('[data-od-id="archive-error"]')).toBeNull();
  });

  it('请求失败 → 错误卡 + 重试（不留白屏）', async () => {
    mockVersions({ error: { code: 'document_not_found', message: '文档不存在' } }, 404);
    renderWithQuery(<VersionList did={DID} compile={COMPILE} />);
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.getByText('文档不存在')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '重试' })).toBeInTheDocument();
  });

  it('载入中：给占位文案，不显示空态（避免"还没读完"被当成"没有版本"）', () => {
    mockApiFetch({ [VERSIONS_PATH]: () => new Promise<Response>(() => {}) });
    renderWithQuery(<VersionList did={DID} compile={COMPILE} />);
    expect(document.querySelector('[data-od-id="archive-loading"]')).not.toBeNull();
    expect(document.querySelector('[data-od-id="archive-empty"]')).toBeNull();
  });
});

// --------------------------------------------------------------------------- #
// 归档 tab（InspectorPanel 的第三个 tab）
// --------------------------------------------------------------------------- #
function mockArchiveTab(body: unknown = RESPONSE) {
  return mockApiFetch({
    [VERSIONS_PATH]: () => jsonResponse(body),
    [`/api/v1/documents/${DID}`]: () => jsonResponse(DETAIL),
    [`/api/v1/documents/${DID}/jobs`]: () => jsonResponse([]),
    [`/api/v1/documents/${DID}/draft`]: () =>
      jsonResponse({ revision: 3, updated_at: null, paragraphs: {} }),
    '/api/v1/profiles': () => jsonResponse([]),
  });
}

describe('归档 tab：导出/下载入口 + 摘要 + 版本列表', () => {
  it('独占版本与导出入口：导出最新 PDF / 下载 PDF / 摘要 / 两行版本卡', async () => {
    mockArchiveTab();
    renderWithQuery(
      <InspectorPanel did={DID} view="archive" feed={makeEventFeed()} compileEvents={[]} />,
    );

    const tab = (await screen.findByRole('tabpanel', { name: '归档' })) as HTMLElement;
    expect(tab).toHaveAttribute('data-od-id', 'archive-tab');
    // 导出/下载都在这一 tab 顶部（下载按钮带修订号 + 质量徽标，规则不变）
    const actions = tab.querySelector('[data-od-id="archive-actions"]') as HTMLElement;
    expect(actions).not.toBeNull();
    expect(
      await screen.findByRole('button', { name: /导出最新 PDF/ }),
    ).toBeInTheDocument();
    const download = (await waitFor(() => {
      const node = actions.querySelector('[data-od-id="download-button"]');
      expect(node?.getAttribute('data-enabled')).toBe('true');
      return node;
    })) as HTMLAnchorElement;
    expect(download.getAttribute('href')).toBe(
      `/api/v1/documents/${DID}/artifacts/output/paper.mono.pdf?r=3`,
    );
    expect(download.getAttribute('download')).toBe('paper.mono.r3.pdf');

    // 摘要 + 完整版本列表（同一个 tab 内，数据同一份）
    expect(tab.querySelector('[data-od-id="archive-summary-count"]')?.textContent).toBe(
      '2 个版本',
    );
    const items = rows(tab);
    expect(items.map((row) => row.getAttribute('data-revision'))).toEqual(['3', '2']);
    expect(items.map((row) => row.getAttribute('data-current'))).toEqual(['true', 'false']);
    expect(
      items[1].querySelector('[data-od-id="archive-row-download"]')?.getAttribute('href'),
    ).toBe(`/api/v1/documents/${DID}/versions/2/pdf`);
    // 归档 tab 是滚动容器（版本多了不能顶到操作区外面）
    expect(tab.className).toContain('overflow-auto');
  });

  it('view=archive 只是默认选中归档 tab；切到段落 tab 后归档内容不挂载', async () => {
    mockArchiveTab();
    renderWithQuery(
      <InspectorPanel did={DID} view="archive" feed={makeEventFeed()} compileEvents={[]} />,
    );
    expect(screen.getByRole('tab', { name: '归档' })).toHaveAttribute('aria-selected', 'true');

    fireEvent.click(screen.getByRole('tab', { name: '段落' }));
    await waitFor(() => expect(document.querySelector('[data-od-id="archive-tab"]')).toBeNull());
    expect(screen.getByRole('tab', { name: '段落' })).toHaveAttribute('aria-selected', 'true');
  });

  it('view=progress 默认段落 tab；摘要的「查看全部」仍指向归档 hash', async () => {
    mockArchiveTab();
    renderWithQuery(
      <InspectorPanel did={DID} view="progress" feed={makeEventFeed()} compileEvents={[]} />,
    );
    expect(screen.getByRole('tab', { name: '段落' })).toHaveAttribute('aria-selected', 'true');
    expect(document.querySelector('[data-od-id="archive-tab"]')).toBeNull();

    fireEvent.click(screen.getByRole('tab', { name: '归档' }));
    await waitFor(() => expect(rows()).toHaveLength(2));
    expect(document.querySelector('[data-od-id="archive-summary-all"] a')).toHaveAttribute(
      'href',
      `#/d/${DID}/archive`,
    );
  });

  it('摘要的 stale 提示在归档 tab 里显式出现（草稿比当前版本新）', async () => {
    mockApiFetch({
      ...{ [VERSIONS_PATH]: () => jsonResponse({ ...RESPONSE, stale: true }) },
      [`/api/v1/documents/${DID}`]: () => jsonResponse(DETAIL),
      [`/api/v1/documents/${DID}/jobs`]: () => jsonResponse([]),
      [`/api/v1/documents/${DID}/draft`]: () =>
        jsonResponse({ revision: 4, updated_at: null, paragraphs: {} }),
      '/api/v1/profiles': () => jsonResponse([]),
    });
    renderWithQuery(
      <InspectorPanel did={DID} view="archive" feed={makeEventFeed()} compileEvents={[]} />,
    );
    const stale = await screen.findByText(/草稿有未编译修改/);
    expect(stale.closest('[data-od-id="archive-summary-stale"]')).not.toBeNull();
  });

  it('无版本：摘要与列表都给空态文案，不冒充有版本', async () => {
    mockArchiveTab({ did: DID, current_revision: 0, stale: false, items: [] });
    renderWithQuery(
      <InspectorPanel did={DID} view="archive" feed={makeEventFeed()} compileEvents={[]} />,
    );
    expect(await screen.findByText('还没有全量编译版本')).toBeInTheDocument();
    expect(document.querySelector('[data-od-id="archive-summary-empty"]')).not.toBeNull();
    expect(rows()).toEqual([]);
  });

  it('ArchiveSummary 自己不滚动（滚动归归档 tab 容器，避免嵌套滚动区）', async () => {
    mockVersions();
    renderWithQuery(<ArchiveSummary did={DID} />);
    const summary = (await screen.findByText('2 个版本')).closest(
      '[data-od-id="archive-summary"]',
    ) as HTMLElement;
    expect(summary.className).not.toContain('overflow-auto');
    expect(summary.className).not.toContain('h-full');
  });
});
