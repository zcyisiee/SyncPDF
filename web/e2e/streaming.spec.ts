/**
 * W14 真用例：SSE 的虚拟 kind `job_update`（job 状态变化 → 前端不用等轮询）。
 *
 * 三条链路（都跑真 `bdt serve --root tmp`，但**不跑真 build**）：
 *
 * 1. **帧契约与时序**：页面里用 fetch + ReadableStream 直接读 `/events/stream` 的原始帧，
 *    断言收到了 `event: job_update` / `id: <job_id>:<n>` / 5 个字段的 data；并量出
 *    「服务端状态已变 → 帧到达」的延迟远小于前端的兜底轮询（5s）。
 * 2. **UI 反应**：工作台自己那一条 SSE 连上（`data-status=open`），job 终态到达后
 *    页面**不重新加载**就发出新的 `GET /artifacts`（终态那一刀的失效链路）。
 * 3. **诚实的进度文案**：运行中的 run job 显示「已译段落 N/M（套版后更新）」——N/M 来自
 *    详情的 `translated_count`/`paragraph_count`，翻译阶段不会跳动（整篇单次子进程调用），
 *    这里断言的是「数字与产物一致 + 文案如实标注」。
 *
 * fixture（都在仓库 `tmp/` 下，不入库）：`tmp/w14-streaming-<时间戳>/` 自建最小 workdir
 * （`agent/document.md` + anchors/translated 产物 + 一个**手工写的** run 归档，让
 * `/events/stream` 立即可用）；profile 写入 `.bdt-serve/profiles.json`（先备份，afterAll 还原）。
 * 取消/收尾与 `upload.spec.ts` 同一套（杀活动 job + 停 debug 查看器）。
 */
import { expect, test } from '@playwright/test';
import type { APIRequestContext, Page } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const WEB_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const REPO_ROOT = resolve(WEB_ROOT, '..');
const SERVE_ROOT = join(REPO_ROOT, 'tmp');
const STATE_DIR = join(SERVE_ROOT, '.bdt-serve');
const PROFILES_FILE = join(STATE_DIR, 'profiles.json');
const PROFILES_BACKUP = join(STATE_DIR, 'profiles.json.w14-e2e-backup');
const SHOT_DIR = join(WEB_ROOT, 'tmp-smoke');
const SLEEP_STUB = join(WEB_ROOT, 'e2e', 'fixtures', 'sleep-translator.sh');
const CANDIDATE_STUB = join(WEB_ROOT, 'e2e', 'fixtures', 'candidate-translator.sh');
const API = '/api/v1';

const STAMP = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 14);
/** 每次跑一个新 did：job 历史按 did 存在 tmp/.bdt-serve/jobs/ 里，固定 did 会看到上一次的记录。 */
const DID = `w14-streaming-${STAMP}`;
const WORKDIR = join(SERVE_ROOT, DID);
/** 手工造的 run 归档 id（形状必须过服务端的 RUN_ID_RE）。 */
const RUN_ID = '20260918T000000Z-00a14e';
const PID = 'P01-001';
const BASELINE = 'W14 e2e 基线译文';
const CANDIDATE = 'W14 e2e 候选译文（stub）';

/** 前端的兜底轮询（`lib/jobs.ts::JOBS_ACTIVE_REFETCH_MS`）：SSE 若没起作用就要等这么久。 */
const FALLBACK_POLL_MS = 5_000;

interface JobRow {
  job_id: string;
  action: string;
  status: string;
  error_code: string | null;
}

async function jobs(request: APIRequestContext): Promise<JobRow[]> {
  const response = await request.get(`${API}/documents/${DID}/jobs`);
  expect(response.ok(), `GET jobs → ${response.status()}`).toBe(true);
  return (await response.json()) as JobRow[];
}

async function cancelActiveJobs(request: APIRequestContext) {
  for (const job of await jobs(request).catch(() => [])) {
    if (job.status === 'queued' || job.status === 'running') {
      await request.post(`${API}/jobs/${job.job_id}/cancel`);
    }
  }
}

