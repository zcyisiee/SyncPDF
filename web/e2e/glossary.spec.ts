/**
 * W13 真用例：全局词表（`#/glossary`）—— CRUD / CSV 导入导出 / 开关联动 / **注入证据**。
 *
 * **不真跑翻译**（不调任何模型）：起跑用的 translator 是离线 stub
 * （`e2e/fixtures/glossary-translator.sh`：读干提示词，把原文当译文吐回去）。注入的验收面是
 * **服务端落盘的产品**：`bdt run --from translate` 会把渲染好的提示词写到
 * `agent/prompt.md`（`GET /documents/{did}/artifacts/agent/prompt.md` 可取回），
 * e2e 断言它含「术语约束（词表）」段与词条 —— 这是「词表真的进了翻译提示词」的直接证据。
 * 同时对比 `use_glossary: false` 的 job：同一文档第二次跑，prompt.md 里**没有**该段。
 *
 * 这个 stub 跑不完整条 pipeline（fixture workdir 没有 `agent/state.pkl`，apply 阶段会如实
 * 失败）—— 那不影响断言：`prompt.md` 在调 translator **之前** 就写好了，job 的失败原因与
 * 词表无关。
 *
 * fixture 与副作用（都在仓库 `tmp/` 下，不入库）：
 * - `tmp/w13-glossary-<时间戳>/`：本用例自建的最小 workdir（`agent/document.md` +
 *   `run_state.json` + `source.pdf`）；
 * - `tmp/.bdt-serve/glossary.csv`：进用例前备份，`afterAll` 还原（**全局词表是共享状态**，
 *   不能给后续 spec 或手工冒烟留一张测试词表）；
 * - `tmp/.bdt-serve/profiles.json`：加一条 `w13-glossary-stub`，`afterAll` 还原；
 * - 用例结束取消活动 job 并停掉 debug 查看器。
 */
import { expect, test } from '@playwright/test';
import type { APIRequestContext, Locator, Page } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import {
  copyFileSync,
  existsSync,
  mkdirSync,
  readFileSync,
  rmSync,
  writeFileSync,
} from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const WEB_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const REPO_ROOT = resolve(WEB_ROOT, '..');
const SERVE_ROOT = join(REPO_ROOT, 'tmp');
const STATE_DIR = join(SERVE_ROOT, '.bdt-serve');
const GLOSSARY_FILE = join(STATE_DIR, 'glossary.csv');
const GLOSSARY_BACKUP = join(STATE_DIR, 'glossary.csv.w13-e2e-backup');
const PROFILES_FILE = join(STATE_DIR, 'profiles.json');
const PROFILES_BACKUP = join(STATE_DIR, 'profiles.json.w13-e2e-backup');
const SHOT_DIR = join(WEB_ROOT, 'tmp-smoke');
const PDF_FIXTURE = join(WEB_ROOT, 'e2e', 'fixtures', 'sample.pdf');
const STUB_TRANSLATOR = join(WEB_ROOT, 'e2e', 'fixtures', 'glossary-translator.sh');
const API = '/api/v1';

/** 每次跑一个新 did：job 历史按 did 存在 `.bdt-serve/jobs/`，固定 did 会看到上一轮的记录。 */
const STAMP = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 14);
const DID = `w13-glossary-${STAMP}`;
const WORKDIR = join(SERVE_ROOT, DID);
const PROFILE = 'w13-glossary-stub';

interface GlossaryBody {
  entries: { source: string; target: string; note: string | null }[];
  count: number;
}

interface JobBody {
  job_id: string;
  status: string;
  use_glossary: boolean;
  error_code: string | null;
}

/** 起手词表：两条术语（顺序故意颠倒，顺带验证服务端会按 source 排序）。 */
const SEED = [
  { source: 'self-attention', target: '自注意力', note: '带连字符' },
  { source: 'attention', target: '注意力', note: null },
];

async function putGlossary(
  request: APIRequestContext,
  entries: { source: string; target: string; note?: string | null }[],
): Promise<GlossaryBody> {
  const response = await request.put(`${API}/glossary`, { data: { entries } });
  expect(response.ok(), `PUT /glossary → ${response.status()}`).toBe(true);
  return (await response.json()) as GlossaryBody;
}

async function getGlossary(request: APIRequestContext): Promise<GlossaryBody> {
  const response = await request.get(`${API}/glossary`);
  expect(response.ok()).toBe(true);
  return (await response.json()) as GlossaryBody;
}

