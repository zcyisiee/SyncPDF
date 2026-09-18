/**
 * W11 候选重译面板（api.md §3.6）：按钮态、三段对比、采用联动草稿、拒绝折叠、活动期禁用。
 *
 * 口径与后端契约一致：
 * - 生成只传 `profile` **id**（请求体里没有 translator/feedback 字段）；
 * - 采用后**服务端**返回新草稿 → 译文框变候选文本 + 「草稿已修改」（草稿缓存更新驱动，不是本地替换正文）；
 * - 拒绝不改草稿 → 活动 job 期间也允许；生成与采用在活动期禁用；
 * - 生成中的候选（`candidate_target=null`）显示"生成中"，采用按钮不可点。
 */
import { screen, waitFor } from '@testing-library/react';
import { fireEvent } from '@testing-library/react';
import { beforeEach, describe, expect, it } from 'vitest';

import { CandidatePanel } from '../src/components/edit/CandidatePanel';
import { ParagraphEditor } from '../src/components/edit/ParagraphEditor';
import { uiStore } from '../src/stores/ui';
import { jsonResponse, mockApiFetch, renderWithQuery, resetUiStore } from './helpers';

const DID = 'ccs3764-dyn';
const PID = 'P05-002';
const PARAGRAPHS_PATH = `/api/v1/documents/${DID}/paragraphs`;
const DRAFT_PATH = `/api/v1/documents/${DID}/draft`;
const CANDIDATES_PATH = `${PARAGRAPHS_PATH}/${PID}/candidates`;
const RETRANSLATE_PATH = `${PARAGRAPHS_PATH}/${PID}/retranslate`;
const ADOPT_PATH = (cid: string) => `${CANDIDATES_PATH}/${cid}/adopt`;
const REJECT_PATH = (cid: string) => `${CANDIDATES_PATH}/${cid}/reject`;

const PROFILES = [
  { id: 'pi-deepseek-flash', label: 'Pi · DeepSeek Flash', builtin: true, thinking_levels: ['off', 'low', 'high', 'max'], default_thinking: 'low', has_translator: true, has_reviewer: false },
  { id: 'review-only', label: 'Review Only', has_translator: false, has_reviewer: true },
];

const PARAGRAPH = {
  id: PID,
  page: 5,
  layout_label: 'text',
  source: 'Source text of P05-002.',
  target: '基线译文',
  geometry: null,
  layout_status: 'ok',
};

const DRAFT_EMPTY = { revision: 0, updated_at: null, paragraphs: {} };

function makeCandidate(overrides: Record<string, unknown> = {}) {
  return {
    id: 'c_0001',
    pid: PID,
    source: 'Source text of P05-002.',
    baseline_target: '基线译文',
    candidate_target: '候选译文甲',
    status: 'pending',
    model_label: 'echo-t',
    job_id: 'j_01M2RDB312K20Q280DHCTX7N19',
    created_at: '2026-09-17T15:54:45.123Z',
    adopted_at: null,
    ...overrides,
  };
}

function mockPanel(options: {
  candidates?: Record<string, unknown>[];
  profiles?: typeof PROFILES;
  retranslate?: () => Response;
  adopt?: () => Response;
  reject?: () => Response;
  /** `GET /draft` 的响应（允许按内部状态变化：采用后服务端就是新草稿）。 */
  draft?: () => Response;
} = {}) {
  return mockApiFetch({
    '/api/v1/profiles': () => jsonResponse(options.profiles ?? PROFILES),
    [PARAGRAPHS_PATH]: () => jsonResponse([PARAGRAPH]),
    [DRAFT_PATH]: options.draft ?? (() => jsonResponse(DRAFT_EMPTY)),
    [CANDIDATES_PATH]: () => jsonResponse({ pid: PID, items: options.candidates ?? [] }),
    [`POST ${RETRANSLATE_PATH}`]:
      options.retranslate ??
      (() => jsonResponse({ candidate_id: 'c_0002', job_id: 'j_new', status: 'queued', action: 'retranslate' }, 202)),
    [`POST ${ADOPT_PATH('c_0001')}`]:
      options.adopt ??
      (() =>
        jsonResponse({
          revision: 1,
          updated_at: '2026-09-17T15:55:00.000Z',
          paragraphs: { [PID]: { target: '候选译文甲', layout: null, updated_at: null } },
        })),
    [`POST ${REJECT_PATH('c_0001')}`]:
      options.reject ?? (() => jsonResponse(makeCandidate({ status: 'rejected' }))),
  });
}

