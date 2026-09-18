/**
 * W15 集成验收：**一条贯穿全旅程**的真浏览器用例 —— 静态构建 → 上传 → 真 job 的实时进度
 * （事件流 / 时间线 live 段 / `job_update` 推送）→ 手改译文与 bbox → 候选重译与采用 →
 * 编译链路 → 版本归档 → **下载 PDF 的字节级校验**。
 *
 * 与既有 e2e 的分工：单点语义各自有专属 spec（上传 `upload.spec.ts`、点段编辑与**真编译**
 * `edit.spec.ts`、候选 `retranslate.spec.ts`、归档 `archive.spec.ts`、SSE 时序
 * `streaming.spec.ts`、词表 `glossary.spec.ts`）。本文件不重复它们的细节，只把**一条完整
 * 用户旅程**串起来，验证各段之间的接线（配置 → job 记录 → 实时 UI → 草稿 → 候选 → 编译状态
 * → 归档 → 下载字节）。
 *
 * 环境（真组件，都在仓库 `tmp/` 下、不入库）：
 * - **静态模式**：`beforeAll` 跑 `pnpm build`，再用**本 spec 自己起的** `bdt serve` 伺候
 *   `web/dist`（W15 新增的静态伺服）：SPA 与 `/api/v1` 同一个端口 = 生产拓扑（无 Vite 代理）。
 *   既有 15 个 spec 仍走 playwright 配置里的 Vite dev 服务器，互不影响。
 * - **专属 root**：`tmp/w15-integration-<时间戳>/`（不共用 `tmp/.bdt-serve/profiles.json`），
 *   里面预置两条 stub profile 与一条词表；上传的文档也落在它里面。
 * - stub translator：`fixtures/slow-translator.sh`（离线，睡 8s 再回显段落标记）——
 *   让 translate 阶段停留几秒，好在**运行中**断言实时链路。
 *
 * 诚实标注（brief + 主控裁决 (B)）：
 * - 上传后的文档只有 `source.pdf`；spec 在它目录里**按真实形状预置 parse 阶段的产物**
 *   （`agent/document.md`/`anchors.json`/`translated.md`/`translated.jsonl`/`layout_geometry.json`
 *   与一份旧的 `output/paper.mono.pdf`），跳过需要 MinerU 联网的 parse。这不是伪造空态。
 * - 真 `bdt run --from translate` 会**如实失败**（apply/build 需要真 IR `agent/state.pkl`，
 *   本 spec 不跑真 LaTeX build）：这里断言的是「UI 完整呈现 running → failed + error_code
 *   可见」与「失败不动产物」。**编译成功 → 新 rN → 下载**这条链用 §3.7 落盘夹具（与 W12
 *   `archive.spec.ts` 同一先例）验证，**不冒充真 build**（真 build 由 W10 `edit.spec.ts` 覆盖）。
 */
import { expect, test } from '@playwright/test';
import type { APIRequestContext, Page } from '@playwright/test';
import { execFileSync, spawn, type ChildProcess } from 'node:child_process';
import { createHash } from 'node:crypto';
import {
  chmodSync,
  closeSync,
  existsSync,
  mkdirSync,
  openSync,
  readFileSync,
  readdirSync,
  writeFileSync,
} from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const WEB_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const REPO_ROOT = resolve(WEB_ROOT, '..');
const BDT = join(REPO_ROOT, '.venv', 'bin', 'bdt');
const SHOT_DIR = join(WEB_ROOT, 'tmp-smoke');
const SAMPLE_PDF = join(WEB_ROOT, 'e2e', 'fixtures', 'sample.pdf');
const SLOW_STUB = join(WEB_ROOT, 'e2e', 'fixtures', 'slow-translator.sh');
const CANDIDATE_STUB = join(WEB_ROOT, 'e2e', 'fixtures', 'candidate-translator.sh');
const API = '/api/v1';

const STAMP = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 14);
/** 专属 root（本 spec 自己起的 serve 的 `--root`）：上传的文档与状态都隔离在这里。 */
const ROOT = join(REPO_ROOT, 'tmp', `w15-integration-${STAMP}`);
const STATE_DIR = join(ROOT, '.bdt-serve');
const GLOSSARY_SOURCE = 'integration';
const GLOSSARY_TARGET = '集成';
const PID = 'P01-001';
const BASELINE = 'W15 e2e 基线译文';
const CANDIDATE = 'W15 e2e 候选译文（stub）';
/** 前端的兜底轮询（`lib/jobs.ts::JOBS_ACTIVE_REFETCH_MS`）：SSE 要赢的就是这个数。 */
const FALLBACK_POLL_MS = 5_000;
const UPLOADED_DID_RE = /^up-sample-/;

/** 上传后的文档 did（`POST /documents` 服务端按文件名生成，未知前缀之外的部分照抄）。 */
let did = '';
/** 本 spec 起的 serve（静态模式）。 */
let serve: ChildProcess | null = null;
let serveOrigin = '';
let serveLogPath = '';

const SAMPLE_BYTES = readFileSync(SAMPLE_PDF);
/** 「编译成功发布的新版」字节：与上一版不同（同一份 PDF + 一段尾注释）。 */
const MONO_BYTES = Buffer.concat([SAMPLE_BYTES, Buffer.from('\n% W15 integration build r-N\n')]);

const workdirOf = (name: string) => join(ROOT, name);

