// `bdt cloud` 接口的薄封装；约定见 docs/reference/cloud-api.md。

export type JobStatus = 'queued' | 'running' | 'recompile' | 'done' | 'partial' | 'failed' | 'canceled';
export const ACTIVE: JobStatus[] = ['queued', 'running', 'recompile'];
export const FINISHED: JobStatus[] = ['done', 'partial'];

/** 页面比例 [left, top, width, height]，相对可见页面。 */
export type Box = [number, number, number, number];

export interface Me {
  name: string;
  code: string;
  daily_quota: number;
  used: number;
  remaining: number;
}

export interface Queue {
  ahead: number;
  eta_seconds: number;
}

export interface Stats {
  pages?: number;
  elapsed_s?: number;
  warnings?: number;
  warn_pages?: number[];
  boxes?: Record<string, Box[]>;
  error?: string;
}

export interface Job {
  id: string;
  filename: string;
  status: JobStatus;
  cache_hit: boolean;
  model: string;
  thinking: string;
  size: number;
  pages: number;
  page_sizes: [number, number][];
  created_at: number;
  started_at: number | null;
  finished_at: number | null;
  stats: Stats | null;
  final_rev: string | null;
  queue: Queue | null;
  rerun: boolean;
}

export interface JobItem {
  id: string;
  filename: string;
  status: JobStatus;
  cache_hit: boolean;
  created_at: number;
  finished_at: number | null;
  warnings: number;
}

export class ApiError extends Error {
  constructor(
    readonly status: number,
    readonly code: string,
    message: string,
  ) {
    super(message);
  }
}

/** 未登录时回调（App 切回登录页）。 */
let onUnauthorized = () => {};
export function setUnauthorizedHandler(handler: () => void) {
  onUnauthorized = handler;
}

function toError(status: number, body: unknown): ApiError {
  const error = (body as { error?: { code?: string; message?: string } } | null)?.error;
  if (status === 401) onUnauthorized();
  return new ApiError(status, error?.code ?? 'http_error', error?.message ?? `请求失败（${status}）`);
}

async function request<T>(method: string, url: string, body?: unknown): Promise<T> {
  let response: Response;
  try {
    response = await fetch(url, {
      method,
      headers: body === undefined ? undefined : { 'Content-Type': 'application/json' },
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch {
    throw new ApiError(0, 'network', '网络连接失败，请稍后再试');
  }
  if (response.status === 204) return undefined as T;
  const data = await response.json().catch(() => null);
  if (!response.ok) throw toError(response.status, data);
  return data as T;
}

export const api = {
  me: () => request<Me>('GET', '/api/me'),
  login: (code: string) => request<{ ok: true }>('POST', '/api/login', { code }),
  logout: () => request<{ ok: true }>('POST', '/api/logout'),
  jobs: () => request<{ items: JobItem[] }>('GET', '/api/jobs').then((r) => r.items),
  job: (id: string) => request<Job>('GET', `/api/jobs/${id}`),
  cancel: (id: string) => request<Job>('POST', `/api/jobs/${id}/cancel`),
  rerun: (id: string, action: 'recompile' | 'retranslate') =>
    request<Job>('POST', `/api/jobs/${id}/rerun`, { action }),
  remove: (id: string) => request<void>('DELETE', `/api/jobs/${id}`),
};

export const pageUrl = (id: string, page: number, v: 'src' | 'tr', rev?: string) =>
  `/api/jobs/${id}/pages/${page}.webp?v=${v}${rev ? `&rev=${encodeURIComponent(rev)}` : ''}`;

export const downloadUrl = (id: string, kind: 'translated' | 'dual') => `/api/jobs/${id}/download?kind=${kind}`;

export const eventsUrl = (id: string) => `/api/jobs/${id}/events`;

/** multipart 上传（XHR 才有上传进度）；返回 abort 以便「取消」。 */
export function upload(
  file: File,
  options: { model: string; thinking: string },
  onProgress: (loaded: number, total: number) => void,
): { promise: Promise<Job>; abort: () => void } {
  const xhr = new XMLHttpRequest();
  const promise = new Promise<Job>((resolve, reject) => {
    const form = new FormData();
    form.append('file', file);
    form.append('model', options.model);
    form.append('thinking', options.thinking);
    xhr.upload.onprogress = (e) => onProgress(e.loaded, e.total || file.size);
    xhr.onload = () => {
      let data: unknown = null;
      try {
        data = JSON.parse(xhr.responseText);
      } catch {
        /* 非 JSON（如 nginx 413 页面）按状态码报错 */
      }
      if (xhr.status === 201) resolve(data as Job);
      else if (xhr.status === 413 && !data) reject(new ApiError(413, 'file_too_large', '文件太大了'));
      else reject(toError(xhr.status, data));
    };
    xhr.onerror = () => reject(new ApiError(0, 'network', '网络连接失败，请稍后再试'));
    xhr.onabort = () => reject(new ApiError(0, 'aborted', '已取消上传'));
    xhr.open('POST', '/api/jobs');
    xhr.send(form);
  });
  return { promise, abort: () => xhr.abort() };
}
