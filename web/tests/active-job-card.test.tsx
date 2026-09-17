import { fireEvent, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { ActiveJobCard } from '../src/components/jobs/ActiveJobCard';
import { jsonResponse, makeJob, mockApiFetch, renderWithQuery, resetUiStore } from './helpers';

const DID = 'up-sample-20260917-120000';
const JOBS_URL = `/api/v1/documents/${DID}/jobs`;

beforeEach(() => {
  resetUiStore();
  vi.restoreAllMocks();
});

describe('ActiveJobCard（活动/最近失败任务卡）', () => {
  it('running：状态徽标 + 取消按钮（confirm 后 POST cancel）', async () => {
    const calls: { url: string; method: string }[] = [];
    mockApiFetch({
      [JOBS_URL]: () => jsonResponse([makeJob({ status: 'canceled' })]),
      '/api/v1/jobs/j_01M2RDB312K20Q280DHCTX7N19/cancel': () => {
        calls.push({ url: '/cancel', method: 'POST' });
        return jsonResponse(makeJob({ status: 'canceled' }), 202);
      },
    });
    const confirmSpy = vi.spyOn(window, 'confirm').mockReturnValue(true);

    renderWithQuery(<ActiveJobCard did={DID} job={makeJob({ status: 'running' })} />);
    const status = screen.getByText('运行中 · 翻译');
    expect(status).toBeInTheDocument();
    expect(document.querySelector('[data-od-id="active-job-status"]')).toHaveAttribute(
      'data-status',
      'running',
    );
    // 真运行中才有脉冲（全站唯一动效）
    expect(document.querySelectorAll('.pulse-dot').length).toBeGreaterThan(0);

    fireEvent.click(screen.getByRole('button', { name: '取消' }));
    await waitFor(() => expect(calls).toHaveLength(1));
    expect(confirmSpy).toHaveBeenCalledTimes(1);
    expect(confirmSpy.mock.calls[0][0]).toContain('不会自动重跑');
  });

  it('取消按钮：confirm 拒绝时**不**发请求', async () => {
    const fetchMock = mockApiFetch({});
    vi.spyOn(window, 'confirm').mockReturnValue(false);
    renderWithQuery(<ActiveJobCard did={DID} job={makeJob({ status: 'running' })} />);
    fireEvent.click(screen.getByRole('button', { name: '取消' }));
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('queued：徽标是「排队中」（无脉冲）', () => {
    mockApiFetch({});
    renderWithQuery(<ActiveJobCard did={DID} job={makeJob({ status: 'queued' })} />);
    expect(screen.getByText('排队中')).toBeInTheDocument();
    expect(document.querySelectorAll('.pulse-dot')).toHaveLength(0);
  });

  it('failed：显示 error_code/error_message + 重试（同参数再发一个新 job）', async () => {
    const bodies: unknown[] = [];
    const fetchMock = mockApiFetch({
      [JOBS_URL]: () =>
        jsonResponse([makeJob({ status: 'failed', error_code: 'translator_failed' })]),
    });
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      if (String(input).endsWith('/jobs')) {
        bodies.push(JSON.parse(String(init?.body)));
        return jsonResponse({ job_id: 'j_2', status: 'queued', action: 'run' }, 202);
      }
      return jsonResponse([]);
    });

    renderWithQuery(
      <ActiveJobCard
        did={DID}
        job={makeJob({
          status: 'failed',
          error_code: 'translator_failed',
          error_message: 'stub 退出码 3',
          pages: '1-3',
          dual: true,
          from_stage: 'translate',
        })}
      />,
    );
    const outcome = document.querySelector('[data-od-id="active-job-outcome"]');
    expect(outcome?.textContent).toContain('translator_failed');
    expect(outcome?.textContent).toContain('stub 退出码 3');

    fireEvent.click(screen.getByRole('button', { name: '重试' }));
    await waitFor(() => expect(bodies).toHaveLength(1));
    // 同参数、新 job（不自动重跑）；词表开关按记录里的实际值回传（W13）
    expect(bodies[0]).toEqual({
      action: 'run',
      from: 'translate',
      pages: '1-3',
      dual: true,
      profile: 'echo-t',
      use_glossary: false,
    });
  });

  it('canceled：徽标写「已取消」（不是「运行中」）+ outcome + 重试', () => {
    mockApiFetch({});
    renderWithQuery(<ActiveJobCard did={DID} job={makeJob({ status: 'canceled' })} />);
    expect(screen.getByText('已取消')).toBeInTheDocument();
    expect(screen.queryByText(/运行中/)).toBeNull();
    expect(document.querySelector('[data-od-id="active-job-status"]')).toHaveAttribute(
      'data-status',
      'canceled',
    );
    expect(document.querySelector('[data-od-id="active-job-outcome"]')?.textContent).toContain(
      '已取消',
    );
    expect(screen.getByRole('button', { name: '重试' })).toBeInTheDocument();
  });

  it('failed（action=retranslate）：不给必然 422 的重试按钮，只指向段落面板（W11）', async () => {
    const fetchMock = mockApiFetch({ [JOBS_URL]: () => jsonResponse([]) });
    renderWithQuery(
      <ActiveJobCard
        did={DID}
        job={makeJob({
          action: 'retranslate',
          status: 'failed',
          error_code: 'translator_failed',
          from_stage: null,
        })}
      />,
    );
    expect(screen.queryByRole('button', { name: '重试' })).toBeNull();
    expect(document.querySelector('[data-od-id="retranslate-retry-hint"]')?.textContent).toContain(
      '段落面板',
    );
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('failed：徽标写「失败」（不是「运行中」）', () => {
    mockApiFetch({});
    renderWithQuery(<ActiveJobCard did={DID} job={makeJob({ status: 'failed' })} />);
    expect(screen.getByText('失败')).toBeInTheDocument();
    expect(screen.queryByText(/运行中/)).toBeNull();
  });

  it('interrupted（服务重启）：提示"服务曾重启，请重试"', () => {
    mockApiFetch({});
    renderWithQuery(
      <ActiveJobCard
        did={DID}
        job={makeJob({ status: 'interrupted', interrupted_reason: 'server_restart' })}
      />,
    );
    expect(document.querySelector('[data-od-id="active-job-outcome"]')?.textContent).toContain(
      '服务曾重启',
    );
  });

  it('取消失败（job 已不存在）→ 错误卡，不假装成功', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true);
    mockApiFetch({
      '/api/v1/jobs/j_01M2RDB312K20Q280DHCTX7N19/cancel': () =>
        jsonResponse({ error: { code: 'job_not_found', message: 'job 不存在' } }, 404),
    });
    renderWithQuery(<ActiveJobCard did={DID} job={makeJob({ status: 'running' })} />);
    fireEvent.click(screen.getByRole('button', { name: '取消' }));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('job 不存在');
  });
});
