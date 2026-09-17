/**
 * 版本归档（api.md §3.7，W12）：判定表（`lib/versions.ts`）+ 归档视图渲染。
 *
 * 断言口径与后端/契约一致：
 * - 清单**新 → 旧**；`current_revision` 那一版标「当前版本」（不是"清单最新行"）；
 * - `stale=true` → 显式提示条（草稿有未编译修改），绝不把旧版本说成最新；
 * - 质量徽标**复用 W10 判定表**：`pipeline_ok=true` 绿、`needs_fix` 黄，且**不**禁用下载；
 * - 下载 href = `/api/v1/documents/{did}/versions/<r>/pdf`，落盘名 = `<名去 .pdf>.r<r>.pdf`；
 * - 从没编译成功过 → 空态引导（指向翻译视图），不是错误。
 */
import { screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import type { VersionItem, VersionsResponse } from '../src/api/types';
import { ArchiveView } from '../src/components/archive/ArchiveView';
import { qualityBadge } from '../src/lib/download';
import {
  archiveHash,
  archiveSummary,
  triggerLabel,
  versionQualityBadge,
  versionPdfUrl,
  versionRows,
} from '../src/lib/versions';
import { jsonResponse, mockApiFetch, renderWithQuery } from './helpers';

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

function rows() {
  return Array.from(document.querySelectorAll('[data-od-id="archive-row"]'));
}

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
// 归档视图
// --------------------------------------------------------------------------- #
function mockVersions(body: unknown = RESPONSE, status = 200) {
  return mockApiFetch({ [VERSIONS_PATH]: () => jsonResponse(body, status) });
}

describe('ArchiveView（版本列表 + 当前高亮 + 下载）', () => {
  it('列出全部版本（新 → 旧），当前版本高亮并带「当前版本」标记', async () => {
    mockVersions();
    renderWithQuery(<ArchiveView did={DID} compile={COMPILE} />);

    await screen.findByText('共 2 个版本');
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
    // 右上角标出保留上限（50 上限是服务端行为，前端只如实说明）
    expect(screen.getByText('保留最近 50 个版本')).toBeInTheDocument();
  });

  it('质量徽标复用 W10 判定表，且 needs_fix 不禁用下载（只记录不门禁）', async () => {
    mockVersions();
    renderWithQuery(<ArchiveView did={DID} compile={COMPILE} />);
    await screen.findByText('共 2 个版本');

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

  it('stale=true → 显式提示「草稿有未编译修改」，不把当前版本当成最新草稿', async () => {
    mockVersions({ ...RESPONSE, stale: true });
    renderWithQuery(<ArchiveView did={DID} compile={{ ...COMPILE, stale: true }} />);

    const stale = await screen.findByText(/草稿有未编译修改/);
    expect(stale.closest('[data-od-id="archive-stale"]')).not.toBeNull();
    expect(stale.textContent).toContain('r3');
  });

  it('stale=false → 没有提示条（不无病呻吟）', async () => {
    mockVersions();
    renderWithQuery(<ArchiveView did={DID} compile={COMPILE} />);
    await screen.findByText('共 2 个版本');
    expect(document.querySelector('[data-od-id="archive-stale"]')).toBeNull();
  });

  it('空态：从没编译成功过 → 引导 + 指向翻译视图（不是错误）', async () => {
    mockVersions({ did: DID, current_revision: 0, stale: false, items: [] });
    renderWithQuery(<ArchiveView did={DID} compile={null} />);

    const empty = await screen.findByText('还没有版本归档');
    expect(empty).toBeInTheDocument();
    expect(document.querySelector('[data-od-id="archive-empty"]')).not.toBeNull();
    expect(
      document.querySelector('[data-od-id="archive-empty-cta"] a')?.getAttribute('href'),
    ).toBe(`#/d/${DID}/translate`);
    expect(rows()).toEqual([]);
    expect(document.querySelector('[data-od-id="archive-error"]')).toBeNull();
  });

  it('请求失败 → 错误卡 + 重试（不留白屏）', async () => {
    mockVersions({ error: { code: 'document_not_found', message: '文档不存在' } }, 404);
    renderWithQuery(<ArchiveView did={DID} compile={COMPILE} />);
    expect(await screen.findByRole('alert')).toBeInTheDocument();
    expect(screen.getByText('文档不存在')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '重试' })).toBeInTheDocument();
  });

  it('载入中：给占位文案，不显示空态（避免"还没读完"被当成"没有版本"）', () => {
    mockApiFetch({ [VERSIONS_PATH]: () => new Promise<Response>(() => {}) });
    renderWithQuery(<ArchiveView did={DID} compile={COMPILE} />);
    expect(document.querySelector('[data-od-id="archive-loading"]')).not.toBeNull();
    expect(document.querySelector('[data-od-id="archive-empty"]')).toBeNull();
  });
});