/** 等该文档没有活动 job（上一次任务彻底落定，否则下一次提交 409）。 */
async function waitIdle(request: APIRequestContext, timeoutMs = 30_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const active = (await jobs(request)).filter(
      (job) => job.status === 'queued' || job.status === 'running',
    );
    if (active.length === 0) return;
    await new Promise((r) => setTimeout(r, 250));
  }
  throw new Error('文档一直没有空闲（还有活动 job）');
}

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

/**
 * 最小 workdir：`agent/` 四份产物（translate 起跑 + 段落面板 + 候选）+
 * 一个手工写的 run 归档（让 `/events/stream` 不是 404 —— SSE 是 job_update 的唯一载体）。
 */
function writeFixture() {
  mkdirSync(join(WORKDIR, 'agent'), { recursive: true });
  writeFileSync(
    join(WORKDIR, 'agent', 'document.md'),
    `<!--${PID}-->\nW14 e2e source sentence.\n`,
    'utf-8',
  );
  writeFileSync(
    join(WORKDIR, 'agent', 'anchors.json'),
    `${JSON.stringify(
      {
        rows: [
          {
            id: PID,
            page: 0,
            layout_label: 'text',
            canonical: 'W14 e2e source sentence.',
            markdown: 'W14 e2e source sentence.',
            anchors: '[]',
          },
        ],
        skipped: [],
      },
      null,
      2,
    )}\n`,
    'utf-8',
  );
  writeFileSync(
    join(WORKDIR, 'agent', 'translated.md'),
    `<!--MD_HEADER-->\n\n<!-- id=${PID} label=text -->\n${BASELINE}\n`,
    'utf-8',
  );
  // translated.jsonl 是 `translated_count` 的来源（apply 阶段写出）：1 行 → N = 1。
  writeFileSync(
    join(WORKDIR, 'agent', 'translated.jsonl'),
    `${JSON.stringify({ id: PID, target: BASELINE, layout_label: 'text' })}\n`,
    'utf-8',
  );
  // layout_geometry.json 是 `paragraph_count` 的来源（1 个段落）→ M = 1。
  writeFileSync(
    join(WORKDIR, 'agent', 'layout_geometry.json'),
    `${JSON.stringify(
      {
        pages: 1,
        page_info: [{ page: 1, cropbox: [0, 0, 300, 120] }],
        paragraphs: [{ id: PID, page: 1, layout_label: 'text', layout_box: [20, 40, 280, 110] }],
      },
      null,
      2,
    )}\n`,
    'utf-8',
  );
  // run 归档：3 条真形状的事件，末条是 report 的 stage_finished（run 已收尾）。
  const runDir = join(WORKDIR, 'debug', 'runs', RUN_ID);
  mkdirSync(runDir, { recursive: true });
  const events = [
    { seq: 1, at: '2026-09-18T00:00:00+00:00', stage: 'translate', kind: 'stage_started', data: {} },
    {
      seq: 2,
      at: '2026-09-18T00:00:01+00:00',
      stage: 'translate',
      kind: 'stage_finished',
      data: { mode: 'whole' },
    },
    { seq: 3, at: '2026-09-18T00:00:02+00:00', stage: 'report', kind: 'stage_finished', data: {} },
  ];
  writeFileSync(
    join(runDir, 'events.jsonl'),
    events.map((event) => JSON.stringify(event)).join('\n') + '\n',
    'utf-8',
  );
}

interface CapturedFrame {
  /** 页面内的相对时刻（performance.now()，毫秒）。 */
  at: number;
  /** 一帧的原始文本。 */
  text: string;
}

declare global {
  interface Window {
    __w14Sse?: CapturedFrame[];
  }
}

/** 在页面里用 fetch 读原始 SSE 帧，逐块记下到达时刻（`at` 是 performance.now()）。 */
async function openRawStream(page: Page) {
  await page.evaluate((url: string) => {
    window.__w14Sse = [];
    void (async () => {
      const response = await fetch(url, { headers: { accept: 'text/event-stream' } });
      const reader = response.body?.getReader();
      if (!reader) return;
      const decoder = new TextDecoder();
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        if (value) {
          window.__w14Sse?.push({
            at: performance.now(),
            text: decoder.decode(value, { stream: true }),
          });
        }
      }
    })();
  }, `${API}/documents/${DID}/events/stream?run_id=${RUN_ID}&after_seq=0`);
}