async function waitJob(request: APIRequestContext, jobId: string, timeoutMs = 60_000) {
  const deadline = Date.now() + timeoutMs;
  let job: JobBody | undefined;
  while (Date.now() < deadline) {
    const response = await request.get(`${API}/jobs/${jobId}`);
    expect(response.ok()).toBe(true);
    job = (await response.json()) as JobBody;
    if (['succeeded', 'failed', 'canceled', 'interrupted'].includes(job.status)) return job;
    await new Promise((resolve) => setTimeout(resolve, 500));
  }
  throw new Error(`job ${jobId} 没在 ${timeoutMs}ms 内落定：${JSON.stringify(job)}`);
}

/** 取 workdir 产物（`agent/prompt.md` 是 translate 阶段落盘的提示词）。 */
async function promptText(request: APIRequestContext): Promise<string> {
  const response = await request.get(`${API}/documents/${DID}/artifacts/agent/prompt.md`);
  expect(response.status(), 'prompt.md 应该已经落盘').toBe(200);
  return await response.text();
}

/** 设置屏里「使用全局词表」偏好旁的条数/空表提示（工作台不再重复展示词表状态）。 */
function glossaryCount(page: Page): Locator {
  return page.locator('[data-od-id="settings-glossary-count"]');
}

/** 一组输入框当前的值（`toHaveValue` 只看单个，这里要看整列）。 */
async function cellValues(locator: Locator): Promise<string[]> {
  return await locator.evaluateAll((elements) =>
    elements.map((element) => (element as HTMLInputElement).value),
  );
}

/** 写一个临时 CSV（导入用）并返回路径。 */
function writeCsv(name: string, text: string): string {
  const path = join(WORKDIR, name);
  mkdirSync(WORKDIR, { recursive: true });
  writeFileSync(path, text, 'utf-8');
  return path;
}

test.beforeAll(async ({ request }) => {
  mkdirSync(SHOT_DIR, { recursive: true });
  mkdirSync(STATE_DIR, { recursive: true });
  expect(existsSync(PDF_FIXTURE), 'fixture PDF 缺失：web/e2e/fixtures/sample.pdf').toBe(true);

  // 共享状态先备份：词表 + profile 表都用完还原
  if (existsSync(GLOSSARY_FILE)) copyFileSync(GLOSSARY_FILE, GLOSSARY_BACKUP);
  if (existsSync(PROFILES_FILE)) copyFileSync(PROFILES_FILE, PROFILES_BACKUP);

  // 词表起手是一张已知的两条表（用例里再改）
  await putGlossary(request, SEED);

  // 最小 workdir：translate 前置只要 agent/document.md + run_state.json
  mkdirSync(join(WORKDIR, 'agent'), { recursive: true });
  writeFileSync(
    join(WORKDIR, 'agent', 'document.md'),
    '<!-- babeldoc-markdown v1 -->\n\n<!-- id=P01-001 label=text -->\nAttention is all you need.\n',
    'utf-8',
  );
  writeFileSync(join(WORKDIR, 'agent', 'run_state.json'), '{}\n', 'utf-8');
  copyFileSync(PDF_FIXTURE, join(WORKDIR, 'source.pdf'));
  execFileSync('chmod', ['755', STUB_TRANSLATOR]);

  // profile 写进 profiles.json（HTTP 的 PUT /profiles 只接受白名单内的脚本引用；
  // e2e 直接写文件，与 upload.spec 的 sleep-t 同一口径）
  const profiles = existsSync(PROFILES_FILE)
    ? (JSON.parse(readFileSync(PROFILES_FILE, 'utf-8')) as Record<string, unknown>)
    : {};
  profiles[PROFILE] = {
    translator: `${STUB_TRANSLATOR} ${join(WORKDIR, 'agent', 'document.md')}`,
  };
  writeFileSync(PROFILES_FILE, JSON.stringify(profiles, null, 2), 'utf-8');
});

test.afterAll(async ({ request }) => {
  // 活动 job 全取消（不留 running 子进程）
  const jobs = await request.get(`${API}/documents/${DID}/jobs`);
  if (jobs.ok()) {
    for (const job of (await jobs.json()) as JobBody[]) {
      if (job.status === 'queued' || job.status === 'running') {
        await request.post(`${API}/jobs/${job.job_id}/cancel`);
      }
    }
  }
  stopViewer(WORKDIR);

  // 还原共享状态
  if (existsSync(GLOSSARY_BACKUP)) {
    copyFileSync(GLOSSARY_BACKUP, GLOSSARY_FILE);
    rmSync(GLOSSARY_BACKUP);
  } else if (existsSync(GLOSSARY_FILE)) {
    rmSync(GLOSSARY_FILE);
  }
  if (existsSync(PROFILES_BACKUP)) {
    copyFileSync(PROFILES_BACKUP, PROFILES_FILE);
    rmSync(PROFILES_BACKUP);
  } else if (existsSync(PROFILES_FILE)) {
    rmSync(PROFILES_FILE);
  }
});

