/**
 * W11 真用例：点段 → AI 重译 → 候选（生成期不改正文）→ 采用（进草稿 + 防抖编译）→ 拒绝。
 *
 * 真 `bdt serve --root tmp` + 真 job/真子进程：`POST …/paragraphs/{pid}/retranslate` 会拉起
 * 真 `bdt translate --ids P01-001`（在隔离副本里跑），translator 换成完全离线的 stub 脚本
 * （读 stdin、回显 id + 固定候选文本，见 `e2e/fixtures/candidate-translator.sh`）。
 *
 * fixture 与副作用（都在仓库 `tmp/` 下，不入库）：
 * - `tmp/w11-candidates-<时间戳>/`：本用例自建的最小 workdir（`source.pdf` + `output/*.mono.pdf`
 *   各一份 602 字节的合法 PDF 让预览能渲染、`agent/{anchors.json,translated.md,translated.jsonl,
 *   layout_geometry.json}` 让段落面板与 bbox 图层有数据）；
 * - `tmp/.bdt-serve/profiles.json`：写入两条 stub profile 前先备份，`afterAll` 还原；
 * - 采用会触发 1.5s 防抖编译：**不等真 build**（副本里没有 state.pkl，编译会很快如实失败），
 *   只断言「compile job 出现了」然后取消。
 *
 * 零副作用的证据在这一条里：生成前记下 `agent/translated.md` 与 `output/paper.mono.pdf` 的
 * sha256，生成后再比一次（必须完全一致），同时断言 `GET /paragraphs` 的 `target` 仍是基线、
 * 草稿 revision 仍是 0。
 */
import { expect, test } from '@playwright/test';
import type { APIRequestContext } from '@playwright/test';
import { createHash } from 'node:crypto';
import { copyFileSync, existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const WEB_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const REPO_ROOT = resolve(WEB_ROOT, '..');
const SERVE_ROOT = join(REPO_ROOT, 'tmp');
const STATE_DIR = join(SERVE_ROOT, '.bdt-serve');
const PROFILES_FILE = join(STATE_DIR, 'profiles.json');
const PROFILES_BACKUP = join(STATE_DIR, 'profiles.json.w11-e2e-backup');
const SHOT_DIR = join(WEB_ROOT, 'tmp-smoke');
const PDF_FIXTURE = join(WEB_ROOT, 'e2e', 'fixtures', 'sample.pdf');
const TRANSLATOR_STUB = join(WEB_ROOT, 'e2e', 'fixtures', 'candidate-translator.sh');
const API = '/api/v1';

const STAMP = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 14);
/** 每次跑一个新 did：job 历史按 did 存在 tmp/.bdt-serve/jobs/ 里，固定 did 会看到上一次的记录。 */
const DID = `w11-candidates-${STAMP}`;
const WORKDIR = join(SERVE_ROOT, DID);
const PID = 'P01-001';
const BASELINE = 'W11 e2e 基线译文';
const CANDIDATE_A = 'W11 e2e 候选译文甲（stub）';
const CANDIDATE_B = 'W11 e2e 候选译文乙（stub）';

async function apiJson<T>(request: APIRequestContext, path: string): Promise<T> {
  const response = await request.get(`${API}${path}`);
  expect(response.ok(), `GET ${path} → ${response.status()}`).toBe(true);
  return (await response.json()) as T;
}

interface CandidateBody {
  pid: string;
  items: {
    id: string;
    pid: string;
    status: string;
    candidate_target: string | null;
    source: string | null;
  }[];
}

interface DraftBody {
  revision: number;
  paragraphs: Record<string, { target?: string | null }>;
}

interface JobRow {
  job_id: string;
  action: string;
  status: string;
  error_code: string | null;
}

async function jobs(request: APIRequestContext, did = DID): Promise<JobRow[]> {
  const response = await request.get(`${API}/documents/${did}/jobs`);
  expect(response.ok(), `GET jobs → ${response.status()}`).toBe(true);
  return (await response.json()) as JobRow[];
}