/** 页面里收到的全部 SSE 帧（按到达顺序）。 */
async function rawFrames(page: Page): Promise<CapturedFrame[]> {
  return (await page.evaluate(() =>
    (window.__w14Sse ?? []).map((frame) => ({ at: frame.at, text: frame.text })),
  )) as CapturedFrame[];
}

/**
 * 切成**真正的帧**：一个 chunk 里可能挤着好几帧（服务端一次 yield 两条，TCP 也可能合并），
 * 所以按空行切——按 chunk 数帧会把同一块里的多条算成一条。
 */
function splitFrames(frames: CapturedFrame[]): CapturedFrame[] {
  return frames.flatMap((chunk) =>
    chunk.text
      .split('\n\n')
      .filter((text) => text.trim() !== '')
      .map((text) => ({ at: chunk.at, text })),
  );
}

/** 只看 job_update 帧（虚拟 kind 的通知）。 */
function jobUpdateFrames(frames: CapturedFrame[]): CapturedFrame[] {
  return splitFrames(frames).filter((frame) => frame.text.includes('event: job_update'));
}

test.beforeAll(() => {
  mkdirSync(SHOT_DIR, { recursive: true });
  writeFixture();

  execFileSync('chmod', ['755', SLEEP_STUB, CANDIDATE_STUB]);

  // 两条 stub profile（命令是脚本**绝对路径**：子进程 cwd 是隔离副本/workdir，相对路径解析不到）
  mkdirSync(STATE_DIR, { recursive: true });
  if (existsSync(PROFILES_FILE)) {
    writeFileSync(PROFILES_BACKUP, readFileSync(PROFILES_FILE));
  }
  writeFileSync(
    PROFILES_FILE,
    `${JSON.stringify(
      {
        'w14-sleep': { label: 'W14 Sleep', translator: SLEEP_STUB },
        'w14-candidate': { label: 'W14 Candidate', translator: `${CANDIDATE_STUB} "${CANDIDATE}"` },
      },
      null,
      2,
    )}\n`,
    'utf-8',
  );
});

test.afterAll(async ({ request }) => {
  await cancelActiveJobs(request).catch(() => undefined);
  stopViewer(WORKDIR);
  if (existsSync(PROFILES_BACKUP)) {
    writeFileSync(PROFILES_FILE, readFileSync(PROFILES_BACKUP));
    rmSync(PROFILES_BACKUP);
  } else if (existsSync(PROFILES_FILE)) {
    rmSync(PROFILES_FILE);
  }
});

