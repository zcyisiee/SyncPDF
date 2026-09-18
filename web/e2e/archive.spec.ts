/**
 * W12 真用例：版本归档视图（`#/d/:did/archive`）—— 列表渲染 + 当前版本高亮 + 任一版本下载。
 *
 * **不跑真编译**（真 build 约 3 分钟）：fixture 直接按 §3.7 的落盘形状造出「已经编译过两次」
 * 的 workdir（`versions.json` + `versions/<r>.pdf` + `compile.json`），断言前端与端点的读取
 * 语义。归档写入（发布成功即归档、50 上限、失败/取消不归档）由 `tests/test_serve_versions.py`
 * 与 `tests/test_serve_compile.py` 用 stub 编译路径覆盖（见 W12 报告）。
 *
 * fixture（都在仓库 `tmp/` 下，不入库）：
 * - `tmp/w12-versions-<时间戳>/`：两版（r1 手动/质量未过、r2 防抖/质量通过），`compile.json`
 *   的当前 revision = 2，`draft.json` revision = 3 → stale（草稿有未编译修改）；
 * - `tmp/w12-empty-<时间戳>/`：只有产物、没有归档 → 空态引导。
 */
import { expect, test } from '@playwright/test';
import type { APIRequestContext } from '@playwright/test';
import { copyFileSync, existsSync, mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const WEB_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const REPO_ROOT = resolve(WEB_ROOT, '..');
const SERVE_ROOT = join(REPO_ROOT, 'tmp');
const SHOT_DIR = join(WEB_ROOT, 'tmp-smoke');
const PDF_FIXTURE = join(WEB_ROOT, 'e2e', 'fixtures', 'sample.pdf');
const API = '/api/v1';

const STAMP = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 14);
const DID = `w12-versions-${STAMP}`;
const EMPTY_DID = `w12-empty-${STAMP}`;
const WORKDIR = join(SERVE_ROOT, DID);
const EMPTY_WORKDIR = join(SERVE_ROOT, EMPTY_DID);
/** 两版的字节（第一版用真最小 PDF，第二版在尾部多一段注释 → sha 必然不同）。 */
const V1_BYTES = readFileSync(PDF_FIXTURE);
const V2_BYTES = Buffer.concat([V1_BYTES, Buffer.from('\n% W12 e2e version 2\n')]);

interface VersionRow {
  revision: number;
  trigger: string;
  artifact_name: string;
  bytes: number;
  sha256_head: string;
  quality: { check_verdict: string; pipeline_ok: boolean };
}

interface VersionsBody {
  did: string;
  current_revision: number;
  stale: boolean;
  items: VersionRow[];
}

async function apiJson<T>(request: APIRequestContext, path: string): Promise<T> {
  const response = await request.get(`${API}${path}`);
  expect(response.ok(), `GET ${path} → ${response.status()}`).toBe(true);
  return (await response.json()) as T;
}

