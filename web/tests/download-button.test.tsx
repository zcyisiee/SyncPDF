/**
 * 下载按钮的状态矩阵（brief 红线：不许把失败/未编译的 PDF 冒充成功；needs_fix 显式黄标）。
 *
 * 判定表在 `lib/download.ts`（`downloadState` / `qualityBadge`），这里断言渲染与语义：
 * `none` / `ok` / `failed` / `stale` × 质量（pipeline_ok / needs_fix / 不可用）。
 */
import { render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import { DownloadButton } from '../src/components/preview/DownloadButton';
import {
  compileArtifactKey,
  downloadState,
  qualityBadge,
  versionedFileName,
  withArtifactRevision,
} from '../src/lib/download';

const DID = 'ccs3764-dyn';
const ARTIFACT = { name: 'paper.mono.pdf', revision: 7, size: 1024 };
const QUALITY_OK = { check: { verdict: 'pass' }, reviewer: { status: 'pass' }, pipeline_ok: true };
const QUALITY_NEEDS_FIX = {
  check: { verdict: 'needs_fix' },
  reviewer: { status: 'needs_fix' },
  pipeline_ok: false,
};

function button() {
  return document.querySelector('[data-od-id="download-button"]');
}

describe('downloadState（判定表）', () => {
  it('从没编译过（none + artifact=null）→ 禁用，不冒充成功', () => {
    const state = downloadState({ status: 'none', revision: 0, stale: false, artifact: null }, QUALITY_OK);
    expect(state.enabled).toBe(false);
    expect(state.fileName).toBe('');
    expect(state.disabledReason).toContain('还没有可下载的编译产物');
  });

  it('编译中但没有旧产物 → 禁用，原因是「编译进行中」', () => {
    const state = downloadState({ status: 'running', revision: 0, stale: true, artifact: null }, null);
    expect(state.enabled).toBe(false);
    expect(state.disabledReason).toBe('编译进行中：完成后才能下载产物');
  });

  it('编译中但有上一版产物 → 仍可下载（旧版），徽标显式写「比草稿旧」', () => {
    const state = downloadState({ status: 'running', revision: 5, stale: true, artifact: { name: 'paper.mono.pdf', revision: 5 } }, null);
    expect(state.enabled).toBe(true);
    expect(state.outdated).toBe(true);
    expect(state.artifactBadge?.label).toBe('比草稿旧 · r5');
    expect(state.artifactBadge?.label).not.toContain('最新');
  });

  it('ok 且不 stale → 「最新 · r7」+ 真实下载键与改名', () => {
    const state = downloadState({ status: 'ok', revision: 7, stale: false, artifact: ARTIFACT }, QUALITY_OK);
    expect(state.enabled).toBe(true);
    expect(state.outdated).toBe(false);
    expect(state.artifactKey).toBe('output/paper.mono.pdf');
    expect(state.fileName).toBe('paper.mono.r7.pdf');
    expect(state.artifactBadge).toEqual(
      expect.objectContaining({ tone: 'pass', label: '最新 · r7' }),
    );
  });

  it('failed 但旧产物还在 → 可下载 + 朱红徽标「编译失败 · 仍是 r5」', () => {
    const state = downloadState({ status: 'failed', revision: 5, stale: true, artifact: { name: 'paper.mono.pdf', revision: 5 } }, QUALITY_NEEDS_FIX);
    expect(state.enabled).toBe(true);
    expect(state.outdated).toBe(true);
    expect(state.artifactBadge?.tone).toBe('err');
    expect(state.artifactBadge?.label).toBe('编译失败 · 仍是 r5');
  });

  it('artifact 名字缺失/为空 → 当没有产物处理（不拼出坏下载键）', () => {
    expect(downloadState({ status: 'ok', revision: 3, stale: false, artifact: { revision: 3 } }, null).enabled).toBe(false);
    expect(downloadState({ status: 'ok', revision: 3, stale: false, artifact: { name: '' } }, null).enabled).toBe(false);
  });

  it('字段缺失（老后端/空对象）不崩，按「没有编译」处理', () => {
    const state = downloadState({}, {});
    expect(state.enabled).toBe(false);
    expect(state.artifactBadge).toBeNull();
  });

  it('versionedFileName：只在 .pdf 前插 r{n}；非 pdf 不退化成乱扩展名', () => {
    expect(versionedFileName('paper.mono.pdf', 7)).toBe('paper.mono.r7.pdf');
    expect(versionedFileName('paper.PDF', 2)).toBe('paper.r2.PDF');
    expect(versionedFileName('report.md', 4)).toBe('report.md.r4');
    expect(versionedFileName('paper.mono.pdf', null)).toBe('paper.mono.pdf');
  });

  it('withArtifactRevision：只在有修订号时加 `?r=`（已有 query 用 &）', () => {
    expect(withArtifactRevision('/api/x/artifacts/output/a.pdf', 7)).toBe('/api/x/artifacts/output/a.pdf?r=7');
    expect(withArtifactRevision('/api/x/a.pdf?keep=1', 3)).toBe('/api/x/a.pdf?keep=1&r=3');
    expect(withArtifactRevision('/api/x/a.pdf', null)).toBe('/api/x/a.pdf');
  });

  it('compileArtifactKey：output/<裸文件名>；没有产物 → null', () => {
    expect(compileArtifactKey({ status: 'ok', revision: 7, stale: false, artifact: ARTIFACT })).toBe(
      'output/paper.mono.pdf',
    );
    expect(compileArtifactKey({ status: 'none', artifact: null })).toBeNull();
    expect(compileArtifactKey(null)).toBeNull();
  });

  it('qualityBadge：只有 pipeline_ok 才绿；needs_fix 黄标；不可用中性', () => {
    expect(qualityBadge(QUALITY_OK)).toEqual(expect.objectContaining({ tone: 'pass', label: '检查通过' }));
    expect(qualityBadge(QUALITY_NEEDS_FIX)).toEqual(
      expect.objectContaining({ tone: 'run', label: '检查未通过' }),
    );
    // check 过了但门禁没放行（reviewer 待人工）→ 不许绿
    expect(qualityBadge({ check: { verdict: 'pass' }, reviewer: { status: 'waiting_for_reviewer' }, pipeline_ok: false }).tone).toBe('run');
    expect(qualityBadge({ check: { verdict: 'not_available' }, reviewer: { status: 'not_run' }, pipeline_ok: false })).toEqual(
      expect.objectContaining({ tone: 'idle', label: '检查不可用' }),
    );
    expect(qualityBadge(null)).toEqual(expect.objectContaining({ tone: 'idle' }));
  });
});

describe('DownloadButton 渲染', () => {
  it('可下载：<a download=改后名> 指向 output/<name>，并带修订徽标 + 质量徽标', () => {
    render(<DownloadButton did={DID} compile={{ status: 'ok', revision: 7, stale: false, artifact: ARTIFACT }} quality={QUALITY_OK} />);
    const link = button() as HTMLAnchorElement;
    expect(link.tagName).toBe('A');
    // 带 `?r=`：同名产物会被编译原地替换，下载的字节必须是徽标上那个修订
    expect(link.getAttribute('href')).toBe(
      `/api/v1/documents/${DID}/artifacts/output/paper.mono.pdf?r=7`,
    );
    expect(link.getAttribute('download')).toBe('paper.mono.r7.pdf');
    expect(link).toHaveAttribute('data-enabled', 'true');
    expect(document.querySelector('[data-od-id="compile-revision-badge"]')?.textContent).toContain('最新 · r7');
    expect(document.querySelector('[data-od-id="quality-badge"]')?.textContent).toContain('检查通过');
  });

  it('不可下载：渲染禁用态（aria-disabled + tooltip 原因），没有 href', () => {
    render(<DownloadButton did={DID} compile={{ status: 'none', revision: 0, stale: false, artifact: null }} quality={null} />);
    const node = button();
    expect(node?.tagName).toBe('SPAN');
    expect(node).toHaveAttribute('data-enabled', 'false');
    expect(node).toHaveAttribute('aria-disabled', 'true');
    expect(document.querySelector('[data-od-id="compile-revision-badge"]')).toBeNull();
    expect(document.querySelector('[data-od-id="quality-badge"]')?.textContent).toContain('检查不可用');
  });

  it('stale：仍然可下载，但按钮上标「（旧版）」且徽标不是「最新」', () => {
    render(<DownloadButton did={DID} compile={{ status: 'ok', revision: 4, stale: true, artifact: { name: 'paper.mono.pdf', revision: 4 } }} quality={QUALITY_NEEDS_FIX} />);
    const link = button() as HTMLAnchorElement;
    expect(link.getAttribute('download')).toBe('paper.mono.r4.pdf');
    expect(link.textContent).toContain('（旧版）');
    expect(document.querySelector('[data-od-id="compile-revision-badge"]')?.textContent).toContain('比草稿旧 · r4');
    expect(document.querySelector('[data-od-id="quality-badge"]')?.textContent).toContain('检查未通过');
  });

  it('failed + needs_fix：下载不冒充成功，质量显式黄标', () => {
    render(<DownloadButton did={DID} compile={{ status: 'failed', revision: 2, stale: true, artifact: { name: 'paper.mono.pdf', revision: 2 } }} quality={QUALITY_NEEDS_FIX} />);
    expect(document.querySelector('[data-od-id="compile-revision-badge"]')?.textContent).toContain('编译失败');
    expect(screen.getByText('检查未通过')).toBeInTheDocument();
  });
});