test('SSE：job 状态变化推成 job_update 帧（命名空间 + 时序 + 失效链路）', async ({
  page,
  request,
}) => {
  test.setTimeout(120_000);
  const pageErrors: string[] = [];
  page.on('pageerror', (error) => pageErrors.push(String(error)));
  // 记录页面发出的请求：终态那一刀必须让页面自己再拉一次产物清单（不重新加载）
  const requests: { at: number; path: string }[] = [];
  page.on('request', (req) => {
    const path = new URL(req.url()).pathname;
    if (path.startsWith(API)) requests.push({ at: Date.now(), path });
  });

  await page.goto(`/#/d/${DID}/progress`);
  await expect(page.locator('[data-od-id="job-panel"]')).toBeVisible();

  // 工作台自己那条流：真连上（open → 「实时」）。没有它 job_update 就没人收。
  await expect(page.locator('[data-od-id="event-stream-status"]')).toHaveAttribute(
    'data-status',
    'open',
    { timeout: 20_000 },
  );

  await openRawStream(page);

  // ---- 触发一次真 job（retranslate：只跑 bdt translate --ids，隔离副本，不跑 build）----
  const accepted = await request.post(`${API}/documents/${DID}/paragraphs/${PID}/retranslate`, {
    data: { profile: 'w14-candidate' },
  });
  expect(accepted.status(), await accepted.text()).toBe(202);
  const postedJobId = ((await accepted.json()) as { job_id: string }).job_id;
  const tAfterPost = Date.now();
  // 同一个时钟（页面用的 performance.now）量“POST 返回 → 帧到达”的延迟
  const postAtInPage = await page.evaluate(() => performance.now());

  // queued 那帧：POST 返回时状态已经落盘，帧应当在一个 SSE 轮询周期（0.5s）内到
  await expect
    .poll(async () => jobUpdateFrames(await rawFrames(page)).length, {
      timeout: 2_500,
      message: `POST 之后 ${FALLBACK_POLL_MS}ms 的兜底轮询之前，SSE 就该推来 job_update`,
    })
    .toBeGreaterThan(0);
  const firstFrame = jobUpdateFrames(await rawFrames(page))[0];
  const firstDelayMs = Math.round(firstFrame.at - postAtInPage);
  // 服务端是先落盘再返回 202，所以 `queued` 那一帧可能**早于** POST 响应被读到（负数）。
  // 那也说明推送不慢于轮询，只是“已经先到了”；照实把两种情况都印出来，断言用下界 0。
  const firstDelayLabel =
    firstDelayMs > 0 ? `${firstDelayMs}ms` : '≤0ms（POST 响应回来时它已经在流里）';
  console.log(
    `[w14] 第一帧 job_update 在 POST 返回后 ${firstDelayLabel} 到达（兜底轮询是 ${FALLBACK_POLL_MS}ms）`,
  );
  expect(Math.max(0, firstDelayMs), 'SSE 推送不应当慢于兜底轮询').toBeLessThan(FALLBACK_POLL_MS);

  // ---- 等 job 落定（这一次是服务端事实，测试用 API 读；页面靠 SSE）----
  await expect
    .poll(
      async () => (await jobs(request)).find((job) => job.job_id === postedJobId)?.status,
      { timeout: 60_000, message: 'retranslate job 应当落定' },
    )
    .toBe('succeeded');

  await expect
    .poll(
      async () =>
        jobUpdateFrames(await rawFrames(page)).some((frame) => frame.text.includes('succeeded')),
      { timeout: 2_500, message: '终态 job_update 应当在兜底轮询之前到' },
    )
    .toBe(true);

  const allFrames = await rawFrames(page);
  const frames = jobUpdateFrames(allFrames);
  const text = frames.map((frame) => frame.text).join('');
  // 1) 帧形状（api.md §1.4.1）：event/id/data 三行，id 是 job 命名空间
  expect(text).toContain('event: job_update');
  expect(text).toContain(`id: ${postedJobId}:1`);
  expect(text).toContain(`id: ${postedJobId}:2`);
  expect(text).toContain(`id: ${postedJobId}:3`);
  // 同一条流里的 run 事件照旧（W03 帧形状没被 job_update 顶掉）：id 仍是 <run_id>:<seq>
  const runText = splitFrames(allFrames)
    .map((frame) => frame.text)
    .join('\n\n');
  expect(runText).toContain(`id: ${RUN_ID}:1`);
  expect(runText).toContain('event: stage_finished');
  // job 命名空间不是 run 游标（两个空间互不串台）
  expect(text).not.toContain(`id: ${RUN_ID}:`);
  // 2) data 只有 5 个字段（带 action/status/from_stage/error_code），命令/信封/pid 不进 SSE
  const parsed = frames
    .map((frame) => {
      const dataLine = frame.text
        .split('\n')
        .find((line) => line.startsWith('data: '));
      return dataLine === undefined ? null : JSON.parse(dataLine.slice('data: '.length));
    })
    .filter((payload): payload is Record<string, unknown> => payload !== null);
  const updates = parsed.map(
    (payload) => payload.data as Record<string, unknown>,
  );
  expect(updates.map((update) => update.status)).toEqual(['queued', 'running', 'succeeded']);
  expect(new Set(updates.map((update) => update.job_id))).toEqual(new Set([postedJobId]));
  expect(new Set(updates.map((update) => update.action))).toEqual(new Set(['retranslate']));
  // 三条帧各自的 id 计次单调递增（命名空间里的 <第 n 次状态变化>）
  expect(frames).toHaveLength(3);
  for (const update of updates) {
    expect(Object.keys(update).sort()).toEqual([
      'action',
      'error_code',
      'from_stage',
      'job_id',
      'status',
    ]);
  }

  // 3) 终态那一刀：页面自己又拉了一次产物清单（预览据此换新 PDF），没有重新加载。
  //    这是「job succeeded 后自动刷新预览/产物」在浏览器里的可观察证据。
  await expect
    .poll(
      async () =>
        requests.some(
          (entry) =>
            entry.path === `${API}/documents/${DID}/artifacts` && entry.at >= tAfterPost,
        ),
      { timeout: 5_000, message: '终态的 job_update 应当让页面失效产物清单' },
    )
    .toBe(true);

  await page.screenshot({ path: join(SHOT_DIR, 'e2e-w14-job-update.png') });
  expect(pageErrors, '页面不应有未捕获异常').toEqual([]);

  await waitIdle(request);
  stopViewer(WORKDIR);
});

