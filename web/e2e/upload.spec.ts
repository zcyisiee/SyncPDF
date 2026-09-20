/**
 * W08 真用例：上传 → 开始翻译 → 取消（真 `bdt serve --root tmp` + 真 multipart + 真子进程）。
 *
 * 三条链路：
 * 1. 左栏拖/选上传（`POST /documents`）→ 左栏列表出现新论文卡（did 前缀 `up-sample-`）→ 进工作台；
 * 2. 点「开始翻译」（`from=parse`：上传的文档只有 `source.pdf`）→ job 进 running（服务环境有
 *    MinerU token 时）**或**诚实失败且 error_code 非空（没有 token / MinerU 不可用）——
 *    两个分支都算通过，但会在 annotation 里写明环境分支；不做"必成功"的假断言；
 * 3. 取消链路：预置工作目录 + `sleep-t` profile（stub translator 长睡）→ 从 translate 起跑 →
 *    running → 取消（confirm）→ canceled。
 *
 * fixture 与副作用（都在 `tmp/`，本仓库不入库）：
 * - `web/e2e/fixtures/sample.pdf`：602 字节的最小**合法** PDF（正确 xref），提交进仓库；
 * - `tmp/w08-e2e-cancel-<时间戳>/`：本用例自建的起跑工作目录（`agent/document.md`）；
 * - `tmp/.bdt-serve/profiles.json`：写入 `sleep-t` 前先备份，`afterAll` 还原（原本不存在就删掉）；
 * - 用例结束取消活动 job 并停掉 debug 查看器，避免残留进程与端口。
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
const PROFILES_BACKUP = join(STATE_DIR, 'profiles.json.w08-e2e-backup');
const SHOT_DIR = join(WEB_ROOT, 'tmp-smoke');
const PDF_FIXTURE = join(WEB_ROOT, 'e2e', 'fixtures', 'sample.pdf');
const SLEEP_STUB = join(WEB_ROOT, 'e2e', 'fixtures', 'sleep-translator.sh');
/**
 * 取消用例的起跑工作目录：**每次跑一个新 did**。
 * job 历史存在 serve 的状态目录（`.bdt-serve/jobs/*.json`）里、按 did 归档，删 workdir 清不掉它，
 * 所以固定 did 会让第二次跑看到上一次那条 canceled job（工作台显示 ActiveJobCard 而不是开始卡）。
 */
const CANCEL_DID = `w08-e2e-cancel-${new Date().toISOString().replace(/[-:T]/g, '').slice(0, 14)}`;
const CANCEL_WORKDIR = join(SERVE_ROOT, CANCEL_DID);

const API = '/api/v1';

/** 上传后的 did：服务端 slug 取自文件名（sample.pdf → `up-sample-<时间戳>`）。 */
const UPLOADED_DID_PREFIX = 'up-sample-';

async function jobStatuses(request: APIRequestContext, did: string) {
  const response = await request.get(`${API}/documents/${did}/jobs`);
  expect(response.status(), `job 列表不可用（${did}）`).toBe(200);
  return (await response.json()) as { job_id: string; status: string; error_code: string | null }[];
}

/** 收尾：把该文档的活动 job 全取消（不留 running 子进程）。 */
async function cancelActiveJobs(request: APIRequestContext, did: string) {
  for (const job of await jobStatuses(request, did).catch(() => [])) {
    if (job.status === 'queued' || job.status === 'running') {
      await request.post(`${API}/jobs/${job.job_id}/cancel`);
    }
  }
}

/** 停掉该 workdir 的 debug 查看器（否则它会挂 30 分钟才自退）。 */
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

test.beforeAll(() => {
  mkdirSync(SHOT_DIR, { recursive: true });
  expect(existsSync(PDF_FIXTURE), 'fixture PDF 缺失：web/e2e/fixtures/sample.pdf').toBe(true);

  // 取消用例的起跑工作目录：一份最小 parse 产物（translate 前置只要 agent/document.md）。
  mkdirSync(join(CANCEL_WORKDIR, 'agent'), { recursive: true });
  writeFileSync(
    join(CANCEL_WORKDIR, 'agent', 'document.md'),
    '<!--P01-001-->\nHello W08 cancel smoke.\n',
    'utf-8',
  );

  // `sleep-t` profile：命令是**脚本绝对路径**（子进程 cwd = workdir，相对路径解析不到）。
  mkdirSync(STATE_DIR, { recursive: true });
  if (existsSync(PROFILES_FILE)) {
    writeFileSync(PROFILES_BACKUP, readFileSync(PROFILES_FILE));
  }
  writeFileSync(
    PROFILES_FILE,
    `${JSON.stringify({ 'sleep-t': { translator: SLEEP_STUB } }, null, 2)}\n`,
    'utf-8',
  );
  execFileSync('chmod', ['755', SLEEP_STUB]);
});

