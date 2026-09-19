/**
 * `BatchPanel`（shift 多选 ≥2 的批量编译面板）：多选渲染与 id 摘要、
 * `POST /documents/{did}/blocks/compile` 请求体（block_ids + base_revision）、
 * job 三态（running / succeeded / failed）、提交错误（409 revision_conflict）与
 * 「取消多选」清空 ui store。
 */
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';

import { BatchPanel } from '../src/components/edit/BatchPanel';
import { uiStore } from '../src/stores/ui';
import { jsonResponse, makeJob, mockApiFetch, renderWithQuery, resetUiStore } from './helpers';

const DID = 'ccs3764-dyn';
const JOBS_PATH = `/api/v1/documents/${DID}/jobs`;
const DRAFT_PATH = `/api/v1/documents/${DID}/draft`;
const COMPILE_PATH = `/api/v1/documents/${DID}/blocks/compile`;

const DRAFT = { revision: 7, updated_at: null, paragraphs: {} };

function blocksJob(overrides: Parameters<typeof makeJob>[0] = {}) {
  return makeJob({
    did: DID,
    action: 'compile',
    status: 'running',
    revision: 7,
    requested_scope: 'blocks',
    effective_scope: 'blocks',
    paragraph_ids: ['P01-001', 'P01-002', 'P02-003'],
    ...overrides,
  });
}

function mockBatch(options: {
  draft?: unknown;
  jobs?: unknown[];
  compile?: () => Response;
} = {}) {
  const fetchMock = mockApiFetch({
    [JOBS_PATH]: () => jsonResponse(options.jobs ?? []),
    [DRAFT_PATH]: () => jsonResponse(options.draft ?? DRAFT),
    [`POST ${COMPILE_PATH}`]:
      options.compile ??
      (() => jsonResponse({ job_id: 'j_batch_1', status: 'queued', action: 'compile' }, 202)),
  });
  return fetchMock;
}

/** POST /blocks/compile 的请求体（从 mock fetch 调用里取）。 */
function compileBody(fetchMock: ReturnType<typeof mockApiFetch>) {
  const call = fetchMock.mock.calls.find(
    ([input, init]) => String(input) === COMPILE_PATH && init?.method === 'POST',
  );
  expect(call, '没有发出 POST /blocks/compile').toBeTruthy();
  return JSON.parse(String(call?.[1]?.body)) as { block_ids: string[]; base_revision: number };
}

beforeEach(() => {
  resetUiStore();
});

describe('BatchPanel 渲染', () => {
  it('多选 ≥2：显示已选块数与 id 摘要（超过 3 个折叠成 …）', async () => {
    mockBatch();
    renderWithQuery(
      <BatchPanel did={DID} blockIds={['P01-001', 'P01-002', 'P02-003', 'P03-004']} />,
    );
    expect(await screen.findByText('已选 4 块')).toBeInTheDocument();
    expect(document.querySelector('[data-od-id="batch-ids"]')?.textContent).toBe(
      'P01-001 P01-002 P02-003 …',
    );
    expect(screen.getByRole('button', { name: '批量编译' })).toBeInTheDocument();
  });

  it('「取消多选」清空 ui store 的多选集合', async () => {
    uiStore.setState({ selectedParagraphIds: ['P01-001', 'P01-002'], selectedParagraphId: 'P01-002' });
    mockBatch();
    renderWithQuery(<BatchPanel did={DID} blockIds={['P01-001', 'P01-002']} />);
    fireEvent.click(await screen.findByRole('button', { name: '取消多选' }));
    expect(uiStore.getState().selectedParagraphIds).toEqual([]);
    expect(uiStore.getState().selectedParagraphId).toBeNull();
  });
});

