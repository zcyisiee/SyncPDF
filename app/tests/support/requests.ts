/** 测试共用的合法请求（与 Rust request.rs 同形）。 */
import type { ConfigureRequest, RunRequest } from '../../src/shared/protocol';

export const FAKE_SIDECAR = `${__dirname}/../../scripts/fake-sidecar.mjs`;

export function configureRequest(overrides: Partial<ConfigureRequest> = {}): ConfigureRequest {
  return {
    type: 'configure',
    provider: 'agy',
    base_url: null,
    model: 'gemini-3.8-flash-low',
    api_key: null,
    concurrency: 1,
    cache_dir: '/tmp/syncpdf-test-cache',
    translator: { kind: 'agy', program: 'agy', model: 'gemini-3.8-flash-low' },
    ...overrides,
  };
}

export function runRequest(docId: string, overrides: Partial<RunRequest> = {}): RunRequest {
  return {
    type: 'run',
    doc_id: docId,
    input: '/nonexistent/in.pdf',
    output: '/nonexistent/out.pdf',
    source_lang: 'en',
    target_lang: 'zh-CN',
    pages: null,
    font_profile: null,
    terminology: null,
    mode: 'full',
    store: null,
    ...overrides,
  };
}