test('UI：运行中的 run job 显示「已译段落 N/M（套版后更新）」，取消后立刻切到已取消', async ({
  page,
  request,
}) => {
  test.setTimeout(120_000);
  await page.goto(`/#/d/${DID}/progress`);
  await expect(page.locator('[data-od-id="job-panel"]')).toBeVisible();
  // 上一个用例的 job 已落定 → 显示开始卡
  await expect(page.locator('[data-od-id="start-job-card"]')).toBeVisible({ timeout: 20_000 });

  await page.locator('[data-od-id="start-job-profile"]').selectOption('w14-sleep');
  await page.getByText('高级：起点阶段').click();
  await page.locator('[data-od-id="start-job-from"]').selectOption('translate');
  await page.locator('[data-od-id="start-job-submit"]').click();

  await expect(page.locator('[data-od-id="active-job-status"]')).toHaveAttribute(
    'data-status',
    'running',
    { timeout: 30_000 },
  );

  // 诚实的进度文案：N/M 来自详情字段（fixture 里是 1 行译文 / 1 个段落），并且写明
  // 「套版后更新」——翻译是整篇单次子进程调用，运行中这个数不会跳动。
  const progress = page.locator('[data-od-id="active-job-progress"]');
  await expect(progress).toBeVisible();
  await expect(progress).toContainText('已译段落 1/1');
  await expect(progress).toContainText('套版后更新');
  await expect(progress).toHaveAttribute('data-translated', '1');
  await expect(progress).toHaveAttribute('data-paragraphs', '1');
  await page.screenshot({ path: join(SHOT_DIR, 'e2e-w14-running.png') });

  // 从 API 取消（不走 UI 的 mutation）→ 页面的状态切换只能来自 SSE 的 job_update，
  // 否则要等兜底轮询（5s）。这里量出实际延迟并断言它小于兜底间隔。
  const running = (await jobs(request)).find((job) => job.status === 'running');
  expect(running, '应当有 running 的 job').toBeTruthy();
  const startedAt = Date.now();
  const canceled = await request.post(`${API}/jobs/${running?.job_id}/cancel`);
  expect(canceled.ok(), await canceled.text()).toBe(true);

  await expect(page.locator('[data-od-id="active-job-outcome"]')).toContainText('已取消', {
    timeout: FALLBACK_POLL_MS,
  });
  const elapsedMs = Date.now() - startedAt;
  console.log(`[w14] 取消后 UI 在 ${elapsedMs}ms 内切到「已取消」（兜底轮询是 ${FALLBACK_POLL_MS}ms）`);
  expect(elapsedMs).toBeLessThan(FALLBACK_POLL_MS);

  await page.screenshot({ path: join(SHOT_DIR, 'e2e-w14-canceled.png') });
  await waitIdle(request);
  stopViewer(WORKDIR);
});
