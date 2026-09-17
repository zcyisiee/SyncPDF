/**
 * HTTP 封装：`/api/v1` 前缀 + 统一错误信封 `{"error": {code, message, detail?}}`（docs/frontend/api.md §1）。
 * 错误一律抛 `ApiError`（带稳定 `code`），调用方按 code 分支，不要匹配 message 文案。
 */
import type { ErrorEnvelope } from '../api/types';

export const API_BASE = '/api/v1';

export class ApiError extends Error {
  readonly code: string;
  /** HTTP 状态码；连接层失败为 0。 */
  readonly status: number;
  readonly detail: unknown;

  constructor(code: string, message: string, status: number, detail?: unknown) {
    super(message);
    this.name = 'ApiError';
    this.code = code;
    this.status = status;
    this.detail = detail;
  }
}

export interface ErrorEnvelopeBody {
  code: string;
  message: string;
  detail: unknown;
}

/** 解析统一错误信封；形状不对（非 JSON、缺 code/message）返回 null，由调用方降级。 */
export function parseErrorEnvelope(body: unknown): ErrorEnvelopeBody | null {
  if (typeof body !== 'object' || body === null) return null;
  const error = (body as ErrorEnvelope).error;
  if (typeof error !== 'object' || error === null) return null;
  if (typeof error.code !== 'string' || typeof error.message !== 'string') return null;
  return { code: error.code, message: error.message, detail: error.detail };
}

export async function apiGet<T>(path: string): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, { headers: { accept: 'application/json' } });
  } catch {
    throw new ApiError(
      'network_error',
      '无法连接后端服务：请先运行 `bdt serve --root tmp --port 8787`',
      0,
    );
  }
  const text = await response.text();
  let body: unknown = null;
  if (text !== '') {
    try {
      body = JSON.parse(text);
    } catch {
      body = null;
    }
  }
  if (!response.ok) {
    const envelope = parseErrorEnvelope(body);
    if (envelope) {
      throw new ApiError(envelope.code, envelope.message, response.status, envelope.detail);
    }
    throw new ApiError('invalid_response', `HTTP ${response.status}：响应不是统一错误信封`, response.status);
  }
  if (body === null) {
    // 契约要求 JSON 资源体：空体/坏 JSON 不许当成「成功但没数据」蒙混过关
    throw new ApiError('invalid_response', `HTTP ${response.status}：响应体不是 JSON`, response.status);
  }
  return body as T;
}
const ERROR_TITLES: Record<string, string> = {
  network_error: '无法连接后端服务',
  document_not_found: '文档不存在',
  invalid_document_id: '非法的文档 id',
  path_escape: '文档路径越界',
  not_found: '端点不存在',
  root_missing: '服务根目录不可用',
  internal_error: '服务内部错误',
  invalid_response: '响应格式不符合契约',
};

export interface ApiErrorDescription {
  title: string;
  message: string;
  detail?: string;
}

/** 错误卡（DESIGN.md §4.9）用的标题/详情；未知错误也返回非空文案，不留白屏。 */
export function describeApiError(error: unknown): ApiErrorDescription {
  if (error instanceof ApiError) {
    const description: ApiErrorDescription = {
      title: ERROR_TITLES[error.code] ?? '请求失败',
      message: error.message,
    };
    if (error.detail !== undefined && error.detail !== null) {
      description.detail = JSON.stringify(error.detail);
    } else if (error.status !== 0) {
      description.detail = `HTTP ${error.status} · ${error.code}`;
    } else {
      description.detail = error.code;
    }
    return description;
  }
  return {
    title: '请求失败',
    message: error instanceof Error ? error.message : String(error),
  };
}