describe('BatchPanel 提交批量编译', () => {
  /** 草稿 revision 是提交前提：先等按钮从「草稿未加载」的禁用态里恢复。 */
  async function enabledCompileButton() {
    const button = await screen.findByRole('button', { name: '批量编译' });
    await waitFor(() => expect(button).toBeEnabled());
    return button;
  }

  it('点击「批量编译」→ POST /blocks/compile，body 带 block_ids 与草稿 base_revision', async () => {
    const fetchMock = mockBatch();
    renderWithQuery(
      <BatchPanel did={DID} blockIds={['P01-001', 'P01-002', 'P02-003']} />,
    );
    fireEvent.click(await enabledCompileButton());
    await waitFor(() => expect(compileBody(fetchMock).block_ids).toBeTruthy());
    expect(compileBody(fetchMock)).toEqual({
      block_ids: ['P01-001', 'P01-002', 'P02-003'],
      base_revision: 7,
    });
  });

  it('409 revision_conflict → 提交错误条展示 describeApiError 的文案', async () => {
    mockBatch({
      compile: () =>
        jsonResponse(
          {
            error: {
              code: 'revision_conflict',
              message: '草稿 revision 已变化',
              detail: { current_revision: 9 },
            },
          },
          409,
        ),
    });
    renderWithQuery(<BatchPanel did={DID} blockIds={['P01-001', 'P01-002']} />);
    fireEvent.click(await enabledCompileButton());
    const error = await screen.findByText(/草稿 revision 已变化/);
    expect(error.closest('[data-od-id="batch-submit-error"]')).not.toBeNull();
  });

  it('404 block_not_found → 提交错误条', async () => {
    mockBatch({
      compile: () =>
        jsonResponse(
          { error: { code: 'block_not_found', message: '文档中没有该 block：P09-999' } },
          404,
        ),
    });
    renderWithQuery(<BatchPanel did={DID} blockIds={['P01-001', 'P09-999']} />);
    fireEvent.click(await enabledCompileButton());
    expect(await screen.findByText(/文档中没有该 block：P09-999/)).toBeInTheDocument();
  });
});

describe('BatchPanel job 状态三态', () => {
  it('running → 「批量编译中…（SSE block_compiled 事件可近实时）」', async () => {
    mockBatch({ jobs: [blocksJob({ status: 'running' })] });
    renderWithQuery(<BatchPanel did={DID} blockIds={['P01-001', 'P01-002']} />);
    expect(await screen.findByText(/批量编译中…（SSE block_compiled 事件可近实时）/)).toBeInTheDocument();
  });

  it('succeeded → 「已编译 N 块」（N 取 job envelope 的 blocks 计数）', async () => {
    mockBatch({
      jobs: [
        blocksJob({
          status: 'succeeded',
          envelope: JSON.stringify({ blocks: 3, pages: [1, 2], duration_s: 4.2 }),
        }),
      ],
    });
    renderWithQuery(<BatchPanel did={DID} blockIds={['P01-001', 'P01-002', 'P02-003']} />);
    expect(await screen.findByText('已编译 3 块')).toBeInTheDocument();
  });

  it('failed → 错误条展示 error_message（失败块清单在后端 detail 里，不保证结构）', async () => {
    mockBatch({
      jobs: [
        blocksJob({
          status: 'failed',
          error_code: 'compile_failed',
          error_message: '2 个 block 编译失败（其余 1 个已发布）',
        }),
      ],
    });
    renderWithQuery(<BatchPanel did={DID} blockIds={['P01-001', 'P01-002', 'P02-003']} />);
    expect(await screen.findByText('2 个 block 编译失败（其余 1 个已发布）')).toBeInTheDocument();
    expect(document.querySelector('[data-od-id="batch-job-error"]')).not.toBeNull();
  });

  it('revision 不匹配当前草稿的旧 blocks job 不展示（草稿又动过就不冒充这一批的结果）', async () => {
    mockBatch({ jobs: [blocksJob({ status: 'succeeded', revision: 6 })] });
    renderWithQuery(<BatchPanel did={DID} blockIds={['P01-001', 'P01-002']} />);
    await screen.findByText('已选 2 块');
    expect(document.querySelector('[data-od-id="batch-job-succeeded"]')).toBeNull();
  });
});