/** 停掉该 workdir 的 debug 查看器（否则它会挂很久才自退）。 */
function stopViewer(workdir: string) {
  if (!existsSync(join(workdir, 'debug', 'viewer.json'))) return;
  try {
    execFileSync(
      join(REPO_ROOT, '.venv', 'bin', 'python'),
      ['-m', 'babeldoc_tools', 'debug', '--workdir', workdir, '--stop'],
      { cwd: REPO_ROOT, env: { ...process.env, PYTHONPATH: REPO_ROOT }, timeout: 20_000 },
    );
  } catch {
    // 查看器已经不在了：收尾失败不影响断言结果
  }
}

test('词表视图：读回服务端的表 + 常驻「不回溯」提示 + 图标栏条数徽标', async ({
  page,
  request,
}) => {
  await putGlossary(request, SEED);
  await page.goto('/#/glossary');

  const table = page.locator('[data-od-id="glossary-table"]');
  await expect(table).toBeVisible();
  // 服务端按 source 排序：attention 在 self-attention 前面
  await expect
    .poll(async () => cellValues(table.locator('[data-od-id="glossary-cell-source"]')))
    .toEqual(['attention', 'self-attention']);
  await expect(page.locator('[data-od-id="glossary-no-retro"]')).toContainText(
    '对已翻译段落无追溯效果',
  );
  // 图标栏徽标 = 条数（2）
  await expect(page.locator('[data-od-id="rail-glossary-badge"]')).toHaveText('2');
});

test('词表视图：改 + 加行 → 保存（整表 PUT）→ GET 断言；删行同理', async ({
  page,
  request,
}) => {
  await putGlossary(request, SEED);
  await page.goto('/#/glossary');
  await expect(page.locator('[data-od-id="glossary-table"]')).toBeVisible();

  // 改一条：attention 的译名 + 备注
  const firstTarget = page.locator('[data-od-id="glossary-cell-target"]').first();
  await firstTarget.fill('注意力（改）');
  await page.locator('[data-od-id="glossary-cell-note"]').first().fill('人工核对过');
  // 加一行
  await page.click('[data-od-id="glossary-add-row"]');
  const sources = page.locator('[data-od-id="glossary-cell-source"]');
  await sources.nth(2).fill('beam search');
  await page.locator('[data-od-id="glossary-cell-target"]').nth(2).fill('束搜索');

  await expect(page.locator('[data-od-id="glossary-dirty"]')).toBeVisible();
  await page.click('[data-od-id="glossary-save"]');
  // 保存成功 = 重新落到服务端那一份（unsaved 标记消失，保存按钮回禁用）
  await expect(page.locator('[data-od-id="glossary-dirty"]')).toHaveCount(0);
  await expect(page.locator('[data-od-id="glossary-save"]')).toBeDisabled();

  const stored = await getGlossary(request);
  expect(stored.count).toBe(3);
  expect(stored.entries).toEqual([
    { source: 'attention', target: '注意力（改）', note: '人工核对过' },
    { source: 'beam search', target: '束搜索', note: null },
    { source: 'self-attention', target: '自注意力', note: '带连字符' },
  ]);

  // 删行 + 保存 → 服务端只剩两条
  await page.locator('[data-od-id="glossary-delete-row"]').nth(1).click();
  await page.click('[data-od-id="glossary-save"]');
  await expect(page.locator('[data-od-id="glossary-save"]')).toBeDisabled();
  const afterDelete = await getGlossary(request);
  expect(afterDelete.entries.map((entry) => entry.source)).toEqual([
    'attention',
    'self-attention',
  ]);

  await page.screenshot({ path: join(SHOT_DIR, 'w13-glossary-editor.png'), fullPage: true });
});

