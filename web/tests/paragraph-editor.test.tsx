/**
 * `ParagraphEditor`：草稿优先显示、本地 1.5s 防抖保存、失焦/Cmd+S 立即保存、
 * 409 两分支（revision_conflict / document_busy）与 422 draft_invalid、编译中只读、
 * 恢复基线、排版参数范围校验，以及「编译样式」区（源文派生摘要 + 三态覆盖下拉）。
 */
import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { ParagraphEditor, SAVE_DEBOUNCE_MS } from '../src/components/edit/ParagraphEditor';
import { jsonResponse, mockApiFetch, renderWithQuery, resetUiStore } from './helpers';

const DID = 'ccs3764-dyn';
const PARAGRAPHS_PATH = `/api/v1/documents/${DID}/paragraphs`;
const DRAFT_PATH = `/api/v1/documents/${DID}/draft`;

const PARAGRAPHS = [
  {
    id: 'P05-002',
    page: 5,
    layout_label: 'text',
    source: 'Source text of P05-002.',
    target: '基线译文',
    geometry: { id: 'P05-002', page: 5, layout_label: 'text', layout_box: [72, 640, 520, 780] },
    style: { font_size: 10.5, bold: true, italic: false, serif: true, font_name: 'TimesNewRomanPSMT' },
    layout_status: 'ok',
  },
];

const DRAFT_EMPTY = { revision: 0, updated_at: null, paragraphs: {} };

/** 一段带草稿覆盖的草稿（译文 + 排版数值）。 */
const DRAFT_WITH_OVERRIDE = {
  revision: 3,
  updated_at: '2026-09-17T15:54:45.123Z',
  paragraphs: {
    'P05-002': {
      target: '草稿译文',
      layout: { font_scale: 1.05, box: [70, 630, 522, 782] },
      updated_at: '2026-09-17T15:54:45.123Z',
    },
  },
};

function mockEditor(options: {
  draft?: unknown;
  patch?: () => Response;
  paragraphsStatus?: number;
  paragraphs?: unknown;
} = {}) {
  const fetchMock = mockApiFetch({
    [PARAGRAPHS_PATH]: () =>
      options.paragraphsStatus === undefined
        ? jsonResponse(options.paragraphs ?? PARAGRAPHS)
        : jsonResponse(
            { error: { code: 'paragraphs_unavailable', message: '没有段落产物' } },
            options.paragraphsStatus,
          ),
    [DRAFT_PATH]: () => jsonResponse(options.draft ?? DRAFT_EMPTY),
    [`PATCH ${DRAFT_PATH}`]: options.patch ?? (() => jsonResponse({ ...DRAFT_EMPTY, revision: 1 })),
  });
  return fetchMock;
}

/** PATCH 请求体（从 mock fetch 的调用里取）。 */
function patchBody(fetchMock: ReturnType<typeof mockApiFetch>) {
  const call = fetchMock.mock.calls.find(
    ([input, init]) => String(input) === DRAFT_PATH && init?.method === 'PATCH',
  );
  expect(call, '没有发出 PATCH /draft').toBeTruthy();
  return JSON.parse(String(call?.[1]?.body)) as {
    base_revision: number;
    paragraphs: Record<string, unknown>;
  };
}

beforeEach(() => {
  resetUiStore();
  vi.useFakeTimers({ shouldAdvanceTime: true });
});

afterEach(() => {
  vi.useRealTimers();
});