interface JobRow {
  job_id: string;
  action: string;
  status: string;
  from_stage: string | null;
  pages: string | null;
  dual: boolean;
  use_glossary: boolean;
  error_code: string | null;
}

// --------------------------------------------------------------------------- #
// 小工具
// --------------------------------------------------------------------------- #
function sha256Bytes(bytes: Buffer): string {
  return createHash('sha256').update(bytes).digest('hex');
}

function sha256File(path: string): string {
  return sha256Bytes(readFileSync(path));
}

async function apiJson<T>(request: APIRequestContext, path: string): Promise<T> {
  const response = await request.get(`${serveOrigin}${API}${path}`);
  expect(response.ok(), `GET ${path} → ${response.status()}`).toBe(true);
  return (await response.json()) as T;
}

async function jobs(request: APIRequestContext, name = did): Promise<JobRow[]> {
  return await apiJson<JobRow[]>(request, `/documents/${name}/jobs`);
}

/** 等该文档没有活动 job（上一次任务落定；否则下一次提交 409 `document_busy`）。 */
async function waitIdle(request: APIRequestContext, timeoutMs = 60_000) {
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

/**
 * 写一次草稿（改译文 / 拖 bbox / 改数值）并等 `revision` 前进。
 *
 * 为什么需要重试：每次草稿写成功都会触发服务端 1.5s 防抖编译，而**活动 job 期间编辑器是只读的**
 * （服务端 409 `document_busy`）。所以先等空闲再写；如果写入正好撞上上一次改动带来的编译，
 * 就再等一次空闲重来（最多 3 次）。
 */
async function editDraft(
  request: APIRequestContext,
  expectedRevision: number,
  interact: () => Promise<void>,
) {
  let last = -1;
  for (let attempt = 0; attempt < 3; attempt += 1) {
    await waitIdle(request);
    await interact();
    try {
      await expect
        .poll(
          async () =>
            (await apiJson<{ revision: number }>(request, `/documents/${did}/draft`)).revision,
          { timeout: 10_000, intervals: [250] },
        )
        .toBe(expectedRevision);
      return;
    } catch (error) {
      last = (await apiJson<{ revision: number }>(request, `/documents/${did}/draft`)).revision;
      if (attempt === 2) throw error;
    }
  }
  throw new Error(`草稿 revision 没前进到 r${expectedRevision}（当前 r${last}）`);
}

async function latestJob(request: APIRequestContext): Promise<JobRow> {
  const listed = await jobs(request);
  expect(listed.length, '该文档应至少有一条 job').toBeGreaterThan(0);
  return listed[0];
}

async function shot(page: Page, name: string) {
  await page.screenshot({ path: join(SHOT_DIR, `${name}.png`) });
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
    // 查看器已经不在了：收尾失败不影响断言
  }
}

// --------------------------------------------------------------------------- #
// fixture
// --------------------------------------------------------------------------- #
/**
 * 上传后的文档只有 `source.pdf`。这里按**真实形状**补上 parse 阶段会写出的产物
 * （跳过需要 MinerU 的联网 parse），外加一份「上一次成功编译」的 `output/paper.mono.pdf`：
 * 后者的 sha256 用来证明失败的 job/编译**没有**改产物。
 */
