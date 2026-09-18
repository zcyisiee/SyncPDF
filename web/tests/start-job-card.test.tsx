import { fireEvent, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';

import type { ArtifactItem, DocumentDetail } from '../src/api/types';
import { StartJobCard } from '../src/components/jobs/StartJobCard';
import { resetUiStore, jsonResponse, mockApiFetch, renderWithQuery } from './helpers';

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
  { id: 'echo-t', label: 'Echo T', has_translator: true, has_reviewer: false },
  { id: 'deepseek-flash', label: 'Deepseek Flash', has_translator: true, has_reviewer: true },
];

/** 全局词表（W13）：默认非空，所以开关默认开着且可用。 */
const GLOSSARY = {
  entries: [
    { source: 'attention', target: '注意力', note: null },
    { source: 'LTO', target: '链接时优化', note: null },
  ],
  count: 2,
};

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

function routes(extra: Record<string, () => Response> = {}) {
  return {
    '/api/v1/documents/up-sample-20260917-120000/artifacts': () => jsonResponse(ARTIFACTS),
    '/api/v1/profiles': () => jsonResponse(PROFILES),
    '/api/v1/models': () => jsonResponse([]),
    // W13：卡上的词表开关要读全局词表条数（空表 → 开关禁用 + 提示）
    '/api/v1/glossary': () => jsonResponse(GLOSSARY),
    ...extra,
  };
}

beforeEach(() => {
  resetUiStore();
});