describe('ParagraphEditor 显示口径', () => {
  it('无选中段：空态提示（不显示输入框）', async () => {
    mockEditor();
    renderWithQuery(<ParagraphEditor did={DID} paragraphId={null} />);
    expect(screen.getByText(/点击预览里的原文框查看该段/)).toBeInTheDocument();
    expect(document.querySelector('[data-od-id="paragraph-editor-empty"]')).not.toBeNull();
    expect(document.querySelector('[data-od-id="paragraph-target"]')).toBeNull();
  });

  it('有选中段但没有草稿覆盖：显示基线译文，无「草稿已修改」', async () => {
    mockEditor();
    renderWithQuery(<ParagraphEditor did={DID} paragraphId={'P05-002'} />);
    const target = (await screen.findByLabelText('译文')) as HTMLTextAreaElement;
    expect(target.value).toBe('基线译文');
    expect(document.querySelector('[data-od-id="paragraph-editor-modified"]')).toBeNull();
    expect(screen.getByText('Source text of P05-002.')).toBeInTheDocument();
    expect(document.querySelector('[data-od-id="draft-revision"]')?.textContent).toBe('草稿 r0');
    // box 只读显示（草稿没有 box → 用几何基线）
    expect(document.querySelector('[data-od-id="paragraph-box"]')?.textContent).toContain(
      '72.00, 640.00, 520.00, 780.00',
    );
  });

  it('草稿覆盖优先于基线，并给出「草稿已修改」+ 恢复按钮 + 排版覆盖值', async () => {
    mockEditor({ draft: DRAFT_WITH_OVERRIDE });
    renderWithQuery(<ParagraphEditor did={DID} paragraphId={'P05-002'} />);
    const target = (await screen.findByLabelText('译文')) as HTMLTextAreaElement;
    expect(target.value).toBe('草稿译文');
    expect(
      document.querySelector('[data-od-id="paragraph-editor-modified"]')?.textContent,
    ).toContain('草稿已修改');
    expect(document.querySelector('[data-od-id="draft-revision"]')?.textContent).toBe('草稿 r3');
    expect(
      (document.querySelector('[data-od-id="paragraph-layout-font_scale"]') as HTMLInputElement)
        .value,
    ).toBe('1.05');
    // 草稿里的 box 优先于基线几何
    expect(document.querySelector('[data-od-id="paragraph-box"]')?.textContent).toContain(
      '70.00, 630.00, 522.00, 782.00',
    );
    expect(screen.getByRole('button', { name: '恢复基线' })).toBeInTheDocument();
  });
});