/** 造一个「已经编译过两次」的文档（落盘形状完全按 §3.7）。 */
function writeArchiveFixture() {
  mkdirSync(join(WORKDIR, 'agent'), { recursive: true });
  mkdirSync(join(WORKDIR, 'output'), { recursive: true });
  mkdirSync(join(WORKDIR, '.bdt-serve', 'versions'), { recursive: true });
  copyFileSync(PDF_FIXTURE, join(WORKDIR, 'source.pdf'));
  // 当前发布（output/ 的最新那份 = r2）
  writeFileSync(join(WORKDIR, 'output', 'paper.mono.pdf'), V2_BYTES);
  // 归档的两版（r1 = 第一版字节，r2 = 当前字节）
  writeFileSync(join(WORKDIR, '.bdt-serve', 'versions', '1.pdf'), V1_BYTES);
  writeFileSync(join(WORKDIR, '.bdt-serve', 'versions', '2.pdf'), V2_BYTES);
  writeFileSync(
    join(WORKDIR, '.bdt-serve', 'versions.json'),
    `${JSON.stringify(
      {
        version: 1,
        items: [
          {
            revision: 1,
            created_at: '2026-09-17T10:00:00.000Z',
            trigger: 'manual',
            artifact_name: 'paper.mono.pdf',
            bytes: V1_BYTES.length,
            sha256_head: 'a'.repeat(64),
            quality: { check_verdict: 'needs_fix', pipeline_ok: false },
          },
          {
            revision: 2,
            created_at: '2026-09-17T10:05:00.000Z',
            trigger: 'debounce',
            artifact_name: 'paper.mono.pdf',
            bytes: V2_BYTES.length,
            sha256_head: 'b'.repeat(64),
            quality: { check_verdict: 'pass', pipeline_ok: true },
          },
        ],
      },
      null,
      2,
    )}\n`,
    'utf-8',
  );
  // 当前编译状态（§3.2）：r2 是最近一次成功发布
  writeFileSync(
    join(WORKDIR, '.bdt-serve', 'compile.json'),
    `${JSON.stringify(
      {
        status: 'ok',
        revision: 2,
        artifact: { name: 'paper.mono.pdf', size: V2_BYTES.length, revision: 2 },
      },
      null,
      2,
    )}\n`,
    'utf-8',
  );
  // 草稿比已编译的那一版新 → stale（归档视图必须显式提示，不得把旧版本说成最新）
  writeFileSync(
    join(WORKDIR, '.bdt-serve', 'draft.json'),
    `${JSON.stringify({ revision: 3, updated_at: '2026-09-17T10:06:00.000Z', paragraphs: {} })}\n`,
    'utf-8',
  );
}

/** 造一个从没编译成功过的文档（空态）。 */
function writeEmptyFixture() {
  mkdirSync(join(EMPTY_WORKDIR, 'output'), { recursive: true });
  copyFileSync(PDF_FIXTURE, join(EMPTY_WORKDIR, 'source.pdf'));
  copyFileSync(PDF_FIXTURE, join(EMPTY_WORKDIR, 'output', 'paper.mono.pdf'));
}

test.beforeAll(() => {
  mkdirSync(SHOT_DIR, { recursive: true });
  expect(existsSync(PDF_FIXTURE), 'fixture PDF 缺失：web/e2e/fixtures/sample.pdf').toBe(true);
  writeArchiveFixture();
  writeEmptyFixture();
});

