/**
 * `lib/humanize.ts`：事件 kind 短标签（本仓库归档里真实出现过的 47 种 + 批量块编译的
 * `block_compiled`/`compile_float` + W14 的虚拟 kind `job_update`）、`formatDuration`
 * 边界、列表页 running 判据。
 */
import { describe, expect, it } from 'vitest';

import {
  EVENT_KINDS,
  FINAL_STAGE,
  formatDuration,
  formatStageDuration,
  hasRunningDocument,
  kindLabel,
  STAGE_LABELS,
  STAGE_NAMES,
  stageLabel,
  stageStatusLabel,
} from '../src/lib/humanize';

/**
 * `tmp/<did>/debug/runs/<run>/events.jsonl` 全量统计出来的 47 种**真 kind**（W06 实测）。
 * W14 的 `job_update` 不在其中（它是 SSE 的虚拟 kind，归档里没有），见下一条断言。
 */
const observedKinds = [
  'candidate_evaluated',
  'call_started',
  'call_finished',
  'canonical_writeback',
  'cache_write',
  'cache_miss',
  'candidate_selected',
  'anchor_repair',
  'cache_bypass',
  'compile_requests',
  'cache_hit',
  'compile_reuse',
  'compile_fallback',
  'stage_started',
  'stage_finished',
  'artifact_bundle',
  'span_started',
  'span_finished',
  'pdf_prepared',
  'page_frames',
  'native_chars',
  'layout_parsed',
  'layout_coverage',
  'provider_artifacts',
  'compile_expand',
  'text_version',
  'inline_math',
  'ocr_backfill',
  'enclosed_marker',
  'toc',
  'styles_formulas',
  'paragraphs_found',
  'source_geometry',
  'links_snapshot',
  'selection',
  'typesetting_geometry',
  'latex_capability',
  'latex_candidates',
  'latex_prepare',
  'latex_stamp',
  'latex_summary',
  'missing_ids',
  'apply_validation',
  'placeholder_validation',
  'writeback_saved',
  'stage_error',
  'replay_notice',
];

describe('kind 标签', () => {
  it('阶段名固定 7 个、最后一个阶段是 report（事件 live 判据依赖它）', () => {
    expect(STAGE_NAMES).toEqual(['parse', 'translate', 'apply', 'build', 'check', 'review', 'report']);
    expect(FINAL_STAGE).toBe('report');
    expect(Object.keys(STAGE_LABELS)).toHaveLength(7);
  });

  it('表里覆盖归档里真实出现过的 47 种 kind，且标签是中文短词', () => {
    // `tmp/<did>/debug/runs/<run>/events.jsonl` 全量统计出来的 kind（W06 实测）
    for (const kind of observedKinds) {
      expect(EVENT_KINDS).toContain(kind);
      expect(kindLabel(kind)).not.toBe(kind);
      // brief 的「≤4 字优先」是偏好：只有 `cache_miss` 取 5 字「缓存未命中」（比「缓存未中」清楚），
      // 其余中文部分都 ≤4 字；`PDF 准备` / `OCR 回填` 带拉丁缩写。
      expect(kindLabel(kind).replace(/[A-Za-z0-9\s]/g, '').length).toBeLessThanOrEqual(5);
    }
  });

  it('表里没有的 kind 原样返回（不编造文案）', () => {
    expect(kindLabel('job_queued')).toBe('job_queued');
    expect(stageLabel('replay')).toBe('replay');
    expect(stageStatusLabel('not_run')).toBe('未运行');
  });

  it('虚拟 kind `job_update`（W14）进表：有中文文案且进 SSE 必须监听的清单', () => {
    // 它是 SSE 同一条流里的 job 状态变化通知（不在 events.jsonl 里、没有 seq）——
    // 进 KIND_LABELS 只是为了有文案 + 进 EVENT_KINDS（否则 addEventListener 收不到）。
    expect(kindLabel('job_update')).toBe('任务状态');
    expect(EVENT_KINDS).toContain('job_update');
    // 47 种真 kind 一个没丢，也没有被虚拟 kind 顶掉
    expect(observedKinds).toHaveLength(47);
    // 54 = 53 + `block_not_replaced`（保留原文，e36c8683 随流式编译资格门禁一起加入
    // KIND_LABELS，但当时漏改这条数量断言）。
    expect(EVENT_KINDS).toHaveLength(54);
  });

  it('批量块编译的两种事件 kind（block_compiled / compile_float）有标签且进 SSE 清单', () => {
    // 不补 EVENT_KINDS 就收不到：批量编译 job 每块完成发 block_compiled、
    // 贴片浮动扩框/迁移发 compile_float（serve/block_compile.py）。
    expect(kindLabel('block_compiled')).toBe('块已编译');
    expect(kindLabel('compile_float')).toBe('贴片浮动');
    expect(EVENT_KINDS).toContain('block_compiled');
    expect(EVENT_KINDS).toContain('compile_float');
  });
});

describe('formatStageDuration', () => {
  it('已完成阶段 <1s 显示实测小数，不用「刚启动」', () => {
    expect(formatStageDuration(0)).toBe('0s');
    expect(formatStageDuration(0.32)).toBe('0.3s');
    expect(formatStageDuration(0.05)).toBe('0.1s');
    expect(formatStageDuration(1)).toBe('1s');
    expect(formatStageDuration(248.93)).toBe('4m 8s');
    expect(formatStageDuration(null)).toBe('—');
    expect(formatStageDuration(Number.NaN)).toBe('—');
  });
});

describe('formatDuration', () => {
  it('brief 口径：<1s 刚启动 / Xs / Xm Ys / Xh Ym', () => {
    expect(formatDuration(0)).toBe('刚启动');
    expect(formatDuration(0.32)).toBe('刚启动');
    expect(formatDuration(1)).toBe('1s');
    expect(formatDuration(15.83)).toBe('15s');
    expect(formatDuration(59.99)).toBe('59s');
    expect(formatDuration(60)).toBe('1m 0s');
    expect(formatDuration(248.93)).toBe('4m 8s');
    expect(formatDuration(3599)).toBe('59m 59s');
    expect(formatDuration(3600)).toBe('1h 0m');
    expect(formatDuration(7500)).toBe('2h 5m');
  });

  it('拿不到耗时 → `—`（不编造 0）', () => {
    expect(formatDuration(null)).toBe('—');
    expect(formatDuration(undefined)).toBe('—');
    expect(formatDuration(Number.NaN)).toBe('—');
  });
});

describe('hasRunningDocument（列表页 3s 轮询判据）', () => {
  const doc = (statuses: string[]) => ({
    stage_summary: Object.fromEntries(STAGE_NAMES.map((stage, index) => [stage, statuses[index] ?? 'not_run'])),
  });

  it('有任一阶段 running → true；全 ok / 全 not_run → false', () => {
    expect(hasRunningDocument([doc(['ok', 'running']), doc([])])).toBe(true);
    expect(hasRunningDocument([doc(STAGE_NAMES.map(() => 'ok'))])).toBe(false);
    expect(hasRunningDocument([doc([])])).toBe(false);
    expect(hasRunningDocument([])).toBe(false);
  });
});
