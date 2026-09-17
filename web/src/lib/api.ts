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
  return request<T>(path, { headers: { accept: 'application/json' } });
}

/** `POST` JSON 体（job 提交/取消、profile 写入都用它；错误形状同 `apiGet`）。 */
export async function apiPost<T>(path: string, body?: unknown): Promise<T> {
  return request<T>(path, {
    method: 'POST',
    headers: { accept: 'application/json', 'content-type': 'application/json' },
    body: JSON.stringify(body ?? {}),
  });
}

/**
 * `PUT` JSON 体（`PUT /profiles`）：只传 profile id 与**脚本路径引用**，
 * 永远不在这里拼命令字符串（服务端也会 422 挡掉这类字段）。
 */
/**
 * `PATCH` JSON 体（`PATCH /documents/{did}/draft`）：草稿的乐观并发写入口。
 * 请求体只带 `base_revision` + `paragraphs`，服务端 409/422 的错误码由调用方分支。
 */
export async function apiPatch<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: 'PATCH',
    headers: { accept: 'application/json', 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export async function apiPut<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: 'PUT',
    headers: { accept: 'application/json', 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
}

/**
 * `POST` multipart 上传（`POST /documents`）。`FormData` 里只有 `file` 字段：
 * 目录名/did 全由服务端生成，客户端不提供任何路径或命令。
 */
export async function apiUpload<T>(path: string, file: File): Promise<T> {
  const body = new FormData();
  body.append('file', file, file.name);
  // 不手写 content-type：浏览器要自己带 multipart 的 boundary。
  return request<T>(path, { method: 'POST', headers: { accept: 'application/json' }, body });
}

/** 三个 `api*` 共用的请求+信封解析（错误一律 `ApiError`）。 */
async function request<T>(path: string, init: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, init);
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
  file_too_large: '文件过大',
  invalid_pdf: '这个文件不是 PDF',
  upload_conflict: '服务端目录名冲突',
  upload_not_supported: '当前服务不支持上传',
  document_busy: '这个文档已有任务在跑',
  revision_conflict: '草稿已被其它会话改动',
  draft_invalid: '草稿字段不合法',
  unknown_profile: 'profile 不存在',
  script_path_forbidden: '脚本不在白名单目录内',
  forbidden_field: '服务端不接受这类输入',
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
