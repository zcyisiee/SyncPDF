import { describe, expect, it } from 'vitest';

import type { DocumentDetail, JobRecord } from '../src/api/types';
import {
  JOBS_ACTIVE_REFETCH_MS,
  JOBS_IDLE_REFETCH_MS,
  activeJob,
  defaultFromStage,
  isValidPagesSpec,
  jobCardMode,
  jobOutcomeMessage,
  jobsRefetchInterval,
  normalizePagesSpec,
} from '../src/lib/jobs';
import { makeJob } from './helpers';

const STAGES_NOT_RUN = {
  parse: 'not_run',
  translate: 'not_run',
  apply: 'not_run',
  build: 'not_run',
  check: 'not_run',
  review: 'not_run',
  report: 'not_run',
};

function makeDocument(overrides: Partial<DocumentDetail> = {}): DocumentDetail {
  return {
    did: 'up-sample-20260917-120000',
    title: null,
    pages: null,
    paragraph_count: null,
    translated_count: null,
    stage_summary: STAGES_NOT_RUN,
    updated_at: null,
    pdf: { source: null, outputs: [] },
    config: null,
    quality: {
      check: { verdict: 'not_available', blockers: [], warnings: [], reasons: [], at: null },
      reviewer: { status: 'not_run', fix_rounds: {}, at: null },
      pipeline_ok: false,
    },
    compile: { status: 'none', revision: 0, stale: false, artifact: null },
    available: {
      run_state: false,
      anchors: false,
      translated: false,
      geometry: false,
      parse_snapshot: false,
      review_verdict: false,
      layout_lint: false,
      link_audit: false,
    },
    ...overrides,
  } as DocumentDetail;
}

describe('job 口径（lib/jobs.ts）', () => {
  it('activeJob 只认 queued/running（列表新 → 旧）', () => {
    const running = makeJob({ status: 'running' });
    const succeeded = makeJob({ status: 'succeeded' });
    expect(activeJob([succeeded, running])).toBe(running);
    expect(activeJob([succeeded])).toBeNull();
    expect(activeJob([])).toBeNull();
    expect(activeJob(undefined)).toBeNull();
  });

  it('轮询间隔：有活动 job 2s，否则 30s 兜底', () => {
    expect(jobsRefetchInterval([makeJob({ status: 'running' })])).toBe(JOBS_ACTIVE_REFETCH_MS);
    expect(jobsRefetchInterval([makeJob({ status: 'queued' })])).toBe(JOBS_ACTIVE_REFETCH_MS);
    expect(jobsRefetchInterval([makeJob({ status: 'succeeded' })])).toBe(JOBS_IDLE_REFETCH_MS);
    expect(jobsRefetchInterval(undefined)).toBe(JOBS_IDLE_REFETCH_MS);
  });

  it('jobCardMode：活动 → active；失败/取消/中断 → failed；成功/无 job → start', () => {
    expect(jobCardMode(undefined)).toBe('start');
    expect(jobCardMode([])).toBe('start');
    expect(jobCardMode([makeJob({ status: 'queued' })])).toBe('active');
    expect(jobCardMode([makeJob({ status: 'running' })])).toBe('active');
    expect(jobCardMode([makeJob({ status: 'succeeded' })])).toBe('start');
    expect(jobCardMode([makeJob({ status: 'failed' })])).toBe('failed');
    expect(jobCardMode([makeJob({ status: 'canceled' })])).toBe('failed');
    expect(jobCardMode([makeJob({ status: 'interrupted' })])).toBe('failed');
    // 最新一条说了算（列表新 → 旧）：老 job 的失败不该盖住正在跑的新 job
    expect(
      jobCardMode([makeJob({ status: 'running' }), makeJob({ status: 'failed' })]),
    ).toBe('active');
  });

  it('defaultFromStage：有 parse 产物 → translate；空文档 → parse', () => {
    expect(defaultFromStage(undefined)).toBe('parse');
    expect(defaultFromStage(makeDocument())).toBe('parse');
    expect(
      defaultFromStage(makeDocument({ stage_summary: { ...STAGES_NOT_RUN, parse: 'ok' } })),
    ).toBe('translate');
    // 旧 workdir 没有 run_state：靠 available 的产物判据
    expect(
      defaultFromStage(
        makeDocument({ available: { ...makeDocument().available, anchors: true } }),
      ),
    ).toBe('translate');
    expect(
      defaultFromStage(
        makeDocument({ available: { ...makeDocument().available, parse_snapshot: true } }),
      ),
    ).toBe('translate');
  });

  it('pages 形状：与服务端 pattern 同口径（空 = 全部页）', () => {
    expect(isValidPagesSpec('')).toBe(true);
    expect(isValidPagesSpec('   ')).toBe(true);
    expect(isValidPagesSpec('1-3,5')).toBe(true);
    expect(isValidPagesSpec('7')).toBe(true);
    expect(isValidPagesSpec('1-3;rm -rf /')).toBe(false);
    expect(isValidPagesSpec('1-')).toBe(false);
    expect(isValidPagesSpec('a-b')).toBe(false);
    expect(normalizePagesSpec(' 1-3,5 ')).toBe('1-3,5');
    expect(normalizePagesSpec('   ')).toBeNull();
  });

  it('终态文案：失败带 error_code，取消/中断人话，成功无文案', () => {
    expect(
      jobOutcomeMessage(
        makeJob({ status: 'failed', error_code: 'translator_failed', error_message: '退出码 3' }),
      ),
    ).toContain('translator_failed');
    expect(jobOutcomeMessage(makeJob({ status: 'failed' }))).toBe('任务失败');
    expect(jobOutcomeMessage(makeJob({ status: 'canceled' }))).toContain('已取消');
    expect(
      jobOutcomeMessage(makeJob({ status: 'interrupted', interrupted_reason: 'server_restart' })),
    ).toContain('服务曾重启');
    expect(jobOutcomeMessage(makeJob({ status: 'succeeded' }))).toBeNull();
  });
});

/** 类型检查用：JobRecord 的必需字段（生成的 OpenAPI 类型）确实被 makeJob 覆盖。 */
const _typecheck: JobRecord = {} as JobRecord;
void _typecheck;