async function cancelActiveJobs(request: APIRequestContext, did = DID) {
  for (const job of await jobs(request, did)) {
    if (job.status === 'queued' || job.status === 'running') {
      await request.post(`${API}/jobs/${job.job_id}/cancel`);
    }
  }
}

/** 等该文档没有活动 job（上一次生成/编译彻底落定）—— 不然下一次提交会 409。 */
async function waitIdle(request: APIRequestContext, timeoutMs = 30_000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const active = (await jobs(request)).filter(
      (job) => job.status === 'queued' || job.status === 'running',
    );
    if (active.length === 0) return;
    await new Promise((r) => setTimeout(r, 300));
  }
  throw new Error('文档一直没有空闲（还有活动 job）');
}

function sha256(path: string): string {
  return createHash('sha256').update(readFileSync(path)).digest('hex');
}

/** 最小可重译 + 可预览的 workdir（真产物形态，不是假 stub）。 */
function writeFixture() {
  mkdirSync(join(WORKDIR, 'agent'), { recursive: true });
  mkdirSync(join(WORKDIR, 'output'), { recursive: true });
  copyFileSync(PDF_FIXTURE, join(WORKDIR, 'source.pdf'));
  copyFileSync(PDF_FIXTURE, join(WORKDIR, 'output', 'paper.mono.pdf'));
  writeFileSync(
    join(WORKDIR, 'agent', 'anchors.json'),
    `${JSON.stringify(
      {
        rows: [
          {
            id: PID,
            page: 0,
            layout_label: 'text',
            canonical: 'W11 e2e source sentence.',
            markdown: 'W11 e2e source sentence.',
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
  writeFileSync(
    join(WORKDIR, 'agent', 'translated.jsonl'),
    `${JSON.stringify({ id: PID, target: BASELINE, layout_label: 'text' }, null, 0)}\n`,
    'utf-8',
  );
  // 版面几何：第 1 页一个段落框（pdf_native、y 向上），预览的 bbox 图层靠它画出可点的框。
  // 尺寸必须与 source.pdf 的 MediaBox 一致（`e2e/fixtures/sample.pdf` = 300x120pt），
  // 否则框会落在页面之外、点不到。
  writeFileSync(
    join(WORKDIR, 'agent', 'layout_geometry.json'),
    `${JSON.stringify(
      {
        pages: 1,
        page_info: [{ page: 1, cropbox: [0, 0, 300, 120] }],
        paragraphs: [
          { id: PID, page: 1, layout_label: 'text', layout_box: [20, 40, 280, 110] },
        ],
      },
      null,
      2,
    )}\n`,
    'utf-8',
  );
}

test.beforeAll(() => {
  mkdirSync(SHOT_DIR, { recursive: true });
  expect(existsSync(PDF_FIXTURE), 'fixture PDF 缺失：web/e2e/fixtures/sample.pdf').toBe(true);
  writeFixture();

  // 两条 stub profile：命令是脚本**绝对路径 + 候选正文**（子进程 cwd 是隔离副本，相对路径解析不到；
  // 正文带空格所以加引号 —— 命令字符串会被 `shlex.split` 拆成 argv，不加引号只会传进第一个词）。
  mkdirSync(STATE_DIR, { recursive: true });
  if (existsSync(PROFILES_FILE)) {
    writeFileSync(PROFILES_BACKUP, readFileSync(PROFILES_FILE));
  }
  writeFileSync(
    PROFILES_FILE,
    `${JSON.stringify(
      {
        'w11-stub-a': { label: 'W11 Stub A', translator: `${TRANSLATOR_STUB} "${CANDIDATE_A}"` },
        'w11-stub-b': { label: 'W11 Stub B', translator: `${TRANSLATOR_STUB} "${CANDIDATE_B}"` },
      },
      null,
      2,
    )}\n`,
    'utf-8',
  );
});

test.afterAll(async ({ request }) => {
  await cancelActiveJobs(request).catch(() => undefined);
  if (existsSync(PROFILES_BACKUP)) {
    writeFileSync(PROFILES_FILE, readFileSync(PROFILES_BACKUP));
    rmSync(PROFILES_BACKUP);
  } else if (existsSync(PROFILES_FILE)) {
    rmSync(PROFILES_FILE);
  }
});

test('点段 → AI 重译（零副作用）→ 采用进草稿 → 拒绝另一条', async ({ page, request }) => {
  test.setTimeout(180_000);
  const pageErrors: string[] = [];
  page.on('pageerror', (error) => pageErrors.push(String(error)));

  // ---- 0) 基线事实：译文基线 + 候选为空 + 草稿 r0 --------------------------------
  const paragraphs = await apiJson<{ id: string; target: string | null }[]>(
    request,
    `/documents/${DID}/paragraphs`,
  );
  expect(paragraphs.find((row) => row.id === PID)?.target).toBe(BASELINE);
  expect((await apiJson<CandidateBody>(request, `/documents/${DID}/paragraphs/${PID}/candidates`)).items).toEqual([]);
  expect((await apiJson<DraftBody>(request, `/documents/${DID}/draft`)).revision).toBe(0);
  const translatedBefore = sha256(join(WORKDIR, 'agent', 'translated.md'));
  const pdfBefore = sha256(join(WORKDIR, 'output', 'paper.mono.pdf'));

  // ---- 1) 翻译视图点段选中 -------------------------------------------------------
  await page.goto(`/#/d/${DID}/translate`);
  await expect(page.locator('[data-od-id="preview-canvas"] canvas')).toBeVisible({ timeout: 30_000 });
  await expect(page.locator(`[data-bbox-id="${PID}"]`)).toBeVisible({ timeout: 30_000 });
  await page.locator(`[data-bbox-id="${PID}"]`).click();
  await expect(page.locator('[data-od-id="selected-paragraph-id"]')).toHaveText(PID);
  await expect(page.locator('[data-od-id="paragraph-editor"]')).toBeVisible();
  await expect(page.locator('[data-od-id="paragraph-target"]')).toHaveValue(BASELINE, {
    timeout: 20_000,
  });
  await page.screenshot({ path: join(SHOT_DIR, 'e2e-w11-selected.png') });

  // ---- 2) AI 重译 → 候选出现（pending）-------------------------------------------
  await page.locator('[data-od-id="retranslate-profile"]').selectOption('w11-stub-a');
  await page.locator('[data-od-id="retranslate-start"]').click();
  const cardA = page.locator('[data-od-id="candidate-card"]').first();
  await expect(cardA).toHaveAttribute('data-status', 'pending', { timeout: 60_000 });
  await expect(cardA).toHaveAttribute('data-ready', 'true', { timeout: 60_000 });
  await expect(cardA).toContainText(CANDIDATE_A);
  // 三段对比：原文 / 当前译文 / 候选译文 都在
  await expect(cardA).toContainText('W11 e2e source sentence.');
  await expect(cardA).toContainText(BASELINE);
  await page.screenshot({ path: join(SHOT_DIR, 'e2e-w11-candidate.png') });

  // 生成结束（job succeeded + 候选拿到译文）
  const firstJob = (await jobs(request)).find((job) => job.action === 'retranslate');
  expect(firstJob?.status).toBe('succeeded');
  const generated = await apiJson<CandidateBody>(
    request,
    `/documents/${DID}/paragraphs/${PID}/candidates`,
  );
  const candidateA = generated.items[0];
  expect(candidateA.candidate_target).toBe(CANDIDATE_A);
  expect(candidateA.source).toBe('W11 e2e source sentence.');

  // ---- 3) 零副作用：正文/草稿/旧 PDF 都没变 --------------------------------------
  expect(sha256(join(WORKDIR, 'agent', 'translated.md')), '生成改了 translated.md').toBe(
    translatedBefore,
  );
  expect(sha256(join(WORKDIR, 'output', 'paper.mono.pdf')), '生成改了旧 PDF').toBe(pdfBefore);
  expect(existsSync(join(WORKDIR, '.bdt-serve', 'draft.json'))).toBe(false);
  const afterGenerate = await apiJson<{ id: string; target: string | null }[]>(
    request,
    `/documents/${DID}/paragraphs`,
  );
  expect(afterGenerate.find((row) => row.id === PID)?.target, '候选混进了正文').toBe(BASELINE);
  // 隔离副本用完即删（不留 candidates-<jid> 目录）
  expect(existsSync(join(WORKDIR, '.bdt-serve', `candidates-${firstJob?.job_id}`))).toBe(false);

  // ---- 4) 采用：草稿 revision+1 且 target=候选 ----------------------------------
  await page.locator('[data-od-id="candidate-adopt"]').click();
  await expect
    .poll(
      async () => (await apiJson<DraftBody>(request, `/documents/${DID}/draft`)).revision,
      { timeout: 20_000, message: '采用后草稿 revision 应 +1' },
    )
    .toBe(1);
  const adopted = await apiJson<DraftBody>(request, `/documents/${DID}/draft`);
  expect(adopted.paragraphs[PID]?.target).toBe(CANDIDATE_A);
  // UI：译文框立刻变候选文本 + 草稿已修改
  await expect(page.locator('[data-od-id="paragraph-target"]')).toHaveValue(CANDIDATE_A, {
    timeout: 20_000,
  });
  await expect(page.locator('[data-od-id="paragraph-editor-modified"]')).toContainText('草稿已修改');
  await expect(page.locator('[data-od-id="candidate-decided"]')).toContainText('已决定的候选（1）');
  // 采用也没改正文产物（只有草稿变了）
  expect(sha256(join(WORKDIR, 'agent', 'translated.md'))).toBe(translatedBefore);
  await page.screenshot({ path: join(SHOT_DIR, 'e2e-w11-adopted.png') });

  // ---- 5) 防抖编译：只断言 job 创建，然后取消（不等真 build）----------------------
  await expect
    .poll(async () => (await jobs(request)).some((job) => job.action === 'compile'), {
      timeout: 30_000,
      message: '采用候选后应触发 1.5s 防抖编译（compile job 出现）',
    })
    .toBe(true);
  await cancelActiveJobs(request);
  await waitIdle(request);

  // ---- 6) 再生成一条并拒绝（拒绝不动草稿，也不需忙守卫）-------------------------
  await page.locator('[data-od-id="retranslate-profile"]').selectOption('w11-stub-b');
  await page.locator('[data-od-id="retranslate-start"]').click();
  const cardB = page.locator('[data-od-id="candidate-card"]').first();
  await expect(cardB).toHaveAttribute('data-ready', 'true', { timeout: 60_000 });
  await expect(cardB).toContainText(CANDIDATE_B);
  await cardB.locator('[data-od-id="candidate-reject"]').click();

  await expect
    .poll(
      async () =>
        (await apiJson<CandidateBody>(request, `/documents/${DID}/paragraphs/${PID}/candidates`))
          .items.filter((item) => item.status === 'rejected').length,
      { timeout: 20_000, message: '拒绝后候选状态应落成 rejected' },
    )
    .toBe(1);
  // 拒绝不改草稿：仍是 r1 + 采用的候选文本；被拒候选折进「已决定的候选」
  const afterReject = await apiJson<DraftBody>(request, `/documents/${DID}/draft`);
  expect(afterReject.revision).toBe(1);
  expect(afterReject.paragraphs[PID]?.target).toBe(CANDIDATE_A);
  await expect(page.locator('[data-od-id="candidate-decided"]')).toContainText('已决定的候选（2）');
  await expect(page.locator('[data-od-id="candidate-decided"]')).toContainText(CANDIDATE_B);
  await page.screenshot({ path: join(SHOT_DIR, 'e2e-w11-rejected.png') });

  expect(pageErrors, '页面不应有未捕获异常').toEqual([]);
});