function writeParseProducts(name: string) {
  const workdir = workdirOf(name);
  const agent = join(workdir, 'agent');
  mkdirSync(join(workdir, 'output'), { recursive: true });
  mkdirSync(agent, { recursive: true });
  writeFileSync(join(agent, 'document.md'), `<!--${PID}-->\nW15 e2e source sentence.\n`, 'utf-8');
  writeFileSync(
    join(agent, 'anchors.json'),
    `${JSON.stringify(
      {
        rows: [
          {
            id: PID,
            page: 0,
            layout_label: 'text',
            canonical: 'W15 e2e source sentence.',
            markdown: 'W15 e2e source sentence.',
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
    join(agent, 'translated.md'),
    `<!--MD_HEADER-->\n\n<!-- id=${PID} label=text -->\n${BASELINE}\n`,
    'utf-8',
  );
  writeFileSync(
    join(agent, 'translated.jsonl'),
    `${JSON.stringify({ id: PID, target: BASELINE, layout_label: 'text' })}\n`,
    'utf-8',
  );
  writeFileSync(
    join(agent, 'layout_geometry.json'),
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
  writeFileSync(join(workdir, 'output', 'paper.mono.pdf'), SAMPLE_BYTES);
}

/**
 * §3.7 落盘夹具：把「已经编译成功 r{N}」的状态写全（`compile.json` + `versions.json` +
 * `versions/<r>.pdf` + 当前发布的 `output/paper.mono.pdf`）。归档视图与下载链读的就是这些
 * 落盘形状 —— 不跑真 LaTeX build（裁决 (B)）。
 *
 * 质量门禁快照（`run_state.json` 的 `quality`）也一并写：`pipeline_ok` 由 check 与 reviewer
 * **都**为 `pass` 才算通过（`views.quality_status`），详情端点与归档快照同源。已有的
 * `stages`（真 translate 阶段）保留，只合并 `quality`。
 */
function writeArchiveFixture(name: string, currentRevision: number) {
  const workdir = workdirOf(name);
  const historyRevision = currentRevision - 1;
  mkdirSync(join(workdir, '.bdt-serve', 'versions'), { recursive: true });
  mkdirSync(join(workdir, 'output'), { recursive: true });
  writeFileSync(join(workdir, 'output', 'paper.mono.pdf'), MONO_BYTES);
  writeFileSync(join(workdir, '.bdt-serve', 'versions', `${historyRevision}.pdf`), SAMPLE_BYTES);
  writeFileSync(join(workdir, '.bdt-serve', 'versions', `${currentRevision}.pdf`), MONO_BYTES);
  writeFileSync(
    join(workdir, '.bdt-serve', 'versions.json'),
    `${JSON.stringify(
      {
        version: 1,
        items: [
          {
            revision: historyRevision,
            created_at: '2026-09-18T00:00:00.000Z',
            trigger: 'manual',
            artifact_name: 'paper.mono.pdf',
            bytes: SAMPLE_BYTES.length,
            sha256_head: sha256Bytes(SAMPLE_BYTES),
            quality: { check_verdict: 'needs_fix', pipeline_ok: false },
          },
          {
            revision: currentRevision,
            created_at: '2026-09-18T00:05:00.000Z',
            trigger: 'debounce',
            artifact_name: 'paper.mono.pdf',
            bytes: MONO_BYTES.length,
            sha256_head: sha256Bytes(MONO_BYTES),
            quality: { check_verdict: 'pass', pipeline_ok: true },
          },
        ],
      },
      null,
      2,
    )}\n`,
    'utf-8',
  );
  const stateFile = join(workdir, 'agent', 'run_state.json');
  const state = existsSync(stateFile)
    ? (JSON.parse(readFileSync(stateFile, 'utf-8')) as Record<string, unknown>)
    : {};
  writeFileSync(
    stateFile,
    `${JSON.stringify(
      {
        ...state,
        quality: {
          check: { verdict: 'pass', reasons: [] },
          reviewer: { status: 'pass', fix_rounds: 0 },
        },
      },
      null,
      2,
    )}\n`,
    'utf-8',
  );
  writeFileSync(
    join(workdir, '.bdt-serve', 'compile.json'),
    `${JSON.stringify(
      {
        status: 'ok',
        revision: currentRevision,
        artifact: {
          name: 'paper.mono.pdf',
          size: MONO_BYTES.length,
          revision: currentRevision,
        },
      },
      null,
      2,
    )}\n`,
    'utf-8',
  );
}

/** 起本 spec 自己的静态 serve：`--port 0`，从 stdout 的第一行 JSON 拿真实端口。 */
async function startStaticServe(): Promise<{ proc: ChildProcess; origin: string; logPath: string }> {
  const logPath = join(WEB_ROOT, 'tmp-smoke', 'w15-serve.log');
  closeSync(openSync(logPath, 'w'));
  const logFd = openSync(logPath, 'a');
  const proc = spawn(
    BDT,
    ['serve', '--root', ROOT, '--port', '0'],
    {
      cwd: REPO_ROOT,
      env: {
        ...process.env,
        PYTHONPATH: REPO_ROOT,
        PATH: `${join(REPO_ROOT, '.venv', 'bin')}:${process.env.PATH ?? ''}`,
      },
      stdio: ['ignore', 'pipe', logFd],
    },
  );

  const banner = await new Promise<string>((resolveLine, reject) => {
    let buffer = '';
    const timer = setTimeout(() => reject(new Error('serve 启动信封 20s 内没出来')), 20_000);
    proc.stdout?.on('data', (chunk: Buffer) => {
      buffer += chunk.toString('utf-8');
      const newline = buffer.indexOf('\n');
      if (newline >= 0) {
        clearTimeout(timer);
        resolveLine(buffer.slice(0, newline));
      }
    });
    proc.on('exit', (code) => {
      clearTimeout(timer);
      reject(new Error(`serve 退出（code=${code}），日志：${logPath}`));
    });
  });

  const parsed = JSON.parse(banner) as { ok: boolean; data?: { port: number } };
  expect(parsed.ok, `启动信封应 ok=true：${banner}`).toBe(true);
  const port = parsed.data?.port ?? 0;
  expect(port).toBeGreaterThan(0);
  return { proc, origin: `http://127.0.0.1:${port}`, logPath };
}

test.beforeAll(async () => {
  mkdirSync(SHOT_DIR, { recursive: true });
  mkdirSync(STATE_DIR, { recursive: true });
  mkdirSync(ROOT, { recursive: true });
  expect(existsSync(SAMPLE_PDF), 'fixture PDF 缺失：web/e2e/fixtures/sample.pdf').toBe(true);

  chmodSync(SLOW_STUB, 0o755);
  chmodSync(CANDIDATE_STUB, 0o755);

  // 两条 stub profile（命令是脚本**绝对路径**：子进程 cwd 是 workdir/隔离副本）
  writeFileSync(
    join(STATE_DIR, 'profiles.json'),
    `${JSON.stringify(
      {
        'w15-slow': { label: 'W15 Slow Stub', translator: `${SLOW_STUB} 8` },
        'w15-candidate': { label: 'W15 Candidate', translator: `${CANDIDATE_STUB} "${CANDIDATE}"` },
      },
      null,
      2,
    )}\n`,
    'utf-8',
  );
  // 一条词表：让「使用词表」开关是**可开**的真实状态（空表时前端会禁用）
  writeFileSync(
    join(STATE_DIR, 'glossary.csv'),
    `source,target,note\n${GLOSSARY_SOURCE},${GLOSSARY_TARGET},W15 e2e\n`,
    'utf-8',
  );

  // 静态模式的构建产物（spec 断言的是产物，不是 dev server）
  execFileSync('pnpm', ['build'], { cwd: WEB_ROOT, timeout: 300_000, stdio: 'pipe' });

  const started = await startStaticServe();
  serve = started.proc;
  serveOrigin = started.origin;
  serveLogPath = started.logPath;
  const port = new URL(serveOrigin).port;
  console.log(`[w15] 静态 serve 就绪：${serveOrigin}（root=${ROOT}，端口 ${port}，日志 ${serveLogPath}）`);
});

test.afterAll(async () => {
  if (did !== '') stopViewer(workdirOf(did));
  serve?.kill('SIGTERM');
  serve = null;
});

test('全旅程：静态构建 → 上传 → 实时进度 → 编辑/bbox → 候选采用 → 编译 → 归档下载（字节校验）', async ({
  page,
  request,
}) => {
  test.setTimeout(240_000);
  const pageErrors: string[] = [];
  const notFoundPaths = new Set<string>();
  /** 预览按修订号重新取字节的证据（`?r=<revision>`）。 */
  const artifactRequests: string[] = [];
  page.on('pageerror', (error) => pageErrors.push(String(error)));
  page.on('response', (response) => {
    if (response.status() === 404) notFoundPaths.add(new URL(response.url()).pathname);
  });
  page.on('request', (outgoing) => {
    if (outgoing.url().includes('/artifacts/')) artifactRequests.push(outgoing.url());
  });

  // ========================================================================= #
  // 阶段 0：静态伺服契约（同一端口既给 SPA 又给 API —— 生产拓扑）
  // ========================================================================= #
  const indexResponse = await request.get(`${serveOrigin}/`);
  expect(indexResponse.status()).toBe(200);
  expect(indexResponse.headers()['content-type']).toContain('text/html');
  expect(indexResponse.headers()['cache-control']).toBe('no-cache');
  const indexHtml = await indexResponse.text();
  expect(indexHtml, 'served HTML 应是 Vite 构建的 SPA 入口').toContain('id="root"');

  const assetPath = /src="([^"]*\/assets\/[^"]+)"/.exec(indexHtml)?.[1] ?? '';
  expect(assetPath, 'index.html 里应有带内容哈希的入口脚本').not.toBe('');
  const asset = await request.get(`${serveOrigin}${assetPath}`);
  expect(asset.status()).toBe(200);
  expect(asset.headers()['cache-control'], '哈希资源应长缓存').toContain('immutable');

  const fallback = await request.get(`${serveOrigin}/d/whatever/translate`);
  expect(fallback.status(), 'SPA 深链应回退到入口').toBe(200);
  expect(fallback.headers()['content-type']).toContain('text/html');

  const health = await request.get(`${serveOrigin}${API}/health`);
  expect(health.status()).toBe(200);
  expect((await health.json()) as { status: string }).toMatchObject({ status: 'ok' });
  console.log(`[w15] 静态伺服：index 200/no-cache、${assetPath} 200/immutable、SPA 回退 200、API 同源 ok`);

  // ========================================================================= #
  // 阶段 1：文件库（静态构建的前端）+ 词表屏 + 真上传
  // ========================================================================= #
  await page.goto(`${serveOrigin}/`);
  await expect(page.locator('[data-od-id="dropzone"]')).toBeVisible({ timeout: 30_000 });
  await shot(page, 'w15-01-library-static');

  await page.goto(`${serveOrigin}/#/glossary`);
  await expect(page.locator('[data-od-id="glossary-table"]')).toBeVisible({ timeout: 30_000 });
  // 词表行是输入框（`value` 不进 textContent）：断言 cell 的值，不是文本
  await expect
    .poll(async () =>
      await page.locator('[data-od-id="glossary-cell-source"]').first().inputValue(),
    {
      timeout: 20_000,
      message: 'root 里预置的词表应出现在词表视图（翻译时会带术语约束）',
    })
    .toBe(GLOSSARY_SOURCE);
  await expect(page.locator('[data-od-id="glossary-cell-target"]').first()).toHaveValue(
    GLOSSARY_TARGET,
  );
  await shot(page, 'w15-02-glossary');

  await page.goto(`${serveOrigin}/#/library`);
  await expect(page.locator('[data-od-id="dropzone"]')).toBeVisible();
  await page.locator('[data-od-id="upload-input"]').setInputFiles(SAMPLE_PDF);
  await expect
    .poll(
      async () => {
        const listed = (await apiJson<{ did: string }[]>(request, '/documents')).map(
          (item) => item.did,
        );
        did = listed.find((name) => UPLOADED_DID_RE.test(name)) ?? '';
        return did;
      },
      { timeout: 30_000, message: '上传后 root 下应出现新的 up-sample-* 文档' },
    )
    .not.toBe('');
  const card = page.locator(`[data-od-id="doc-card"][data-did="${did}"]`);
  await expect(card).toBeVisible({ timeout: 20_000 });
  const uploadedArtifacts = await apiJson<{ name: string }[]>(
    request,
    `/documents/${did}/artifacts`,
  );
  expect(uploadedArtifacts.map((item) => item.name)).toContain('source.pdf');
  await shot(page, 'w15-03-uploaded');
  console.log(`[w15] 上传：did=${did}，产物 source.pdf 已落盘`);

  // 补 parse 产物（真实形状；跳过需要 MinerU 的联网 parse）——见文件头「诚实标注」
  writeParseProducts(did);

  // ========================================================================= #
  // 阶段 2：配置 job（profile / 页范围 / dual / 词表开关）+ 提交真 run job
  // ========================================================================= #
  await page.goto(`${serveOrigin}/#/d/${did}/progress`);
  await expect(page.locator('[data-od-id="start-job-card"]')).toBeVisible({ timeout: 30_000 });
  await page.locator('[data-od-id="start-job-profile"]').selectOption('w15-slow');
  await page.locator('[data-od-id="start-job-pages"]').fill('1');
  await page.locator('[data-od-id="start-job-dual"]').check();
  // 词表有 1 条 → 开关可用且默认开（空表时前端禁用它，那是另一条分支）
  await expect(page.locator('[data-od-id="start-job-use-glossary"]')).toBeEnabled();
  await expect(page.locator('[data-od-id="start-job-use-glossary"]')).toBeChecked();
  await page.getByText('高级：起点阶段').click();
  await page.locator('[data-od-id="start-job-from"]').selectOption('translate');
  await shot(page, 'w15-04-configured');

  const beforeRunPdf = sha256File(join(workdirOf(did), 'output', 'paper.mono.pdf'));

  await page.locator('[data-od-id="start-job-submit"]').click();
  await expect(page.locator('[data-od-id="active-job-card"]')).toBeVisible({ timeout: 30_000 });

  // 配置真的进了 job 记录（服务端字段，不是前端自说自话）
  const running = await latestJob(request);
  expect(running.action).toBe('run');
  expect(running.from_stage).toBe('translate');
  expect(running.pages).toBe('1');
  expect(running.dual).toBe(true);
  expect(running.use_glossary).toBe(true);

  // ---- 实时进度：运行中状态 + 事件流条目 + 时间线 live 段 ---------------------
  await expect(page.locator('[data-od-id="active-job-status"]')).toHaveAttribute(
    'data-status',
    'running',
    { timeout: 30_000 },
  );
  // 运行中草稿只读：服务端 409 document_busy（前端据此禁用编辑）——紧跟 running 断言，
  // 免得 stub 的 8s 睡完（job 落定后 PATCH 就不再是 409 了）
  const busy = await request.patch(`${serveOrigin}${API}/documents/${did}/draft`, {
    data: { base_revision: 0, paragraphs: { [PID]: { target: '运行中不该写得进去' } } },
  });
  expect(busy.status(), '活动 job 期间草稿必须只读').toBe(409);
  await expect(page.locator('[data-od-id="event-row"]').first()).toBeVisible({ timeout: 30_000 });
  // 事件流（SSE）真的连上了：job_update 就是走这条流推来的（进度视图的右侧面板默认是「事件」tab）
  await expect(page.locator('[data-od-id="event-stream-status"]')).toHaveAttribute(
    'data-status',
    'open',
    { timeout: 30_000 },
  );
  await expect(
    page.locator('[data-od-id="timeline-stage-translate"][data-state="live"]'),
    '运行中的 run job 应让 translate 段显示为 live',
  ).toBeVisible({ timeout: 30_000 });
  await shot(page, 'w15-05-running');
  console.log('[w15] 运行中：active-job=running、事件流有条目、时间线 translate=live、草稿 409');

  // ---- 终态：如实失败（apply/build 需要真 IR）-------------------------------
  await expect
    .poll(async () => (await latestJob(request)).status, {
      timeout: 120_000,
      intervals: [1_000],
      message: 'run job 应在 stub 睡醒后落定',
    })
    .toMatch(/failed|canceled|succeeded/);
  const finished = await latestJob(request);
  expect(finished.status, '本 spec 的 fixture 没有真 IR：run 应如实失败').toBe('failed');
  expect(finished.error_code, '失败必须带 error_code（不是静默）').toBeTruthy();
  await expect(page.locator('[data-od-id="active-job-outcome"]')).toContainText(
    finished.error_code ?? '',
    { timeout: 30_000 },
  );
  await shot(page, 'w15-06-run-failed');
  // translate 阶段**真跑完了**（stub 译文落进产物；run_state 记 translate=ok）——
  // 失败发生在 apply（本 fixture 没有真 IR `agent/state.pkl`）
  const stubTranslated = readFileSync(join(workdirOf(did), 'agent', 'translated.md'), 'utf-8');
  expect(stubTranslated).toContain('W15 e2e 译文（stub）');
  const runState = JSON.parse(
    readFileSync(join(workdirOf(did), 'agent', 'run_state.json'), 'utf-8'),
  ) as { stages: Record<string, { status: string }> };
  expect(runState.stages.translate.status).toBe('ok');
  // 失败不动「已发布产物」与草稿：旧 PDF 的 sha256 一字不差、草稿仍是 r0
  expect(sha256File(join(workdirOf(did), 'output', 'paper.mono.pdf'))).toBe(beforeRunPdf);
  expect(
    (await apiJson<{ revision: number }>(request, `/documents/${did}/draft`)).revision,
    '失败的 job 不该动草稿',
  ).toBe(0);
  console.log(
    `[w15] run job 终态：${finished.status} / ${finished.error_code}（translate 阶段 ok；UI 已显示失败原因；旧 PDF 与草稿未动）`,
  );

  // ========================================================================= #
  // 阶段 3：SSE 的 job_update + 一次**成功**的真 job（候选重译）
  // ========================================================================= #
  await waitIdle(request);
  await page.goto(`${serveOrigin}/#/d/${did}/translate`);
  await expect(page.locator('[data-od-id="preview-canvas"] canvas')).toBeVisible({ timeout: 30_000 });
  await expect(page.locator(`[data-bbox-id="${PID}"]`)).toBeVisible({ timeout: 30_000 });
  await page.locator(`[data-bbox-id="${PID}"]`).click();
  await expect(page.locator('[data-od-id="selected-paragraph-id"]')).toHaveText(PID);
  await expect(page.locator('[data-od-id="paragraph-target"]')).toHaveValue(BASELINE, {
    timeout: 20_000,
  });
  // 页面内读原始 SSE 帧（run 事件 + job_update 同一条流）
  const eventPage = await apiJson<{ run_id: string }>(request, `/documents/${did}/events`);
  await page.evaluate((url: string) => {
    (window as unknown as { __w15Frames: { at: number; text: string }[] }).__w15Frames = [];
    void (async () => {
      const response = await fetch(url, { headers: { accept: 'text/event-stream' } });
      const reader = response.body?.getReader();
      if (!reader) return;
      const decoder = new TextDecoder();
      for (;;) {
        const { value, done } = await reader.read();
        if (done) break;
        if (value) {
          (window as unknown as { __w15Frames: { at: number; text: string }[] }).__w15Frames.push({
            at: performance.now(),
            text: decoder.decode(value, { stream: true }),
          });
        }
      }
    })();
  }, `${serveOrigin}${API}/documents/${did}/events/stream?run_id=${eventPage.run_id}&after_seq=0`);

  await page.locator('[data-od-id="retranslate-profile"]').selectOption('w15-candidate');
  await page.locator('[data-od-id="retranslate-start"]').click();
  const retranslateJob = await latestJob(request);
  const postAtInPage = await page.evaluate(() => performance.now());

  const framesOf = async () =>
    await page.evaluate(() => {
      const chunks =
        (window as unknown as { __w15Frames?: { at: number; text: string }[] }).__w15Frames ?? [];
      return chunks.flatMap((chunk) =>
        chunk.text
          .split('\n\n')
          .filter((text) => text.trim() !== '')
          .map((text) => ({ at: chunk.at, text })),
      );
    });
  await expect
    .poll(async () => (await framesOf()).filter((f) => f.text.includes('event: job_update')).length, {
      timeout: 3_000,
      message: `job_update 应在 ${FALLBACK_POLL_MS}ms 的兜底轮询之前推来`,
    })
    .toBeGreaterThan(0);
  const jobFrames = (await framesOf()).filter((f) =>
    f.text.includes(`id: ${retranslateJob.job_id}:`),
  );
  expect(jobFrames.length, 'job_update 的 id 是 <job_id>:<n> 命名空间').toBeGreaterThan(0);
  const firstDelayMs = Math.round(jobFrames[0].at - postAtInPage);
  expect(firstDelayMs, '首帧不许慢于兜底轮询').toBeLessThan(FALLBACK_POLL_MS);
  const firstData = /data: (\{.*\})/.exec(jobFrames[0].text)?.[1] ?? '{}';
  expect(Object.keys(JSON.parse(firstData).data).sort()).toEqual([
    'action',
    'error_code',
    'from_stage',
    'job_id',
    'status',
  ]);
  console.log(
    `[w15] SSE：POST 返回后 ${firstDelayMs}ms 收到 job_update（兜底轮询 ${FALLBACK_POLL_MS}ms）`,
  );

  // 真 job 成功：候选出现（页面不重新加载）
  const candidateCard = page.locator('[data-od-id="candidate-card"]').first();
  await expect(candidateCard).toHaveAttribute('data-ready', 'true', { timeout: 90_000 });
  await expect(candidateCard).toContainText(CANDIDATE);
  await expect
    .poll(async () => (await latestJob(request)).status, {
      timeout: 30_000,
      message: 'retranslate job 应 succeeded',
    })
    .toBe('succeeded');
  await shot(page, 'w15-07-candidate');

  // ========================================================================= #
  // 阶段 4：手改译文 + bbox（拖拽 + 数值）→ 草稿修订前进
  // ========================================================================= #
  await page.locator('[data-od-id="candidate-adopt"]').click();
  await expect
    .poll(
      async () =>
        (
          await apiJson<{ revision: number; paragraphs: Record<string, { target?: string }> }>(
            request,
            `/documents/${did}/draft`,
          )
        ).revision,
      { timeout: 20_000, message: '采用候选应写进草稿（revision +1）' },
    )
    .toBeGreaterThan(0);
  const adopted = await apiJson<{ revision: number; paragraphs: Record<string, { target?: string }> }>(
    request,
    `/documents/${did}/draft`,
  );
  expect(adopted.paragraphs[PID]?.target).toBe(CANDIDATE);
  await waitIdle(request);

  // 手改译文（失焦即存）——每次写入都可能撞上"上一次改动的防抖编译"，用 editDraft 重试
  const edited = `W15 手改译文 ${STAMP}`;
  const textarea = page.locator('[data-od-id="paragraph-target"]');
  await editDraft(request, adopted.revision + 1, async () => {
    await textarea.fill(edited);
    await textarea.blur();
  });
  expect(
    (
      await apiJson<{ paragraphs: Record<string, { target?: string }> }>(
        request,
        `/documents/${did}/draft`,
      )
    ).paragraphs[PID]?.target,
  ).toBe(edited);

  // bbox 拖拽（se 手柄）：屏幕 → PDF 逆变换由前端做（`screenToPdfBox`）
  const geometry = await apiJson<{ paragraphs: { id: string; layout_box: number[] }[] }>(
    request,
    `/documents/${did}/geometry?kind=layout&page=1`,
  );
  const baselineBox = geometry.paragraphs.find((row) => row.id === PID)?.layout_box;
  expect(baselineBox, '第 1 页应有该段的 layout_box').toBeTruthy();
  const scaleText = (await page.locator('[data-od-id="preview-zoom"]').textContent()) ?? '';
  const scale = Number(/([0-9.]+)×/.exec(scaleText)?.[1] ?? '1');
  expect(scale).toBeGreaterThan(0);
  const dx = 36;
  const dy = 20;
  await editDraft(request, adopted.revision + 2, async () => {
    const handle = page.locator('[data-od-id="bbox-handle-se"]');
    const handleBox = await handle.boundingBox();
    expect(handleBox, 'se 手柄应可见（选中段的编辑层）').toBeTruthy();
    const startX = (handleBox?.x ?? 0) + (handleBox?.width ?? 0) / 2;
    const startY = (handleBox?.y ?? 0) + (handleBox?.height ?? 0) / 2;
    await page.mouse.move(startX, startY);
    await page.mouse.down();
    await page.mouse.move(startX + dx / 2, startY + dy / 2, { steps: 3 });
    await page.mouse.move(startX + dx, startY + dy, { steps: 3 });
    await page.mouse.up();
  });
  const dragged = await apiJson<{
    revision: number;
    paragraphs: Record<string, { target?: string; layout?: { box?: number[] } }>;
  }>(request, `/documents/${did}/draft`);
  const newBox = dragged.paragraphs[PID]?.layout?.box as number[] | undefined;
  expect(newBox, '草稿里应有拖拽后的 layout.box').toBeTruthy();
  // 右下角往右下拖 = x2 变大、y（PDF 底部）变小，量级 = 位移 / scale
  expect((newBox?.[2] ?? 0) - (baselineBox?.[2] ?? 0)).toBeCloseTo(dx / scale, 0);
  expect((newBox?.[1] ?? 0) - (baselineBox?.[1] ?? 0)).toBeCloseTo(-dy / scale, 0);
  expect(newBox?.[2]).toBeGreaterThan(newBox?.[0] ?? 0);
  expect(dragged.paragraphs[PID]?.target, '拖拽不能弄丢译文').toBe(edited);
  await shot(page, 'w15-08-edited-dragged');

  // bbox 数值调整（box_scale）也写进同一份草稿
  const boxScale = page.locator('[data-od-id="paragraph-layout-box_scale"]');
  await editDraft(request, adopted.revision + 3, async () => {
    await boxScale.fill('1.25');
    await boxScale.blur();
  });
  const numeric = await apiJson<{
    revision: number;
    paragraphs: Record<string, { layout?: { box_scale?: number } }>;
  }>(request, `/documents/${did}/draft`);
  expect(numeric.paragraphs[PID]?.layout?.box_scale).toBeCloseTo(1.25, 5);
  const draftRevision = numeric.revision;
  await shot(page, 'w15-09-box-numeric');
  console.log(`[w15] 编辑链：采用候选 → 手改译文 → 拖 bbox → 数值调整，草稿 r${draftRevision}`);

  // ========================================================================= #
  // 阶段 5：编译链路（真 job 如实失败）+ 归档/下载（§3.7 落盘夹具，裁决 (B)）
  // ========================================================================= #
  await expect
    .poll(async () => (await jobs(request)).filter((job) => job.action === 'compile').length, {
      timeout: 30_000,
      message: '草稿有改动 → 服务端 1.5s 防抖应提交一次真 compile job',
    })
    .toBeGreaterThan(0);
  await waitIdle(request);
  const compileJob = (await jobs(request)).find((job) => job.action === 'compile');
  expect(compileJob?.status, '本 spec 不跑真 build：编译应如实失败').toBe('failed');
  expect(compileJob?.error_code).toBeTruthy();
  await expect(page.locator('[data-od-id="compile-bar"]')).toHaveAttribute(
    'data-compile-status',
    'failed',
    { timeout: 30_000 },
  );
  await expect(page.locator('[data-od-id="compile-error"]')).toContainText(
    compileJob?.error_code ?? '',
  );
  // 失败不留隔离副本、不写版本归档、不动已发布产物
  expect(readdirSync(join(workdirOf(did), '.bdt-serve')).filter((name) => name.startsWith('compile-'))).toEqual([]);
  expect(existsSync(join(workdirOf(did), '.bdt-serve', 'versions.json'))).toBe(false);
  expect(sha256File(join(workdirOf(did), 'output', 'paper.mono.pdf'))).toBe(beforeRunPdf);
  // 手动编译入口也是真 job（再提交一次，同样如实失败）
  await page.locator('[data-od-id="compile-retry"]').click();
  await expect
    .poll(async () => (await jobs(request)).filter((job) => job.action === 'compile').length, {
      timeout: 30_000,
      message: '「重试编译」应再提交一条真 compile job',
    })
    .toBeGreaterThan(1);
  await waitIdle(request);
  await shot(page, 'w15-10-compile-failed');
  console.log(`[w15] 编译：真 job ${compileJob?.job_id} → failed/${compileJob?.error_code}（隔离目录已清、无版本归档、产物未动）`);

  // ---- 归档/下载夹具：§3.7 落盘形状（编译成功 r{N} + 上一版 r{N-1}）---------
  writeArchiveFixture(did, draftRevision);
  await page.reload();
  await expect(page.locator('[data-od-id="compile-bar"]')).toHaveAttribute(
    'data-compile-status',
    'ok',
    { timeout: 30_000 },
  );
  await expect(page.locator('[data-od-id="compile-bar"]')).toContainText(`已更新到 r${draftRevision}`);
  const downloadLink = page.locator('[data-od-id="download-button"]');
  await expect(downloadLink).toHaveAttribute('data-enabled', 'true');
  await expect(downloadLink).toHaveAttribute('download', `paper.mono.r${draftRevision}.pdf`);
  await expect(downloadLink).toHaveAttribute('href', new RegExp(`r=${draftRevision}$`));
  await expect(page.locator('[data-od-id="compile-revision-badge"]')).toContainText(
    `最新 · r${draftRevision}`,
  );
  await expect(page.locator('[data-od-id="quality-badge"]')).toContainText('检查通过');
  // 预览按新修订重新取字节（同名产物原地替换，没有 `?r=` 就会显示旧 PDF）
  await expect
    .poll(
      () => artifactRequests.some((url) => url.includes('/artifacts/output/') && url.includes(`r=${draftRevision}`)),
      { timeout: 30_000, message: '刷新后预览应按 ?r=<revision> 重新取产物' },
    )
    .toBe(true);
  await expect(page.locator('[data-od-id="preview-canvas"] canvas')).toBeVisible({ timeout: 30_000 });
  await shot(page, 'w15-11-compiled-ok');

  // ---- 下载：字节必须等于磁盘上那一版（浏览器真下载）------------------------
  const [mainDownload] = await Promise.all([
    page.waitForEvent('download'),
    downloadLink.click(),
  ]);
  expect(mainDownload.suggestedFilename()).toBe(`paper.mono.r${draftRevision}.pdf`);
  const mainPath = await mainDownload.path();
  expect(mainPath).toBeTruthy();
  const downloadedMain = readFileSync(mainPath as string);
  expect(sha256Bytes(downloadedMain), '下载的最新版必须与磁盘 output/ 的字节一致').toBe(
    sha256File(join(workdirOf(did), 'output', 'paper.mono.pdf')),
  );
  expect(downloadedMain.length).toBe(MONO_BYTES.length);

  // ---- 归档视图：当前版本高亮 + 历史版本可下载（字节同样是历史那一版）------
  const versions = await apiJson<{
    current_revision: number;
    stale: boolean;
    items: { revision: number; trigger: string }[];
  }>(request, `/documents/${did}/versions`);
  expect(versions.current_revision).toBe(draftRevision);
  expect(versions.stale).toBe(false);
  expect(versions.items.map((row) => row.revision)).toEqual([draftRevision, draftRevision - 1]);

  await page.locator('[data-od-id="download-history"]').click();
  await expect(page.locator('[data-od-id="archive-panel"]')).toBeVisible({ timeout: 30_000 });
  const rows = page.locator('[data-od-id="archive-row"]');
  await expect(rows).toHaveCount(2);
  await expect(rows.nth(0)).toHaveAttribute('data-revision', String(draftRevision));
  await expect(rows.nth(0)).toHaveAttribute('data-current', 'true');
  await expect(rows.nth(0).locator('[data-od-id="archive-row-quality"]')).toHaveText('检查通过');
  await expect(rows.nth(1)).toHaveAttribute('data-revision', String(draftRevision - 1));
  await expect(rows.nth(1).locator('[data-od-id="archive-row-quality"]')).toHaveText('检查未通过');
  await shot(page, 'w15-12-archive');

  const [historyDownload] = await Promise.all([
    page.waitForEvent('download'),
    rows.nth(1).locator('[data-od-id="archive-row-download"]').click(),
  ]);
  expect(historyDownload.suggestedFilename()).toBe(`paper.mono.r${draftRevision - 1}.pdf`);
  const historyPath = await historyDownload.path();
  const downloadedHistory = readFileSync(historyPath as string);
  expect(sha256Bytes(downloadedHistory), '历史版本下载必须是当时那一版字节').toBe(
    sha256File(join(workdirOf(did), '.bdt-serve', 'versions', `${draftRevision - 1}.pdf`)),
  );
  expect(downloadedHistory.equals(MONO_BYTES), '历史版本不能是最新那版字节').toBe(false);
  console.log(
    `[w15] 下载：r${draftRevision}=${sha256Bytes(downloadedMain).slice(0, 12)}…、r${draftRevision - 1}=${sha256Bytes(downloadedHistory).slice(0, 12)}…（均与磁盘归档字节一致）`,
  );

  // ---- 收尾：页面不许有未捕获异常；404 只允许「还没跑过那个阶段」的那几个 ----------
  expect(pageErrors, '页面不应有未捕获异常').toEqual([]);
  expect(
    [...notFoundPaths].every((path) => /(events|stage-state|geometry|paragraphs|versions)$/.test(path)),
    `非预期 404：${[...notFoundPaths].join(', ')}`,
  ).toBe(true);
});