function bodyOf(fetchMock: ReturnType<typeof mockApiFetch>, path: string) {
  const call = fetchMock.mock.calls.find(([input]) => String(input) === path);
  expect(call, `没有请求 ${path}`).toBeTruthy();
  return JSON.parse(String(call?.[1]?.body)) as Record<string, unknown>;
}

/** 等按钮真的可点再点：profile 列表是异步拉的，早产的一次 click 会落在 disabled 按钮上。 */
async function clickWhenEnabled(label: string) {
  const button = await screen.findByText(label);
  await waitFor(() => expect(button).toBeEnabled());
  fireEvent.click(button);
}

beforeEach(() => {
  resetUiStore();
  uiStore.setState({ retranslateProfile: null });
});

describe('CandidatePanel 按钮态', () => {
  it('只列有 translator 的 profile，提交时只带 profile id', async () => {
    const fetchMock = mockPanel();
    renderWithQuery(
      <CandidatePanel did={DID} pid={PID} currentTarget="基线译文" baselineTarget="基线译文" />,
    );

    const select = await screen.findByLabelText('重译模型');
    await waitFor(() => expect(select).toHaveValue('pi-deepseek-flash'));
    // 没配 translator 的 profile 不出现在候选生成的下拉里
    expect(screen.queryByText('Review Only')).toBeNull();

    await clickWhenEnabled('AI 重译');
    await waitFor(() => expect(bodyOf(fetchMock, RETRANSLATE_PATH)).toEqual({ profile: 'pi-deepseek-flash', thinking: 'low' }));
  });

  it('活动 job 期间：生成与采用禁用，拒绝仍可点（拒绝不写草稿）', async () => {
    mockPanel({ candidates: [makeCandidate()] });
    renderWithQuery(
      <CandidatePanel
        did={DID}
        pid={PID}
        currentTarget="基线译文"
        baselineTarget="基线译文"
        disabled
        disabledReason="编译中"
      />,
    );

    await waitFor(() => expect(screen.getByText('采用')).toBeDisabled());
    expect(screen.getByText('AI 重译')).toBeDisabled();
    expect(screen.getByText('拒绝')).toBeEnabled();
    // 只读态也把候选显示出来（候选没采用前不影响正文，看一眼是安全的）
    expect(screen.getByText('候选译文甲')).toBeInTheDocument();
  });

  it('没有可用翻译配置时禁用重译并提供设置入口', async () => {
    mockPanel({ profiles: [PROFILES[1]] });
    renderWithQuery(
      <CandidatePanel did={DID} pid={PID} currentTarget="基线译文" baselineTarget="基线译文" />,
    );
    expect(await screen.findByText(/未能读取内置模型/)).toBeInTheDocument();
    expect(screen.getByRole('link', { name: '管理翻译配置' })).toHaveAttribute('href', '#/settings');
    expect(screen.getByText('AI 重译')).toBeDisabled();
  });
});

describe('CandidatePanel 候选渲染', () => {
  it('三段对比 + 生成中的候选显示占位（采用不可点）', async () => {
    mockPanel({
      candidates: [makeCandidate(), makeCandidate({ id: 'c_0002', candidate_target: null })],
    });
    renderWithQuery(
      <CandidatePanel
        did={DID}
        pid={PID}
        currentTarget="草稿译文"
        baselineTarget="基线译文"
      />,
    );

    const generating = await screen.findByText('生成中');
    const card = generating.closest('[data-od-id="candidate-card"]');
    expect(card?.getAttribute('data-candidate-id')).toBe('c_0002');
    expect(card?.getAttribute('data-ready')).toBe('false');
    expect(screen.getByText('候选译文生成中（job 结束后出现在这里）')).toBeInTheDocument();

    // 三段对比的标签与内容都在（当前译文显示草稿版并标注）
    expect(screen.getByText('候选译文甲')).toBeInTheDocument();
    expect(screen.getAllByText('当前译文 （草稿版）')).toHaveLength(2);
    expect(screen.getAllByText('Source text of P05-002.')).toHaveLength(2);
  });

  it('已决定的候选折叠起来（默认收起，点开后能看到）', async () => {
    mockPanel({
      candidates: [
        makeCandidate(),
        makeCandidate({ id: 'c_0000', candidate_target: '被拒的译文', status: 'rejected' }),
      ],
    });
    renderWithQuery(
      <CandidatePanel did={DID} pid={PID} currentTarget="基线译文" baselineTarget="基线译文" />,
    );

    const details = (await screen.findByText('已决定的候选（1）')).closest('details');
    expect(details).not.toBeNull();
    expect(details?.open).toBe(false);
    fireEvent.click(screen.getByText('已决定的候选（1）'));
    expect(screen.getByText('被拒的译文')).toBeInTheDocument();
    expect(screen.getByText('已拒绝')).toBeInTheDocument();
  });
});