test.afterAll(async ({ request }) => {
  await cancelActiveJobs(request, CANCEL_DID);
  stopViewer(CANCEL_WORKDIR);
  // 还原 profiles.json（原本没有就删掉，别留一条测试专用 profile 给后续手工冒烟）
  if (existsSync(PROFILES_BACKUP)) {
    writeFileSync(PROFILES_FILE, readFileSync(PROFILES_BACKUP));
    rmSync(PROFILES_BACKUP);
  } else if (existsSync(PROFILES_FILE)) {
    rmSync(PROFILES_FILE);
  }
});

/** 进入某个 did 的工作台（二级视图合并后唯一工作台），返回该文档的 job 列表状态轮询器。 */
async function openProgress(page: Page, did: string) {
  await page.goto(`/#/d/${did}/progress`);
  await expect(page.locator('[data-od-id="workbench"]')).toBeVisible();
}

test('上传 PDF → 列表出现新文档 → 开始翻译（from=parse，诚实断言两种环境分支）', async ({
  page,
  request,
}) => {
  const consoleErrors: string[] = [];
  const notFoundPaths = new Set<string>();
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text());
  });
  page.on('response', (response) => {
    if (response.status() === 404) notFoundPaths.add(new URL(response.url()).pathname);
  });

  await page.goto('/#/library');
  // 左栏论文导航常驻（中栏是「选择或上传一篇论文」空态，不再有独立的文件库屏）
  await expect(page.locator('[data-od-id="nav-rail"]')).toBeVisible();
  await expect(page.locator('[data-od-id="library-empty"]')).toBeVisible();

  // 本次上传之前的文档集合：upload 之后只认**新出现**的那个 did
  // （tmp/ 里可能还留着上一次 e2e 跑出来的 up-sample-* 文档，不能按前缀取第一个）
  const before = new Set(
    ((await (await request.get(`${API}/documents`)).json()) as { did: string }[]).map(
      (item) => item.did,
    ),
  );

  // 通过文件选择器上传（拖放的等价路径在 vitest 里覆盖；这里走真 multipart）
  await page.locator('[data-od-id="upload-input"]').setInputFiles(PDF_FIXTURE);

  let did = '';
  await expect
    .poll(
      async () => {
        const listed = (await (await request.get(`${API}/documents`)).json()) as { did: string }[];
        did = listed.map((item) => item.did).find((name) => name.startsWith(UPLOADED_DID_PREFIX) && !before.has(name)) ?? '';
        return did;
      },
      { timeout: 20_000, message: '上传后 --root 下应出现新的 up-sample-* 文档' },
    )
    .not.toBe('');

  const card = page.locator(`[data-od-id="paper-${did}"]`);
  await expect(card).toBeVisible({ timeout: 20_000 });
  // 服务端真建了 source.pdf（W03 白名单里的 kind=source）
  const artifacts = await (await request.get(`${API}/documents/${did}/artifacts`)).json();
  expect(artifacts.map((item: { name: string }) => item.name)).toContain('source.pdf');
  // 不自动跳转（用户自己点卡片）
  await expect(page).toHaveURL(/#\/library$/);
  await card.click();
  await expect(page).toHaveURL(new RegExp(`#/d/${did}/progress$`));
  await expect(page.locator('[data-od-id="workbench"]')).toBeVisible();

  // 上传的文档没有 parse 产物 → job 会从 parse 起（起点自动判断，不再有下拉）；
  // 模型等配置在设置屏设默认，任务控制（开始翻译）在右栏底部操作区，状态徽标同处
  const startButton = page.locator('[data-od-id="start-job-submit"]');
  await expect(startButton).toBeEnabled({ timeout: 20_000 });
  await startButton.click();
  await page.screenshot({ path: join(SHOT_DIR, 'w08-e2e-start.png'), fullPage: false });

  // 诚实断言：要么真跑起来（running），要么失败且 error_code 非空（无 MinerU 环境）。
  // 失败态的任务状态徽标带 data-tip=「error_code：message」（旧 active-job-outcome 条已并入）。
  const status = page.locator('[data-od-id="active-job-status"]');
  await expect(status).toBeVisible({ timeout: 30_000 });
  const running = page.locator('[data-od-id="active-job-status"][data-status="running"]');
  const failed = page.locator('[data-od-id="active-job-status"][data-status="failed"]');
  await expect(running.or(failed).first()).toBeVisible({ timeout: 60_000 });

  const jobs = await jobStatuses(request, did);
  const latest = jobs[0];
  expect(latest, 'POST job 之后服务端应有该文档的 job').toBeTruthy();
  let branch: string;
  if (latest.status === 'running' || latest.status === 'queued') {
    branch = `running（服务环境有 MinerU token：${latest.status}）`;
  } else {
    branch = `诚实失败（无 MinerU token/不可用）：${latest.status} / ${latest.error_code}`;
    expect(latest.error_code, '失败必须有 error_code（不是静默）').toBeTruthy();
    await expect(page.locator('[data-od-id="active-job-status"] [data-tip]')).toContainText(
      latest.error_code ?? '',
    );
  }
  test.info().annotations.push({ type: 'mineru', description: branch });
  console.log(`[w08] 上传后开始翻译：${branch}（did=${did}）`);

  await page.screenshot({ path: join(SHOT_DIR, 'w08-e2e-running.png'), fullPage: false });

  // 全新文档没有任何 run 归档：`events`（events_unavailable）与 `geometry` 的 404 是**契约行为**
  // （W06/W05 都归一成空态/小条），浏览器仍会给失败的 fetch 打一条 console error。
  // 放行这两类 404；其它任何 console error 都算回归。
  expect(
    [...notFoundPaths].every((path) => path.endsWith('/events') || path.endsWith('/geometry')),
    `非预期 404：${[...notFoundPaths].join(', ')}`,
  ).toBe(true);
  expect(
    consoleErrors.filter((text) => !text.includes('404')),
    consoleErrors.join('\n'),
  ).toEqual([]);

  await cancelActiveJobs(request, did); // 不留 running 子进程（parse 可能正在调 MinerU）
  stopViewer(join(SERVE_ROOT, did));
});

