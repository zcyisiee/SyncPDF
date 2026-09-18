import { QueryClientProvider } from '@tanstack/react-query';
import { renderHook, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

import { createQueryClient } from '../src/app/App';
import {
  apiGet,
  apiPost,
  apiPut,
  apiUpload,
  parseErrorEnvelope,
} from '../src/lib/api';
import {
  JOBS_ACTIVE_REFETCH_MS,
  JOBS_IDLE_REFETCH_MS,
} from '../src/lib/jobs';
import {
  useCancelJobMutation,
  useCreateJobMutation,
  useJobs,
  useProfiles,
  useUploadMutation,
} from '../src/lib/queries';
import { jsonResponse, makeJob, makePdfFile } from './helpers';

function wrapper({ children }: { children: ReactNode }) {
  return <QueryClientProvider client={createQueryClient()}>{children}</QueryClientProvider>;
}

/** 记录每个请求（method/url/body），按 URL 前缀给响应。 */
function recordFetch(routes: Record<string, () => Response | Promise<Response>>) {
  const calls: { method: string; url: string; body: unknown }[] = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    const method = (init?.method ?? 'GET').toUpperCase();
    const body = init?.body instanceof FormData ? init.body.get('file') : init?.body;
    calls.push({ method, url, body });
    const route = routes[`${method} ${url}`] ?? routes[url];
    if (route) return await route();
    return jsonResponse({ error: { code: 'not_found', message: `未 mock：${method} ${url}` } }, 404);
  });
  vi.stubGlobal('fetch', fetchMock);
  return calls;
}

beforeEach(() => {
  vi.unstubAllGlobals();
});