describe('ParagraphEditor 保存', () => {
  it('输入后 1.5s 防抖自动保存：PATCH 带 base_revision + 只发变化字段', async () => {
    const fetchMock = mockEditor();
    renderWithQuery(<ParagraphEditor did={DID} paragraphId={'P05-002'} />);
    const target = (await screen.findByLabelText('译文')) as HTMLTextAreaElement;

    fireEvent.change(target, { target: { value: '改后的译文' } });
    expect(screen.getByText(/有未保存改动/)).toBeInTheDocument();
    expect(
      fetchMock.mock.calls.some(([, init]) => init?.method === 'PATCH'),
    ).toBe(false);

    await act(async () => {
      vi.advanceTimersByTime(SAVE_DEBOUNCE_MS + 50);
    });
    await waitFor(() => expect(patchBody(fetchMock).paragraphs).toBeTruthy());
    const body = patchBody(fetchMock);
    expect(body.base_revision).toBe(0);
    expect(body.paragraphs).toEqual({ 'P05-002': { target: '改后的译文' } });
  });

  it('Cmd+S 立即保存；失焦也保存', async () => {
    const fetchMock = mockEditor();
    renderWithQuery(<ParagraphEditor did={DID} paragraphId={'P05-002'} />);
    const target = (await screen.findByLabelText('译文')) as HTMLTextAreaElement;
    fireEvent.change(target, { target: { value: 'A' } });
    fireEvent.keyDown(target, { key: 's', metaKey: true });
    await waitFor(() => expect(patchBody(fetchMock).paragraphs).toEqual({ 'P05-002': { target: 'A' } }));

    fireEvent.change(target, { target: { value: 'AB' } });
    fireEvent.blur(target);
    await waitFor(() =>
      expect(fetchMock.mock.calls.filter(([, init]) => init?.method === 'PATCH')).toHaveLength(2),
    );
  });

  it('改回基线值 → PATCH target:null（删掉覆盖，而不是写一份与基线相同的覆盖）', async () => {
    const fetchMock = mockEditor({ draft: DRAFT_WITH_OVERRIDE });
    renderWithQuery(<ParagraphEditor did={DID} paragraphId={'P05-002'} />);
    const target = (await screen.findByLabelText('译文')) as HTMLTextAreaElement;
    await waitFor(() => expect(target.value).toBe('草稿译文'));
    fireEvent.change(target, { target: { value: '基线译文' } });
    fireEvent.blur(target);
    await waitFor(() => expect(patchBody(fetchMock).paragraphs['P05-002']).toEqual({ target: null }));
    expect(patchBody(fetchMock).base_revision).toBe(3);
  });

  it('排版数值超范围：输入框旁给出范围提示且不发 PATCH', async () => {
    const fetchMock = mockEditor();
    renderWithQuery(<ParagraphEditor did={DID} paragraphId={'P05-002'} />);
    const input = (await screen.findByLabelText(/行距倍数/)) as HTMLInputElement;
    fireEvent.change(input, { target: { value: '9' } });
    expect(await screen.findByText('范围 0.8–3')).toBeInTheDocument();
    await act(async () => {
      vi.advanceTimersByTime(SAVE_DEBOUNCE_MS * 3);
    });
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'PATCH')).toBe(false);
    expect(screen.getByRole('button', { name: '保存' })).toBeDisabled();
  });

  it('改排版数值 → layout 整对象补丁（带上草稿里已有的 box，不丢）', async () => {
    const fetchMock = mockEditor({ draft: DRAFT_WITH_OVERRIDE });
    renderWithQuery(<ParagraphEditor did={DID} paragraphId={'P05-002'} />);
    const input = (await screen.findByLabelText(/字号缩放/)) as HTMLInputElement;
    await waitFor(() => expect(input.value).toBe('1.05'));
    fireEvent.change(input, { target: { value: '1.2' } });
    fireEvent.blur(input);
    await waitFor(() => expect(patchBody(fetchMock).paragraphs['P05-002']).toBeTruthy());
    expect(patchBody(fetchMock).paragraphs['P05-002']).toEqual({
      layout: { font_scale: 1.2, box: [70, 630, 522, 782] },
    });
  });
});