describe('StartJobCard（开始翻译配置卡）', () => {
  it('没有产物时不显示（空目录没有可跑的东西，不造假按钮）', async () => {
    mockApiFetch(
      routes({
        '/api/v1/documents/up-sample-20260917-120000/artifacts': () => jsonResponse([]),
      }),
    );
    renderWithQuery(<StartJobCard did="up-sample-20260917-120000" document={makeDocument()} />);
    await waitFor(() =>
      expect(
        screen.queryByRole('button', { name: '开始翻译' }),
      ).toBeNull(),
    );
  });

  it('有 source.pdf 时渲染表单：profile 下拉 + 页码 + dual + 高级 from', async () => {
    mockApiFetch(routes());
    renderWithQuery(<StartJobCard did="up-sample-20260917-120000" document={makeDocument()} />);

    const submit = await screen.findByRole('button', { name: '开始翻译' });
    expect(submit).toBeEnabled();
    const profile = screen.getByLabelText('翻译配置') as HTMLSelectElement;
    expect(profile.value).toBe('echo-t');
    expect(screen.getByLabelText('页码范围（留空 = 全部）')).toHaveAttribute(
      'placeholder',
      '1-3,5 全部留空',
    );
    expect(screen.getByLabelText(/生成 dual/)).not.toBeChecked();
    // 没有 parse 产物 → 默认从 parse 起，并提示 MinerU 约束
    expect((screen.getByLabelText('起点阶段') as HTMLSelectElement).value).toBe('parse');
    expect(screen.getByText(/MINERU_API_TOKEN/)).toBeInTheDocument();
  });

  it('已有 parse 产物 → 默认从 translate 续跑（不再跑 MinerU）', async () => {
    mockApiFetch(routes());
    renderWithQuery(
      <StartJobCard
        did="up-sample-20260917-120000"
        document={makeDocument({ stage_summary: { ...STAGES_NOT_RUN, parse: 'ok' } })}
      />,
    );
    const from = await screen.findByLabelText('起点阶段');
    expect((from as HTMLSelectElement).value).toBe('translate');
    expect(screen.queryByText(/MINERU_API_TOKEN/)).toBeNull();
  });

  it('提交时只发服务端接受的字段（action/from/pages/dual/profile/use_glossary）', async () => {
    const calls: { url: string; body: unknown }[] = [];
    const fetchMock = mockApiFetch(routes());
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/jobs')) {
        calls.push({ url, body: JSON.parse(String(init?.body)) });
        return jsonResponse({ job_id: 'j_1', status: 'queued', action: 'run' }, 202);
      }
      const all = routes();
      const route = all[url as keyof typeof all];
      return route ? route() : jsonResponse({ error: { code: 'not_found', message: url } }, 404);
    });

    renderWithQuery(<StartJobCard did="up-sample-20260917-120000" document={makeDocument()} />);
    fireEvent.change(await screen.findByLabelText('页码范围（留空 = 全部）'), {
      target: { value: '1-3,5' },
    });
    fireEvent.click(screen.getByLabelText(/生成 dual/));
    fireEvent.click(screen.getByRole('button', { name: '开始翻译' }));

    await waitFor(() => expect(calls).toHaveLength(1));
    expect(calls[0].url).toBe('/api/v1/documents/up-sample-20260917-120000/jobs');
    expect(calls[0].body).toEqual({
      action: 'run',
      from: 'parse',
      pages: '1-3,5',
      dual: true,
      profile: 'echo-t',
      // 词表只有这一个布尔字段（内容/路径全在服务端）
      use_glossary: true,
    });
  });

  it('页码形状不对时禁用提交并给出提示', async () => {
    mockApiFetch(routes());
    renderWithQuery(<StartJobCard did="up-sample-20260917-120000" document={makeDocument()} />);
    fireEvent.change(await screen.findByLabelText('页码范围（留空 = 全部）'), {
      target: { value: '1-3;rm -rf /' },
    });
    expect(await screen.findByText(/页码范围形状不对/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '开始翻译' })).toBeDisabled();
  });

  it('没有 profile 时给出可操作提示并禁用提交', async () => {
    mockApiFetch(routes({ '/api/v1/profiles': () => jsonResponse([]) }));
    renderWithQuery(<StartJobCard did="up-sample-20260917-120000" document={makeDocument()} />);
    expect(await screen.findByText(/没有可用翻译配置：/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: '开始翻译' })).toBeDisabled();
  });

  it('服务端 409 document_busy → 错误卡显示人话标题', async () => {
    mockApiFetch(
      routes({
        '/api/v1/documents/up-sample-20260917-120000/jobs': () =>
          jsonResponse(
            {
              error: {
                code: 'document_busy',
                message: '文档已有活动 job：j_1（running）',
                detail: { job_id: 'j_1' },
              },
            },
            409,
          ),
      }),
    );
    renderWithQuery(<StartJobCard did="up-sample-20260917-120000" document={makeDocument()} />);
    fireEvent.click(await screen.findByRole('button', { name: '开始翻译' }));
    const alert = await screen.findByRole('alert');
    expect(alert).toHaveTextContent('这个文档已有任务在跑');
    expect(alert).toHaveTextContent('文档已有活动 job：j_1（running）');
  });

  it('词表非空：开关默认开着（并标出条数），关掉后提交 use_glossary=false', async () => {
    const bodies: unknown[] = [];
    const fetchMock = mockApiFetch(routes());
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/jobs')) {
        bodies.push(JSON.parse(String(init?.body)));
        return jsonResponse({ job_id: 'j_1', status: 'queued', action: 'run' }, 202);
      }
      const all = routes();
      const route = all[url as keyof typeof all];
      return route ? route() : jsonResponse({ error: { code: 'not_found', message: url } }, 404);
    });

    renderWithQuery(<StartJobCard did="up-sample-20260917-120000" document={makeDocument()} />);
    const toggle = await screen.findByLabelText(/使用词表/);
    expect(toggle).toBeChecked();
    expect(toggle).toBeEnabled();
    expect(screen.getByText('2 条')).toBeInTheDocument();

    fireEvent.click(toggle); // 关掉这一次的注入
    expect(toggle).not.toBeChecked();
    fireEvent.click(screen.getByRole('button', { name: '开始翻译' }));
    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toMatchObject({ use_glossary: false });
  });

  it('词表为空：开关禁用、恒为关、提示「词表为空」，提交仍带 use_glossary=false', async () => {
    const bodies: unknown[] = [];
    const fetchMock = mockApiFetch(
      routes({ '/api/v1/glossary': () => jsonResponse({ entries: [], count: 0 }) }),
    );
    fetchMock.mockImplementation(async (input: RequestInfo | URL, init?: RequestInit) => {
      const url = String(input);
      if (url.endsWith('/jobs')) {
        bodies.push(JSON.parse(String(init?.body)));
        return jsonResponse({ job_id: 'j_1', status: 'queued', action: 'run' }, 202);
      }
      const all = routes({ '/api/v1/glossary': () => jsonResponse({ entries: [], count: 0 }) });
      const route = all[url as keyof typeof all];
      return route ? route() : jsonResponse({ error: { code: 'not_found', message: url } }, 404);
    });

    renderWithQuery(<StartJobCard did="up-sample-20260917-120000" document={makeDocument()} />);
    const toggle = await screen.findByLabelText(/使用词表/);
    expect(await screen.findByText('（词表为空）')).toBeInTheDocument();
    expect(toggle).toBeDisabled();
    expect(toggle).not.toBeChecked();

    fireEvent.click(screen.getByRole('button', { name: '开始翻译' }));
    await waitFor(() => expect(bodies).toHaveLength(1));
    expect(bodies[0]).toMatchObject({ use_glossary: false });
  });
});
