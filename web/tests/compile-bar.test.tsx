/**
 * 编译状态条（`CompileBar`）：running / failed / stale / ok 四分支 + 手动编译与重试提交
 * `POST /blocks/{selectedId}/compile {base_revision}`，禁止回退全文编译。
 *
 * `error_code` 取自 `GET /documents/{did}/jobs` 里最近一条 `action=compile`（契约的
 * `compile` 字段不带错误码），所以这里也顺带钉住「不编造错误码」这条。
 */
import { screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';

import { uiStore } from '../src/stores/ui';
import { CompileBar } from '../src/components/preview/CompileBar';
import { jsonResponse, makeJob, mockApiFetch, renderWithQuery, resetUiStore } from './helpers';

const DID = 'ccs3764-dyn';
const JOBS_PATH = `/api/v1/documents/${DID}/jobs`;

function mockBar(jobs: unknown = []) {
  return mockApiFetch({
    [JOBS_PATH]: () => jsonResponse(jobs),
    [`POST /api/v1/documents/${DID}/blocks/P01-010/compile`]: () =>
      jsonResponse({ job_id: 'j_compile', status: 'queued', action: 'compile' }, 202),
  });
}

beforeEach(() => {
  resetUiStore();
  uiStore.setState({ selectedParagraphId: 'P01-010' });
});

describe('CompileBar 状态', () => {
  it('切换到第二个块不显示历史全文失败', async () => {
    mockBar([makeJob({ action: 'compile', effective_scope: 'block', paragraph_id: 'P01-001', revision: 3, status: 'succeeded' })]);
    renderWithQuery(<CompileBar did={DID} compile={{ status: 'failed', revision: 1, stale: true }} draftRevision={4} />);
    await waitFor(() => expect(screen.queryByText('上次编译失败')).toBeNull());
    expect(screen.getByRole('button', { name: '编译选中块' })).toBeEnabled();
  });
  it('当前块当前修订的失败仍显示真实错误', async () => {
    mockBar([makeJob({ action: 'compile', effective_scope: 'block', paragraph_id: 'P01-010', revision: 4, status: 'failed', error_message: '贴片失败' })]);
    renderWithQuery(<CompileBar did={DID} compile={{ status: 'ok', revision: 1, stale: true }} draftRevision={4} />);
    expect(await screen.findByText('贴片失败')).toBeInTheDocument();
  });
  it('未选中 bbox 时禁用编译，不回退全文', () => {
    mockBar();
    uiStore.setState({ selectedParagraphId: null });
    renderWithQuery(<CompileBar did={DID} compile={{ status: 'ok', revision: 1, stale: true }} draftRevision={2} />);
    expect(screen.getByRole('button', { name: '编译选中块' })).toBeDisabled();
    expect(screen.getByText(/请先选中一个译文框/)).toBeInTheDocument();
  });
  it('running：编译中 + 脉冲 + 编辑已禁用（不发任何请求也看得见）', async () => {
    mockBar();
    renderWithQuery(
      <CompileBar did={DID} compile={{ status: 'running', revision: 5, stale: true }} draftRevision={6} />,
    );
    const bar = document.querySelector('[data-od-id="compile-bar"]');
    expect(bar).toHaveAttribute('data-compile-status', 'running');
    expect(bar?.textContent).toContain('编译中…');
    expect(bar?.textContent).toContain('编辑已禁用');
    expect(bar?.querySelector('.pulse-dot')).not.toBeNull();
    expect(screen.queryByRole('button', { name: /手动编译|重试编译/ })).toBeNull();
  });

  it('stale：草稿比 PDF 新（两个修订号都写出来）+「手动编译」按钮', async () => {
    const fetchMock = mockBar();
    renderWithQuery(
      <CompileBar did={DID} compile={{ status: 'ok', revision: 5, stale: true, artifact: { name: 'paper.mono.pdf', revision: 5 } }} draftRevision={6} />,
    );
    const bar = document.querySelector('[data-od-id="compile-bar"]');
    expect(bar).toHaveAttribute('data-compile-status', 'stale');
    expect(bar?.textContent).toContain('PDF 修订 r5');
    expect(bar?.textContent).toContain('草稿 r6');

    screen.getByRole('button', { name: '编译选中块' }).click();
    await waitFor(() => {
      const call = fetchMock.mock.calls.find(
        ([input, init]) => String(input) === `/api/v1/documents/${DID}/blocks/P01-010/compile` && init?.method === 'POST',
      );
      expect(call).toBeTruthy();
      expect(fetchMock.mock.calls.some(([input, init]) => String(input) === JOBS_PATH && init?.method === 'POST')).toBe(false);
      expect(JSON.parse(String(call?.[1]?.body))).toEqual({
        base_revision: 6,
      });
    });
  });

  it('failed：错误条带真实 error_code（来自 compile job）+ 重试按钮 + 仍是 r{n}', async () => {
    mockBar([
      makeJob({ action: 'compile', status: 'failed', error_code: 'build_failed', error_message: 'build 阶段未成功（子进程退出码 1）' }),
    ]);
    renderWithQuery(
      <CompileBar did={DID} compile={{ status: 'failed', revision: 4, stale: true, artifact: { name: 'paper.mono.pdf', revision: 4 } }} draftRevision={5} />,
    );
    const bar = document.querySelector('[data-od-id="compile-bar"]');
    expect(bar).toHaveAttribute('data-compile-status', 'failed');
    // 真冒烟发现的回归：react-query 无错时 `error` 是 null，不许渲染成 `请求失败：null`
    expect(document.querySelector('[data-od-id="compile-submit-error"]')).toBeNull();
    // job 列表是异步到达的：等错误码渲染出来
    expect(await screen.findByText(/build 阶段未成功/)).toBeInTheDocument();
    expect(document.querySelector('[data-od-id="compile-error"]')?.textContent).toContain(
      'error_code: build_failed',
    );
    expect(bar?.textContent).toContain('仍是 r4');
    expect(screen.getByRole('button', { name: '重试编译' })).toBeInTheDocument();
    // 冒烟发现的回归：react-query 无错时 error 是 null，不许渲染成 `请求失败：null`
    expect(document.querySelector('[data-od-id="compile-submit-error"]')).toBeNull();
  });

  it('failed 但没有 compile job 记录：不编造错误码，只给「最近一次编译没有成功」', async () => {
    mockBar([]);
    renderWithQuery(
      <CompileBar did={DID} compile={{ status: 'failed', revision: 0, stale: false, artifact: null }} draftRevision={1} />,
    );
    const error = document.querySelector('[data-od-id="compile-error"]');
    expect(error?.textContent).not.toContain('error_code');
    expect(error?.textContent).toContain('最近一次编译没有成功');
  });

  it('ok 且不 stale：不渲染常驻绿条（修订号与质量徽标在下载按钮上）', async () => {
    mockBar();
    renderWithQuery(
      <CompileBar did={DID} compile={{ status: 'ok', revision: 7, stale: false, artifact: { name: 'paper.mono.pdf', revision: 7 } }} draftRevision={7} />,
    );
    expect(document.querySelector('[data-od-id="compile-bar"]')).toBeNull();
  });

  it('none 且没有产物：不渲染状态条（下载按钮自己解释为什么禁用）', async () => {
    mockBar();
    renderWithQuery(
      <CompileBar did={DID} compile={{ status: 'none', revision: 0, stale: false, artifact: null }} draftRevision={0} />,
    );
    expect(document.querySelector('[data-od-id="compile-bar"]')).toBeNull();
  });

  it('有活动 job 时手动编译按钮禁用（服务端也会 409 document_busy）', async () => {
    mockBar();
    renderWithQuery(
      <CompileBar
        did={DID}
        compile={{ status: 'ok', revision: 5, stale: true, artifact: { name: 'paper.mono.pdf', revision: 5 } }}
        draftRevision={6}
        busyJobId="j_running"
      />,
    );
    expect(screen.getByRole('button', { name: '编译选中块' })).toBeDisabled();
    expect(document.querySelector('[data-od-id="compile-bar"]')?.textContent).toContain('有任务在跑');
  });

  it('拿不到草稿 revision（草稿查询失败）时不给手动编译（避免 409 假动作）', async () => {
    mockBar();
    renderWithQuery(
      <CompileBar did={DID} compile={{ status: 'ok', revision: 5, stale: true, artifact: null }} draftRevision={null} />,
    );
    expect(screen.getByRole('button', { name: '编译选中块' })).toBeDisabled();
    expect(document.querySelector('[data-od-id="compile-bar"]')?.textContent).toContain('草稿 r?');
  });
});
