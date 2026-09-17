import { describe, expect, it, vi } from 'vitest';

import { ApiError, apiGet, describeApiError, parseErrorEnvelope } from '../src/lib/api';
import { jsonResponse, textResponse } from './helpers';

describe('parseErrorEnvelope', () => {
  it('解析统一错误信封（code/message/detail）', () => {
    expect(
      parseErrorEnvelope({
        error: { code: 'document_not_found', message: '文档不存在', detail: { did: 'nope' } },
      }),
    ).toEqual({ code: 'document_not_found', message: '文档不存在', detail: { did: 'nope' } });
  });

  it('形状不符时返回 null（不把非信封体当业务错误）', () => {
    expect(parseErrorEnvelope(null)).toBeNull();
    expect(parseErrorEnvelope('boom')).toBeNull();
    expect(parseErrorEnvelope({ detail: 'boom' })).toBeNull();
    expect(parseErrorEnvelope({ error: { code: 1, message: 2 } })).toBeNull();
  });
});

describe('apiGet', () => {
  it('成功时直接返回资源 JSON 并带上 /api/v1 前缀', async () => {
    const fetchMock = vi.fn(async () => jsonResponse([{ did: 'ccs3764-dyn' }]));
    vi.stubGlobal('fetch', fetchMock);
    await expect(apiGet<{ did: string }[]>('/documents')).resolves.toEqual([
      { did: 'ccs3764-dyn' },
    ]);
    expect(fetchMock).toHaveBeenCalledWith('/api/v1/documents', {
      headers: { accept: 'application/json' },
    });
  });

  it('404 + 错误信封 → ApiError 带 code/status/detail', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        jsonResponse(
          { error: { code: 'document_not_found', message: '文档不存在', detail: { did: 'nope' } } },
          404,
        ),
      ),
    );
    const thrown = await apiGet('/documents/nope').catch((error: unknown) => error);
    expect(thrown).toBeInstanceOf(ApiError);
    const apiError = thrown as ApiError;
    expect(apiError.code).toBe('document_not_found');
    expect(apiError.status).toBe(404);
    expect(apiError.detail).toEqual({ did: 'nope' });
    expect(apiError.message).toBe('文档不存在');
  });

  it('非 JSON 错误体降级为 invalid_response', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => textResponse('<html>bad gateway</html>', 502)));
    await expect(apiGet('/documents')).rejects.toMatchObject({
      code: 'invalid_response',
      status: 502,
    });
  });

  it('连接失败 → network_error（status 0）', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new TypeError('Failed to fetch');
      }),
    );
    await expect(apiGet('/documents')).rejects.toMatchObject({ code: 'network_error', status: 0 });
  });

  it('200 但响应体不是 JSON → invalid_response（不当成「成功但没数据」）', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => textResponse('', 200)));
    await expect(apiGet('/documents')).rejects.toMatchObject({
      code: 'invalid_response',
      status: 200,
    });
  });
});

describe('describeApiError', () => {
  it('用 code 映射标题，并给出 HTTP 详情', () => {
    expect(describeApiError(new ApiError('network_error', '无法连接', 0))).toEqual({
      title: '无法连接后端服务',
      message: '无法连接',
      detail: 'network_error',
    });
    expect(describeApiError(new ApiError('document_not_found', '文档不存在', 404)).title).toBe(
      '文档不存在',
    );
    expect(describeApiError(new ApiError('weird_code', 'x', 500)).title).toBe('请求失败');
  });

  it('非 ApiError 也返回非空文案', () => {
    expect(describeApiError(new Error('boom')).message).toBe('boom');
    expect(describeApiError('boom').message).toBe('boom');
  });
});