test('词表视图：CSV 导出（本地生成）与导入（浏览器解析）', async ({ page, request }) => {
  await putGlossary(request, SEED);
  await page.goto('/#/glossary');
  await expect(page.locator('[data-od-id="glossary-table"]')).toBeVisible();

  // 导出：浏览器落盘一个 glossary.csv，内容就是屏幕上的表
  const download = await Promise.all([
    page.waitForEvent('download'),
    page.click('[data-od-id="glossary-export"]'),
  ]).then(([event]) => event);
  expect(download.suggestedFilename()).toBe('glossary.csv');
  const exported = readFileSync((await download.path()) as string, 'utf-8');
  expect(exported.split('\n')[0]).toBe('source,target,note');
  expect(exported).toContain('attention,注意力,');

  // 导入：选一个文件 → 表格换成文件里的内容（还没有保存）
  const importPath = writeCsv(
    'import.csv',
    'source,target,note\nthroughput,吞吐量,单位时间\n',
  );
  await page.setInputFiles('[data-od-id="glossary-import-input"]', importPath);

  const table = page.locator('[data-od-id="glossary-table"]');
  await expect
    .poll(async () => cellValues(table.locator('[data-od-id="glossary-cell-source"]')))
    .toEqual(['throughput']);
  // 导入只改本地草稿：服务端还是原来那张表
  expect((await getGlossary(request)).entries.map((entry) => entry.source)).toEqual([
    'attention',
    'self-attention',
  ]);

  await page.click('[data-od-id="glossary-save"]');
  await expect(page.locator('[data-od-id="glossary-save"]')).toBeDisabled();
  await expect
    .poll(async () => (await getGlossary(request)).entries.map((entry) => entry.source))
    .toEqual(['throughput']);
});

test('坏 CSV 导入 → 明确报错，不动现有表', async ({ page, request }) => {
  await putGlossary(request, SEED);
  await page.goto('/#/glossary');
  await expect(page.locator('[data-od-id="glossary-table"]')).toBeVisible();

  const badPath = writeCsv('bad.csv', 'term,translation\nfoo,bar\n');
  await page.setInputFiles('[data-od-id="glossary-import-input"]', badPath);

  await expect(page.locator('[data-od-id="glossary-csv-error"]')).toContainText(
    'source 与 target',
  );
  await expect(page.locator('[data-od-id="glossary-cell-target"]').first()).toHaveValue('注意力');
});

test('设置屏：词表非空显示条数；清空后 → 提示「词表为空」（服务端空表不注入）', async ({
  page,
  request,
}) => {
  await putGlossary(request, SEED);
  await page.goto('/#/settings');
  await expect(glossaryCount(page)).toHaveText('1 条');

  // 清空词表（DELETE）→ 刷新后设置屏如实显示「词表为空」
  const cleared = await request.delete(`${API}/glossary`);
  expect(cleared.ok()).toBe(true);
  await page.reload();
  await expect(glossaryCount(page)).toHaveText('（词表为空）');

  await page.screenshot({ path: join(SHOT_DIR, 'w13-settings-glossary.png'), fullPage: true });
});

test('注入证据：run job 的 prompt.md 含术语约束段；use_glossary=false 时不含', async ({
  request,
}) => {
  // 词表起手两条术语
  await putGlossary(request, SEED);

  // 1) use_glossary 缺省 true → 服务端把词表注入提示词
  const withGlossary = await request.post(`${API}/documents/${DID}/jobs`, {
    data: { action: 'run', from: 'translate', profile: PROFILE },
  });
  expect(withGlossary.status(), await withGlossary.text()).toBe(202);
  const firstId = ((await withGlossary.json()) as { job_id: string }).job_id;
  const firstJob = await waitJob(request, firstId);
  // job 记录如实回显"这次带词表"（客户端只给了布尔）
  expect(firstJob.use_glossary).toBe(true);

  const injected = await promptText(request);
  expect(injected).toContain('## 术语约束（词表）');
  expect(injected).toContain('- attention → 注意力');
  expect(injected).toContain('- self-attention → 自注意力（带连字符）');
  expect(injected).toContain('Attention is all you need.'); // 文档本体也在

  // 2) use_glossary=false → 同一条链路，提示词里没有任何词表痕迹
  const withoutGlossary = await request.post(`${API}/documents/${DID}/jobs`, {
    data: { action: 'run', from: 'translate', profile: PROFILE, use_glossary: false },
  });
  expect(withoutGlossary.status()).toBe(202);
  const secondId = ((await withoutGlossary.json()) as { job_id: string }).job_id;
  const secondJob = await waitJob(request, secondId);
  expect(secondJob.use_glossary).toBe(false);

  const plain = await promptText(request);
  expect(plain).not.toContain('术语约束');
  expect(plain).not.toContain('self-attention');
  expect(plain).toContain('Attention is all you need.');
});
