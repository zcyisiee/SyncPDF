/** `JobControls`（顶栏任务控制）：开始翻译（设置屏默认偏好）/ 取消 / 重试 / 错误卡。 */
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import type { ArtifactItem, DocumentDetail } from '../src/api/types';
import { JobControls } from '../src/components/jobs/JobControls';
import { HARNESS_DEFAULT_KEY } from '../src/lib/harnesses';
import { jsonResponse, makeJob, mockApiFetch, renderWithQuery } from './helpers';

const DID = 'up-sample-20260917-120000';
const JOBS_URL = `/api/v1/documents/${DID}/jobs`;

const ARTIFACTS: ArtifactItem[] = [
  {
    name: 'source.pdf',
    path: 'source.pdf',
    kind: 'source',
    size: 1024,
    mtime: '2026-09-17T12:00:00.000Z',
  },
];

const PROFILES = [
  { id: 'pi-deepseek-flash', label: 'Pi · DeepSeek Flash', builtin: true, thinking_levels: ['off', 'low', 'high', 'max'], default_thinking: 'low', has_translator: true, has_reviewer: false },
];

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
    did: DID,
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

function routes(extra: Record<string, () => Response> = {}) {
  return {
    [`${JOBS_URL}`]: () => jsonResponse([]),
    '/api/v1/documents/up-sample-20260917-120000/artifacts': () => jsonResponse(ARTIFACTS),
    '/api/v1/profiles': () => jsonResponse(PROFILES),
    ...extra,
  };
}

/** 拦下 POST /jobs 并记录请求体（服务端只收 profile id，命令由服务端解析）。 */
function interceptSubmit(extra: Record<string, () => Response> = {}) {
  const calls: { url: string; body: unknown }[] = [];
  const fetchMock = mockApiFetch(routes(extra));
  fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    if (url.endsWith('/jobs') && init?.method === 'POST') {
      calls.push({ url, body: JSON.parse(String(init.body)) });
      return jsonResponse({ job_id: 'j_1', status: 'queued', action: 'run' }, 202);
    }
    const all = routes(extra);
    const route = all[url as keyof typeof all];
    return route ? route() : jsonResponse({ error: { code: 'not_found', message: url } }, 404);
  });
  return { calls, fetchMock };
}

beforeEach(() => {
  window.localStorage.clear();
});

