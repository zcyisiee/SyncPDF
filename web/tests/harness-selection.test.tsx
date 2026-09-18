import { fireEvent, screen, waitFor, within } from '@testing-library/react';
import { beforeEach, expect, it } from 'vitest';
import { SettingsScreen } from '../src/screens/SettingsScreen';
import { StartJobCard } from '../src/components/jobs/StartJobCard';
import { CandidatePanel } from '../src/components/edit/CandidatePanel';
import { HARNESS_DEFAULT_KEY } from '../src/lib/harnesses';
import { jsonResponse, mockApiFetch, renderWithQuery } from './helpers';

const builtins = [
  { id: 'pi-deepseek-flash', label: 'Pi · DeepSeek Flash', model: 'deepseek/deepseek-v4-flash', thinking_levels: ['off', 'low', 'high', 'max'], default_thinking: 'low' },
  { id: 'pi-deepseek-v4-pro', label: 'Pi · DeepSeek V4 Pro', model: 'deepseek/deepseek-v4-pro', thinking_levels: ['off', 'high', 'max'], default_thinking: 'high' },
  { id: 'agy-gemini-3-8-flash', label: 'agy · Gemini 3.8 Flash', model: 'gemini-3.8-flash', thinking_levels: ['low', 'medium', 'high'], default_thinking: 'low' },
  { id: 'agy-gemini-3-1-pro', label: 'agy · Gemini 3.1 Pro', model: 'gemini-3.1-pro', thinking_levels: ['low', 'high'], default_thinking: 'low' },
].map(item => ({ ...item, builtin: true, has_translator: true, has_reviewer: false }));

function mockRoutes() {
  return mockApiFetch({
    '/api/v1/profiles': () => jsonResponse([...builtins, { id: 'legacy', label: 'Old API config', has_translator: true, has_reviewer: false }]),
    '/api/v1/documents/doc/artifacts': () => jsonResponse([{ name: 'source.pdf', kind: 'source' }]),
    '/api/v1/glossary': () => jsonResponse({ entries: [], count: 0 }),
    '/api/v1/documents/doc/paragraphs/P01/candidates': () => jsonResponse({ items: [] }),
    'POST /api/v1/documents/doc/jobs': () => jsonResponse({ job_id: 'j_mock', status: 'queued' }, 202),
    'POST /api/v1/documents/doc/paragraphs/P01/retranslate': () => jsonResponse({ candidate_id: 'c_mock', job_id: 'j_mock' }, 202),
  });
}

beforeEach(() => localStorage.clear());

it('settings exposes only four built-ins, supported thinking and a saved default without generation', async () => {
  const fetch = mockRoutes();
  renderWithQuery(<SettingsScreen />);
  await screen.findByText('deepseek/deepseek-v4-flash');
  expect(within(screen.getByLabelText('翻译模型')).getAllByRole('option')).toHaveLength(4);
  expect(screen.queryByText('Old API config')).toBeNull();
  expect(screen.queryByLabelText('API Key')).toBeNull();
  expect(screen.queryByLabelText('Base URL')).toBeNull();
  expect(screen.getByLabelText('思考强度')).toHaveValue('low');
  fireEvent.change(screen.getByLabelText('翻译模型'), { target: { value: 'pi-deepseek-v4-pro' } });
  expect(screen.getByLabelText('思考强度')).toHaveValue('high');
  expect(within(screen.getByLabelText('思考强度')).getAllByRole('option').map(option => option.textContent)).toEqual(['off', 'high', 'max']);
  fireEvent.click(screen.getByRole('button', { name: '设为默认' }));
  expect(JSON.parse(localStorage.getItem(HARNESS_DEFAULT_KEY)!)).toEqual({ profile: 'pi-deepseek-v4-pro', thinking: 'high' });
  expect(fetch.mock.calls.every(([, init]) => !init?.method || init.method === 'GET')).toBe(true);
});

it('start translation submits built-in and actual thinking, with optional review off by default', async () => {
  const fetch = mockRoutes();
  renderWithQuery(<StartJobCard did="doc" document={undefined} />);
  await screen.findByText('deepseek/deepseek-v4-flash');
  expect(screen.getByLabelText('AI 审校')).not.toBeChecked();
  fireEvent.change(screen.getByLabelText('翻译配置'), { target: { value: 'agy-gemini-3-8-flash' } });
  fireEvent.change(screen.getByLabelText('思考强度'), { target: { value: 'medium' } });
  fireEvent.click(screen.getByRole('button', { name: '开始翻译' }));
  await waitFor(() => expect(fetch.mock.calls.some(([, init]) => init?.method === 'POST')).toBe(true));
  const body = JSON.parse(String(fetch.mock.calls.find(([, init]) => init?.method === 'POST')![1]!.body));
  expect(body).toMatchObject({ profile: 'agy-gemini-3-8-flash', thinking: 'medium' });
  expect(body).not.toHaveProperty('reviewer_profile');
  expect(body).not.toHaveProperty('command');
});

it('review uses selected harness and candidate submission resets incompatible effort', async () => {
  localStorage.setItem(HARNESS_DEFAULT_KEY, JSON.stringify({ profile: 'pi-deepseek-flash', thinking: 'max' }));
  const fetch = mockRoutes();
  const start = renderWithQuery(<StartJobCard did="doc" document={undefined} />);
  await screen.findByText('deepseek/deepseek-v4-flash');
  fireEvent.click(screen.getByLabelText('AI 审校'));
  fireEvent.click(screen.getByRole('button', { name: '开始翻译' }));
  await waitFor(() => expect(fetch.mock.calls.some(([, init]) => init?.method === 'POST')).toBe(true));
  expect(JSON.parse(String(fetch.mock.calls.find(([, init]) => init?.method === 'POST')![1]!.body))).toMatchObject({
    profile: 'pi-deepseek-flash', thinking: 'max', reviewer_profile: 'pi-deepseek-flash',
  });
  start.unmount();
  fetch.mockClear();
  renderWithQuery(<CandidatePanel did="doc" pid="P01" currentTarget="旧译" baselineTarget="旧译" />);
  await screen.findByText('deepseek/deepseek-v4-flash');
  expect(screen.getByLabelText('思考强度')).toHaveValue('max');
  fireEvent.change(screen.getByLabelText('重译模型'), { target: { value: 'agy-gemini-3-1-pro' } });
  expect(screen.getByLabelText('思考强度')).toHaveValue('low');
  expect(within(screen.getByLabelText('思考强度')).getAllByRole('option').map(option => option.textContent)).toEqual(['low', 'high']);
  fireEvent.click(screen.getByRole('button', { name: 'AI 重译' }));
  await waitFor(() => expect(fetch.mock.calls.some(([, init]) => init?.method === 'POST')).toBe(true));
  expect(JSON.parse(String(fetch.mock.calls.find(([, init]) => init?.method === 'POST')![1]!.body))).toEqual({
    profile: 'agy-gemini-3-1-pro', thinking: 'low',
  });
});