describe('ParagraphEditor 编译样式区', () => {
  it('只读行显示源文派生值：字号/字体名/加粗/斜体/衬线（font_scale 覆盖时带 × n）', async () => {
    mockEditor({ draft: DRAFT_WITH_OVERRIDE }); // 草稿里 font_scale: 1.05
    renderWithQuery(<ParagraphEditor did={DID} paragraphId={'P05-002'} />);
    await screen.findByLabelText('译文');
    const info = document.querySelector('[data-od-id="paragraph-style-info"]');
    expect(info?.textContent).toContain('字号 10.5pt');
    expect(info?.textContent).toContain('× 1.05');
    expect(info?.textContent).toContain('TimesNewRomanPSMT');
    expect(info?.textContent).toContain('加粗 是');
    expect(info?.textContent).toContain('斜体 否');
    expect(info?.textContent).toContain('衬线 是');
    // 三个三态下拉默认「跟随原文」（草稿里没有样式键）
    for (const key of ['bold', 'italic', 'serif']) {
      expect((document.querySelector(`[data-od-id="paragraph-style-${key}"]`) as HTMLSelectElement).value).toBe('');
    }
  });

  it('style 为 null：整区显示「样式信息不可用」，不渲染下拉', async () => {
    mockEditor({ paragraphs: [{ ...PARAGRAPHS[0], style: null }] });
    renderWithQuery(<ParagraphEditor did={DID} paragraphId={'P05-002'} />);
    await screen.findByLabelText('译文');
    expect(document.querySelector('[data-od-id="paragraph-style-unavailable"]')).not.toBeNull();
    expect(document.querySelector('[data-od-id="paragraph-style-info"]')).toBeNull();
    expect(document.querySelector('[data-od-id="paragraph-style-bold"]')).toBeNull();
  });

  it('下拉切「开启」→ 防抖 PATCH 的 layout 含 bold:true（box/数值不丢）', async () => {
    const fetchMock = mockEditor({ draft: DRAFT_WITH_OVERRIDE });
    renderWithQuery(<ParagraphEditor did={DID} paragraphId={'P05-002'} />);
    await screen.findByLabelText('译文');
    fireEvent.change(document.querySelector('[data-od-id="paragraph-style-bold"]') as HTMLSelectElement, {
      target: { value: 'on' },
    });
    await act(async () => {
      vi.advanceTimersByTime(SAVE_DEBOUNCE_MS + 50);
    });
    await waitFor(() => expect(patchBody(fetchMock).paragraphs['P05-002']).toBeTruthy());
    expect(patchBody(fetchMock).paragraphs['P05-002']).toEqual({
      // layout 整对象替换：带上草稿里已有的 font_scale 与 box，再写 bold
      layout: { font_scale: 1.05, box: [70, 630, 522, 782], bold: true },
    });
  });

  it('草稿里已有 bold:true，切回「跟随原文」→ 防抖 PATCH 的 layout 无 bold（删键）', async () => {
    const draftWithBold = {
      ...DRAFT_WITH_OVERRIDE,
      paragraphs: {
        'P05-002': {
          ...DRAFT_WITH_OVERRIDE.paragraphs['P05-002'],
          layout: { font_scale: 1.05, box: [70, 630, 522, 782], bold: true },
        },
      },
    };
    const fetchMock = mockEditor({ draft: draftWithBold });
    renderWithQuery(<ParagraphEditor did={DID} paragraphId={'P05-002'} />);
    await screen.findByLabelText('译文');
    const select = document.querySelector('[data-od-id="paragraph-style-bold"]') as HTMLSelectElement;
    await waitFor(() => expect(select.value).toBe('on')); // 草稿覆盖先显示出来
    fireEvent.change(select, { target: { value: '' } }); // 跟随原文
    await act(async () => {
      vi.advanceTimersByTime(SAVE_DEBOUNCE_MS + 50);
    });
    await waitFor(() => expect(patchBody(fetchMock).paragraphs['P05-002']).toBeTruthy());
    const entry = patchBody(fetchMock).paragraphs['P05-002'] as { layout: Record<string, unknown> };
    expect(entry.layout).toEqual({ font_scale: 1.05, box: [70, 630, 522, 782] });
    expect('bold' in entry.layout).toBe(false);
  });

  it('样式布尔计为排版覆盖：只有 bold 覆盖的草稿也出现「恢复基线」', async () => {
    const draftStyleOnly = {
      revision: 2,
      updated_at: null,
      paragraphs: { 'P05-002': { layout: { bold: true }, updated_at: null } },
    };
    mockEditor({ draft: draftStyleOnly });
    renderWithQuery(<ParagraphEditor did={DID} paragraphId={'P05-002'} />);
    expect(await screen.findByRole('button', { name: '恢复基线' })).toBeInTheDocument();
  });
});