describe('JobControls（顶栏任务控制）', () => {
  it('没有产物时禁用「开始翻译」并解释原因（空目录没有可跑的东西，不造假按钮）', async () => {
    mockApiFetch(routes({
      '/api/v1/documents/up-sample-20260917-120000/artifacts': () => jsonResponse([]),
    }));
    renderWithQuery(<JobControls did={DID} document={makeDocument()} jobs={[]} />);
    const submit = await screen.findByRole('button', { name: '开始翻译' });
    expect(submit).toBeDisabled();
    // 产物清单到达后，tooltip 从「正在读取产物清单…」换成真正的禁用原因
    await waitFor(() =>
      expect(submit.closest('[data-tip]')).toHaveAttribute(
        'data-tip',
        expect.stringContaining('没有可跑的内容'),
      ),
    );
  });

  it('有产物 + 有 profile → 可用；提交只发服务端接受的字段，偏好取设置屏默认（dual/词表/审校）', async () => {
    window.localStorage.setItem(
      HARNESS_DEFAULT_KEY,
      JSON.stringify({ profile: 'pi-deepseek-flash', thinking: 'high', dual: true, useGlossary: false, reviewer: true }),
    );
    const { calls } = interceptSubmit();
    renderWithQuery(<JobControls did={DID} document={makeDocument()} jobs={[]} />);

    const submit = await screen.findByRole('button', { name: '开始翻译' });
    await waitFor(() => expect(submit).toBeEnabled());
    // 配置不再在工作台重复展示（模型/思考/dual/词表/审校/起点阶段都在设置屏或自动判断）
    expect(screen.queryByLabelText('翻译模型')).toBeNull();
    expect(screen.queryByLabelText('思考强度')).toBeNull();
    expect(screen.queryByLabelText(/页码范围/)).toBeNull();
    expect(screen.queryByLabelText('起点阶段')).toBeNull();
    expect(screen.queryByLabelText(/生成 dual/)).toBeNull();
    expect(screen.queryByLabelText(/使用词表/)).toBeNull();

    fireEvent.click(submit);
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].url).toBe(JOBS_URL);
    expect(calls[0].body).toEqual({
      action: 'run',
      // 新文档没有 parse 产物 → 自动从 parse 起步
      from: 'parse',
      pages: null,
      dual: true,
      profile: 'pi-deepseek-flash',
      thinking: 'high',
      use_glossary: false,
      // 预览并行编译 worker 数取自设置屏默认偏好（1..8）
      preview_workers: 8,
      // AI 审校默认开（设置屏保存过）→ 服务端字段
      reviewer_profile: 'pi-deepseek-flash',
    });
  });

  it('已有 parse 产物 → 自动从 translate 续跑（不再跑 MinerU）', async () => {
    const { calls } = interceptSubmit();
    renderWithQuery(
      <JobControls
        did={DID}
        document={makeDocument({
          stage_summary: { ...STAGES_NOT_RUN, parse: 'ok' },
        })}
        jobs={[]}
      />,
    );
    const submit = await screen.findByRole('button', { name: '开始翻译' });
    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.click(submit);
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].body).toMatchObject({ from: 'translate', dual: false, use_glossary: true });
    // 偏好没存过 reviewer → 不带 reviewer_profile 字段
    expect(calls[0].body).not.toHaveProperty('reviewer_profile');
  });

  it('服务端 409 document_busy → 错误卡显示人话标题，「知道了」可关掉', async () => {
    const fetchMock = mockApiFetch(routes());
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/jobs') && init?.method === 'POST') {
        return jsonResponse(
          { error: { code: 'document_busy', message: '这个文档已有任务在跑' } },
          409,
        );
      }
      const all = routes();
      const route = all[url as keyof typeof all];
      return route ? route() : jsonResponse({ error: { code: 'not_found', message: url } }, 404);
    });
    renderWithQuery(<JobControls did={DID} document={makeDocument()} jobs={[]} />);

    const submit = await screen.findByRole('button', { name: '开始翻译' });
    await waitFor(() => expect(submit).toBeEnabled());
    fireEvent.click(submit);
    // ErrorCard 自带 role="alert"；外层包装只是定位容器
    const alert = await screen.findByRole('alert');
    expect(alert.textContent).toContain('这个文档已有任务在跑');
    expect(document.querySelector('[data-od-id="job-controls-error"]')).not.toBeNull();

    fireEvent.click(screen.getByRole('button', { name: '知道了' }));
    await waitFor(() => expect(screen.queryByRole('alert')).toBeNull());
  });

  it('有活动 job：徽标 + 取消（confirm 后 POST cancel）替换「开始翻译」', async () => {
    const calls: string[] = [];
    mockApiFetch({
      [JOBS_URL]: () => jsonResponse([]),
      '/api/v1/documents/up-sample-20260917-120000/artifacts': () => jsonResponse(ARTIFACTS),
      '/api/v1/profiles': () => jsonResponse(PROFILES),
      '/api/v1/jobs/j_01M2RDB312K20Q280DHCTX7N19/cancel': () => {
        calls.push('/cancel');
        return jsonResponse(makeJob({ status: 'canceled' }), 200);
      },
    });
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);

    renderWithQuery(
      <JobControls did={DID} document={makeDocument()} jobs={[makeJob({ did: DID, status: 'running', from_stage: 'translate' })]} />,
    );
    const status = await screen.findByText('运行中 · 翻译');
    expect(status).toBeInTheDocument();
    expect(document.querySelector('[data-od-id="active-job-status"]')).toHaveAttribute('data-status', 'running');
    // 真运行中才有脉冲（全站唯一动效）
    expect(document.querySelectorAll('.pulse-dot').length).toBeGreaterThan(0);
    expect(screen.queryByRole('button', { name: '开始翻译' })).toBeNull();

    fireEvent.click(screen.getByRole('button', { name: '取消' }));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(confirmSpy.mock.calls[0][0]).toContain('不会自动重跑');
  });

  it('取消的 confirm 拒绝时不发请求；queued 徽标无脉冲', async () => {
    const fetchMock = mockApiFetch(routes({
      '/api/v1/documents/up-sample-20260917-120000/artifacts': () => jsonResponse(ARTIFACTS),
    }));
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    renderWithQuery(
      <JobControls did={DID} document={makeDocument()} jobs={[makeJob({ did: DID, status: 'queued' })]} />,
    );
    expect(await screen.findByText('排队中')).toBeInTheDocument();
    expect(document.querySelectorAll('.pulse-dot')).toHaveLength(0);
    fireEvent.click(screen.getByRole('button', { name: '取消' }));
    expect(fetchMock.mock.calls.every((call) => !(String(call[0]).includes('/cancel')))).toBe(true);
  });

  it('最近一次失败：失败徽标 + 重试（同参数发新 job）；取消失败给错误卡', async () => {
    const { calls } = interceptSubmit({
      [JOBS_URL]: () =>
        jsonResponse([
          makeJob({
            did: DID, job_id: 'j_fail', status: 'failed', error_code: 'translator_crash',
            error_message: '模型超时', profile: 'pi-deepseek-flash', reviewer_profile: 'pi-deepseek-flash',
            thinking: 'high', dual: true, use_glossary: true,
          }),
        ]),
      '/api/v1/jobs/j_01M2RDB312K20Q280DHCTX7N19/cancel': () =>
        jsonResponse({ error: { code: 'not_found', message: 'job 已不存在' } }, 404),
    });
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    renderWithQuery(
      <JobControls
        did={DID}
        document={makeDocument()}
        jobs={[
          makeJob({ did: DID, job_id: 'j_fail', status: 'failed', error_code: 'translator_crash', error_message: '模型超时', profile: 'pi-deepseek-flash', reviewer_profile: 'pi-deepseek-flash', thinking: 'high', dual: true, use_glossary: true }),
        ]}
      />,
    );
    expect(await screen.findByText('失败')).toBeInTheDocument();
    expect(document.querySelector('[data-od-id="active-job-status"]')).toHaveAttribute('data-status', 'failed');

    fireEvent.click(screen.getByRole('button', { name: '重试' }));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].body).toMatchObject({
      action: 'run', profile: 'pi-deepseek-flash', reviewer_profile: 'pi-deepseek-flash',
      thinking: 'high', dual: true, use_glossary: true,
    });
  });

  it('failed 的 retranslate：不给必然 422 的重试按钮，只指向段落面板（W11）', async () => {
    mockApiFetch(routes());
    renderWithQuery(
      <JobControls
        did={DID}
        document={makeDocument()}
        jobs={[makeJob({ did: DID, action: 'retranslate', status: 'failed' })]}
      />,
    );
    expect(await screen.findByText(/在段落面板重试/)).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: '重试' })).toBeNull();
  });

  it('check/compile 失败重试不携带只对 run 有效的字段', async () => {
    const { calls } = interceptSubmit();
    const check = renderWithQuery(
      <JobControls
        did={DID}
        document={makeDocument()}
        jobs={[makeJob({ did: DID, action: 'check', status: 'failed', profile: 'pi-deepseek-flash', thinking: 'high', from_stage: 'check' })]}
      />,
    );
    fireEvent.click(await screen.findByRole('button', { name: '重试' }));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].body).toEqual({ action: 'check', profile: 'pi-deepseek-flash', thinking: 'high', dual: false, use_glossary: false });
    check.unmount();

    calls.length = 0;
    renderWithQuery(
      <JobControls
        did={DID}
        document={makeDocument()}
        jobs={[makeJob({ did: DID, action: 'compile', status: 'failed', requested_scope: 'pages', from_stage: 'apply' })]}
      />,
    );
    fireEvent.click(await screen.findByRole('button', { name: '重试' }));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].body).toEqual({ action: 'compile', scope: 'pages', dual: false, use_glossary: false });
  });
});