test('运行中任务：取消 → canceled（真进程组终止）', async ({ page, request }) => {
  await openProgress(page, CANCEL_DID);

  // stub 脚本 profile（sleep-t）不再出现在 UI 下拉里（模型选择只列内置 harness），
  // 长睡任务改由 API 直接提交；UI 负责如实展示徽标与取消。
  const accepted = await request.post(`${API}/documents/${CANCEL_DID}/jobs`, {
    data: { action: 'run', from: 'translate', profile: 'sleep-t' },
  });
  expect(accepted.ok(), await accepted.text()).toBe(true);

  await expect(page.locator('[data-od-id="active-job-status"]')).toHaveAttribute(
    'data-status',
    'running',
    { timeout: 30_000 },
  );
  await expect(page.locator('[data-od-id="cancel-job"]')).toBeVisible();

  // 取消要 confirm：Playwright 默认忽略对话框 → 显式 accept
  page.once('dialog', (dialog) => void dialog.accept());
  await page.locator('[data-od-id="cancel-job"]').click();
  await page.screenshot({ path: join(SHOT_DIR, 'w08-e2e-cancel.png'), fullPage: false });

  // 取消后徽标立刻切「已取消」（tooltip 带结果文案），并出现「重试」
  await expect(page.locator('[data-od-id="active-job-status"]')).toHaveAttribute(
    'data-status',
    'canceled',
    { timeout: 30_000 },
  );
  await expect(page.locator('[data-od-id="active-job-status"] [data-tip]')).toContainText('已取消');
  await expect(page.locator('[data-od-id="retry-job"]')).toBeVisible();

  const jobs = await jobStatuses(request, CANCEL_DID);
  expect(jobs[0].status).toBe('canceled');
  stopViewer(CANCEL_WORKDIR);
});