describe('ParagraphEditor 冲突与只读', () => {
  it('409 revision_conflict → 提示刷新重试 + 刷新草稿按钮（不假装保存成功）', async () => {
    const fetchMock = mockEditor({
      draft: DRAFT_WITH_OVERRIDE,
      patch: () =>
        jsonResponse(
          {
            error: {
              code: 'revision_conflict',
              message: '草稿已被其它会话改动',
              detail: { current_revision: 9 },
            },
          },
          409,
        ),
    });
    renderWithQuery(<ParagraphEditor did={DID} paragraphId={'P05-002'} />);
    const target = (await screen.findByLabelText('译文')) as HTMLTextAreaElement;
    await waitFor(() => expect(target.value).toBe('草稿译文'));
    fireEvent.change(target, { target: { value: '另一个会话的译文' } });
    fireEvent.blur(target);
    const conflict = await screen.findByText('草稿已被其它会话改动');
    expect(conflict).toBeInTheDocument();
    expect(document.querySelector('[data-od-id="paragraph-conflict"]')).not.toBeNull();
    expect(await screen.findByRole('button', { name: '刷新草稿' })).toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'PATCH')).toBe(true);
  });

  it('409 document_busy → 「编译中，稍后再试」', async () => {
    mockEditor({
      patch: () =>
        jsonResponse(
          { error: { code: 'document_busy', message: '有活动 job', detail: { job_id: 'j_1' } } },
          409,
        ),
    });
    renderWithQuery(<ParagraphEditor did={DID} paragraphId={'P05-002'} />);
    const target = (await screen.findByLabelText('译文')) as HTMLTextAreaElement;
    fireEvent.change(target, { target: { value: 'X' } });
    fireEvent.blur(target);
    expect(await screen.findByText('编译中，稍后再试')).toBeInTheDocument();
    expect(document.querySelector('[data-od-id="paragraph-busy"]')).not.toBeNull();
  });

  it('422 draft_invalid → 错误条给服务端逐条 errors', async () => {
    mockEditor({
      patch: () =>
        jsonResponse(
          {
            error: {
              code: 'draft_invalid',
              message: '草稿字段不合法',
              detail: { errors: ['paragraphs.P05-002.layout.font_scale: 超出范围 [0.2, 5.0]'] },
            },
          },
          422,
        ),
    });
    renderWithQuery(<ParagraphEditor did={DID} paragraphId={'P05-002'} />);
    const target = (await screen.findByLabelText('译文')) as HTMLTextAreaElement;
    fireEvent.change(target, { target: { value: 'X' } });
    fireEvent.blur(target);
    const invalid = await screen.findByText('草稿字段不合法');
    expect(invalid).toBeInTheDocument();
    expect(document.querySelector('[data-od-id="paragraph-invalid"]')?.textContent).toContain(
      '超出范围',
    );
  });

  it('编译中（disabled）：textarea 只读、输入禁用、提示只读原因', async () => {
    const fetchMock = mockEditor({ draft: DRAFT_WITH_OVERRIDE });
    renderWithQuery(
      <ParagraphEditor
        did={DID}
        paragraphId={'P05-002'}
        disabled
        disabledReason="编译中…：编译结束后可继续编辑"
      />,
    );
    const target = (await screen.findByLabelText('译文')) as HTMLTextAreaElement;
    expect(target).toHaveAttribute('readonly');
    expect((screen.getByLabelText(/字号缩放/) as HTMLInputElement).disabled).toBe(true);
    expect(document.querySelector('[data-od-id="paragraph-editor-locked"]')?.textContent).toBe('只读');
    expect(screen.getByRole('button', { name: '保存' })).toBeDisabled();
    expect(screen.getByRole('button', { name: '恢复基线' })).toBeDisabled();

    fireEvent.change(target, { target: { value: '编译中乱改' } });
    await act(async () => {
      vi.advanceTimersByTime(SAVE_DEBOUNCE_MS * 3);
    });
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === 'PATCH')).toBe(false);
  });

  it('没有段落产物（404 paragraphs_unavailable）：面板仍可用，译文空态是基线缺省', async () => {
    mockEditor({ paragraphsStatus: 404 });
    renderWithQuery(<ParagraphEditor did={DID} paragraphId={'P05-002'} />);
    const target = (await screen.findByLabelText('译文')) as HTMLTextAreaElement;
    expect(target.value).toBe('');
    expect(await screen.findByText(/该 id 不在段落产物里/)).toBeInTheDocument();
  });
});