describe('CandidatePanel 采用 / 拒绝', () => {
  it('采用：译文框立刻变候选文本 + 草稿已修改（草稿缓存由响应驱动）', async () => {
    // 服务端返回新草稿后，`GET /draft` 也就是那份（r1 + 候选译文）：mock 跟随同样的状态，
    // 否则这里比的就是"服务端说的"和"mock 编的"两回事。
    const adoptedDraft = {
      revision: 1,
      updated_at: '2026-09-17T15:55:00.000Z',
      paragraphs: { [PID]: { target: '候选译文甲', layout: null, updated_at: null } },
    };
    let adopted = false;
    mockPanel({
      candidates: [makeCandidate()],
      draft: () => jsonResponse(adopted ? adoptedDraft : DRAFT_EMPTY),
      adopt: () => {
        adopted = true;
        return jsonResponse(adoptedDraft);
      },
    });

    // 用整块编辑器验证联动：采用后 textarea 的值与「草稿已修改」都来自服务端返回的新草稿
    renderWithQuery(<ParagraphEditor did={DID} paragraphId={PID} />);

    const textarea = await screen.findByLabelText('译文');
    await waitFor(() => expect(textarea).toHaveValue('基线译文'));
    expect(screen.queryByText('草稿已修改')).toBeNull();

    await clickWhenEnabled('采用');

    await waitFor(() => expect(textarea).toHaveValue('候选译文甲'));
    expect(screen.getByText('草稿已修改')).toBeInTheDocument();
    expect(screen.getByText('草稿 r1')).toBeInTheDocument();
  });

  it('拒绝：只发 reject，不动草稿（不出现「草稿已修改」）', async () => {
    const fetchMock = mockPanel({ candidates: [makeCandidate()] });
    renderWithQuery(<ParagraphEditor did={DID} paragraphId={PID} />);

    const textarea = await screen.findByLabelText('译文');
    await clickWhenEnabled('拒绝');

    await waitFor(() =>
      expect(
        fetchMock.mock.calls.some(([input]) => String(input) === REJECT_PATH('c_0001')),
      ).toBe(true),
    );
    expect(fetchMock.mock.calls.some(([input]) => String(input) === ADOPT_PATH('c_0001'))).toBe(false);
    expect(textarea).toHaveValue('基线译文');
    expect(screen.queryByText('草稿已修改')).toBeNull();
  });

  it('采用失败（409 candidate_decided）→ 显示错误卡，草稿不变', async () => {
    mockPanel({
      candidates: [makeCandidate()],
      adopt: () =>
        jsonResponse(
          { error: { code: 'candidate_decided', message: '候选已被采用', detail: { status: 'adopted' } } },
          409,
        ),
    });
    renderWithQuery(<ParagraphEditor did={DID} paragraphId={PID} />);

    await clickWhenEnabled('采用');
    const error = await screen.findByText('采用候选失败');
    expect(error).toBeInTheDocument();
    expect(screen.getByText(/候选仍是待定/)).toBeInTheDocument();
    expect(screen.getByLabelText('译文')).toHaveValue('基线译文');
  });

  it('生成失败（409 document_busy）→ 错误卡显示服务端文案', async () => {
    mockPanel({
      retranslate: () =>
        jsonResponse(
          { error: { code: 'document_busy', message: '文档有活动 job：编译中', detail: { job_id: 'j_x' } } },
          409,
        ),
    });
    renderWithQuery(
      <CandidatePanel did={DID} pid={PID} currentTarget="基线译文" baselineTarget="基线译文" />,
    );

    await clickWhenEnabled('AI 重译');
    expect(await screen.findByText('提交重译失败')).toBeInTheDocument();
    expect(screen.getByText(/文档有活动 job/)).toBeInTheDocument();
  });
});