test('归档视图：列表（新 → 旧）+ 当前版本高亮 + stale 提示 + 任一版本下载', async ({
  page,
  request,
}) => {
  test.setTimeout(120_000);
  const pageErrors: string[] = [];
  page.on('pageerror', (error) => pageErrors.push(String(error)));

  // ---- 1) 端点事实：清单新 → 旧、当前 revision=2、stale=true ---------------------
  const body = await apiJson<VersionsBody>(request, `/documents/${DID}/versions`);
  expect(body.did).toBe(DID);
  expect(body.current_revision).toBe(2);
  expect(body.stale).toBe(true);
  expect(body.items.map((row) => row.revision)).toEqual([2, 1]);
  expect(body.items[0].trigger).toBe('debounce');
  expect(body.items[1].trigger).toBe('manual');
  expect(body.items[1].quality.pipeline_ok).toBe(false); // 质量只记录不门禁

  // ---- 2) 版本文件不在产物白名单里（防目录混入）----------------------------------
  const artifacts = await apiJson<{ name: string }[]>(request, `/documents/${DID}/artifacts`);
  expect(artifacts.some((item) => item.name.includes('versions'))).toBe(false);

  // ---- 3) 归档视图渲染 ----------------------------------------------------------
  await page.goto(`/#/d/${DID}/archive`);
  await expect(page.locator('[data-od-id="archive-panel"]')).toBeVisible({ timeout: 30_000 });
  await expect(page.locator('[data-od-id="archive-count"]')).toHaveText('共 2 个版本');

  const rows = page.locator('[data-od-id="archive-row"]');
  await expect(rows).toHaveCount(2);
  const first = rows.nth(0);
  const second = rows.nth(1);
  // 新 → 旧：第一行是 r2（当前版本，绿色「当前版本」标记），第二行是 r1
  await expect(first).toHaveAttribute('data-revision', '2');
  await expect(first).toHaveAttribute('data-current', 'true');
  await expect(first.locator('[data-od-id="archive-row-current"]')).toHaveText('当前版本');
  await expect(first.locator('[data-od-id="archive-row-trigger"]')).toHaveText('自动');
  await expect(first.locator('[data-od-id="archive-row-quality"]')).toHaveText('检查通过');
  await expect(second).toHaveAttribute('data-revision', '1');
  await expect(second).toHaveAttribute('data-current', 'false');
  await expect(second.locator('[data-od-id="archive-row-trigger"]')).toHaveText('手动');
  // 质量未过的版本**仍然可下载**（黄标，不禁用）
  await expect(second.locator('[data-od-id="archive-row-quality"]')).toHaveText('检查未通过');
  const download = second.locator('[data-od-id="archive-row-download"]');
  await expect(download).toHaveAttribute('href', `${API}/documents/${DID}/versions/1/pdf`);
  await expect(download).toHaveAttribute('download', 'paper.mono.r1.pdf');

  // ---- 4) stale 提示条（草稿有未编译修改）----------------------------------------
  await expect(page.locator('[data-od-id="archive-stale"]')).toBeVisible();
  await expect(page.locator('[data-od-id="archive-stale"]')).toContainText('草稿有未编译修改');

  // ---- 5) 右侧面板的归档摘要在前（默认 tab）+ 「查看全部」------------------------
  await expect(page.locator('[data-od-id="archive-summary"]')).toBeVisible();
  await expect(page.locator('[data-od-id="archive-summary-count"]')).toHaveText('2 个版本');
  await expect(page.locator('[data-od-id="archive-summary-all"] a')).toHaveAttribute(
    'href',
    `#/d/${DID}/archive`,
  );
  await page.screenshot({ path: join(SHOT_DIR, 'e2e-w12-archive.png') });

  // ---- 6) 下载 r1：字节就是当时那一版 --------------------------------------------
  const pdf = await request.get(`${API}/documents/${DID}/versions/1/pdf`);
  expect(pdf.status()).toBe(200);
  expect(pdf.headers()['content-type']).toBe('application/pdf');
  expect(pdf.headers()['content-disposition']).toBe('inline; filename="paper.mono.r1.pdf"');
  expect(Buffer.from(await pdf.body()).equals(V1_BYTES)).toBe(true);
  // 与当前发布（r2）不同：历史版本拿到的是历史字节，不是 output/ 里那份
  expect(Buffer.from(V1_BYTES).equals(V2_BYTES)).toBe(false);

  // 非数字 / 不在清单 → 404 version_not_found（不泄露存在性）
  for (const bad of ['abc', '99']) {
    const missing = await request.get(`${API}/documents/${DID}/versions/${bad}/pdf`);
    expect(missing.status(), `versions/${bad}/pdf`).toBe(404);
    expect((await missing.json()).error.code).toBe('version_not_found');
  }

  // ---- 7) 空态（从没编译成功过）不报错，给引导 -----------------------------------
  await page.goto(`/#/d/${EMPTY_DID}/archive`);
  await expect(page.locator('[data-od-id="archive-empty"]')).toBeVisible({ timeout: 30_000 });
  await expect(page.locator('[data-od-id="archive-empty"]')).toContainText('还没有版本归档');
  await expect(page.locator('[data-od-id="archive-empty-cta"] a')).toHaveAttribute(
    'href',
    `#/d/${EMPTY_DID}/translate`,
  );
  await page.screenshot({ path: join(SHOT_DIR, 'e2e-w12-empty.png') });

  expect(pageErrors, '页面不应有未捕获异常').toEqual([]);
});