describe('W08 hooks（mock fetch）', () => {
  it('useProfiles：GET /profiles，响应只有 id/label/has_*', async () => {
    const calls = recordFetch({
      '/api/v1/profiles': () =>
        jsonResponse([
          { id: 'echo-t', label: 'Echo T', has_translator: true, has_reviewer: false },
        ]),
    });
    const { result } = renderHook(() => useProfiles(), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.[0].id).toBe('echo-t');
    expect(calls).toEqual([{ method: 'GET', url: '/api/v1/profiles', body: undefined }]);
  });

  it('useJobs：拉该文档的 job 列表（新 → 旧）；轮询口径由 jobsRefetchInterval 纯函数固定', async () => {
    const calls = recordFetch({
      '/api/v1/documents/alpha/jobs': () =>
        jsonResponse([makeJob({ status: 'running' }), makeJob({ job_id: 'j_old', status: 'failed' })]),
    });
    const { result } = renderHook(() => useJobs('alpha'), { wrapper });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.map((job) => job.job_id)).toEqual([makeJob().job_id, 'j_old']);
    expect(calls).toEqual([
      { method: 'GET', url: '/api/v1/documents/alpha/jobs', body: undefined },
    ]);
    // 5s / 30s 两个间隔是 jobsRefetchInterval 的输出（tests/jobs.test.ts 已断言口径）；
    // 这里只钉住常量没被人改错（W14 从 2s 放宽到 5s：快路径是 SSE 的 job_update）。
    expect(JOBS_ACTIVE_REFETCH_MS).toBe(5000);
    expect(JOBS_IDLE_REFETCH_MS).toBe(30_000);
  });

  it('useJobs：did=null 不发请求', () => {
    const calls = recordFetch({});
    const { result } = renderHook(() => useJobs(null), { wrapper });
    expect(result.current.fetchStatus).toBe('idle');
    expect(calls).toEqual([]);
  });

  it('useUploadMutation：POST multipart，body 只有 file 字段；成功后列表失效', async () => {
    const calls = recordFetch({
      'POST /api/v1/documents': () =>
        jsonResponse({ did: 'up-sample-20260917-120000', bytes: 5, source: 'source.pdf' }, 201),
    });
    const { result } = renderHook(() => useUploadMutation(), { wrapper });
    result.current.mutate(makePdfFile());
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.did).toBe('up-sample-20260917-120000');
    expect(calls).toHaveLength(1);
    expect(calls[0].method).toBe('POST');
    expect(calls[0].url).toBe('/api/v1/documents');
    // FormData 里只有 file（没有 did/路径/命令字段）
    const body = calls[0].body as File;
    expect(body).toBeInstanceOf(File);
    expect(body.name).toBe('sample.pdf');
  });

  it('useUploadMutation：413 走统一错误信封', async () => {
    recordFetch({
      'POST /api/v1/documents': () =>
        jsonResponse(
          {
            error: {
              code: 'file_too_large',
              message: '上传超过上限 209715200 字节',
              detail: { limit_bytes: 209715200, size_bytes: 300000000 },
            },
          },
          413,
        ),
    });
    const { result } = renderHook(() => useUploadMutation(), { wrapper });
    result.current.mutate(makePdfFile());
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error).toMatchObject({ code: 'file_too_large', status: 413 });
  });

  it('useCreateJobMutation：POST 只带 action/from/pages/dual/profile/use_glossary', async () => {
    const calls = recordFetch({
      'POST /api/v1/documents/alpha/jobs': () =>
        jsonResponse({ job_id: 'j_1', status: 'queued', action: 'run' }, 202),
    });
    const { result } = renderHook(() => useCreateJobMutation('alpha'), { wrapper });
    result.current.mutate({
      action: 'run',
      from: 'parse',
      pages: '1-3',
      dual: true,
      profile: 'echo-t',
      use_glossary: true,
    });
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(calls[0].method).toBe('POST');
    expect(calls[0].url).toBe('/api/v1/documents/alpha/jobs');
    expect(JSON.parse(String(calls[0].body))).toEqual({
      action: 'run',
      from: 'parse',
      pages: '1-3',
      dual: true,
      profile: 'echo-t',
      use_glossary: true,
    });
    // 客户端侧没有命令/密钥字段可传（类型里也没有）；词表只有这一个布尔开关
    expect(Object.keys(JSON.parse(String(calls[0].body)))).toEqual([
      'action',
      'from',
      'pages',
      'dual',
      'profile',
      'use_glossary',
    ]);
  });

  it('useCancelJobMutation：POST /jobs/{jid}/cancel（幂等，终态返回 200 也是成功）', async () => {
    const calls = recordFetch({
      'POST /api/v1/jobs/j_1/cancel': () => jsonResponse(makeJob({ status: 'succeeded' })),
    });
    const { result } = renderHook(() => useCancelJobMutation('alpha'), { wrapper });
    result.current.mutate('j_1');
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data?.status).toBe('succeeded');
    expect(calls[0].url).toBe('/api/v1/jobs/j_1/cancel');
  });

  it('apiPut：PUT /profiles 只传脚本路径引用（不是命令）', async () => {
    const calls = recordFetch({
      'PUT /api/v1/profiles': () =>
        jsonResponse({ id: 'echo-t', label: 'Echo T', has_translator: true, has_reviewer: false }),
    });
    await apiPut('/profiles', { id: 'echo-t', translator_script: 'scripts/agy-translator.sh' });
    expect(calls[0]).toEqual({
      method: 'PUT',
      url: '/api/v1/profiles',
      body: JSON.stringify({ id: 'echo-t', translator_script: 'scripts/agy-translator.sh' }),
    });
  });

  it('apiPost/apiGet 的错误信封解析不变（网络失败 → network_error）', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new TypeError('Failed to fetch');
      }),
    );
    await expect(apiGet('/documents')).rejects.toMatchObject({ code: 'network_error', status: 0 });
    await expect(apiPost('/documents/alpha/jobs', {})).rejects.toMatchObject({
      code: 'network_error',
    });
    expect(parseErrorEnvelope({ error: { code: 'x', message: 'y' } })).toEqual({
      code: 'x',
      message: 'y',
      detail: undefined,
    });
  });

  it('apiUpload 用 FormData（不手写 content-type，浏览器补 boundary）', async () => {
    const init: RequestInit[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (_input: RequestInfo | URL, options?: RequestInit) => {
        init.push(options ?? {});
        return jsonResponse({ did: 'd', bytes: 1, source: 'source.pdf' }, 201);
      }),
    );
    await apiUpload('/documents', makePdfFile());
    expect(init[0].method).toBe('POST');
    expect(init[0].body).toBeInstanceOf(FormData);
    expect(init[0].headers).toEqual({ accept: 'application/json' });
  });
});
