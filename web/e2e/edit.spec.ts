/**
 * W10 真用例：点段改译文 → 保存 → **真编译**（`bdt run --from apply`，实测 ~180s）→
 * 修订号/ stale 断言 → 拖 bbox 手柄 → 草稿 `layout.box` 更新断言。
 *
 * fixture 与副作用：
 * - 只读基线 `tmp/e2e-2602-02908v2-20260917`（8.3G，65MB mono 级产物 + 完整 apply/build 链路）；
 * - setup 里用 `cp -Rc`（clonefile，秒级、块级共享）克隆出一份**可写副本**
 *   `tmp/w10-edit-<时间戳>/` —— serve root 仍是仓库 `tmp/`（既有 3 个 spec + W08 上传都靠它），
 *   克隆只为了不在基线里写 `draft.json` / `compile.json` / 新 PDF；基线本身零写入；
 * - 编译产物与草稿都留在副本里（证据，可手工删）；`afterAll` 取消活动 job，不留子进程。
 *
 * 真编译只跑**一次**（改译文 → 保存 → 等 ok → revision/stale 断言）：第二次编译（拖完 bbox
 * 之后服务端防抖触发）只断言到“进 running + 编辑禁用”，随即取消——不让 e2e 跑十分钟。
 * stale / 未选中段时的编译入口 / 失败态的 UI 分支由 `tests/compile-bar.test.tsx` 用替身数据覆盖。
 */
import { expect, test } from '@playwright/test';
import type { APIRequestContext } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync } from 'node:fs';
import { dirname, join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

const WEB_ROOT = resolve(dirname(fileURLToPath(import.meta.url)), '..');
const REPO_ROOT = resolve(WEB_ROOT, '..');
const SERVE_ROOT = join(REPO_ROOT, 'tmp');
const FIXTURE = join(SERVE_ROOT, 'e2e-2602-02908v2-20260917');
const SHOT_DIR = join(WEB_ROOT, 'tmp-smoke');
const API = '/api/v1';

const STAMP = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 14);
/** 每次跑一个新 did：job 历史按 did 存在 `tmp/.bdt-serve/jobs/`，固定 did 会看到上一次的记录。 */
const COPY_DID = `w10-edit-${STAMP}`;
const COPY_DIR = join(SERVE_ROOT, COPY_DID);

/** 是否已完成克隆（fixture 缺失时用例 skip）。 */
let cloned = false;

async function apiJson<T>(request: APIRequestContext, path: string): Promise<T> {
  const response = await request.get(`${API}${path}`);
  expect(response.ok(), `GET ${path} → ${response.status()}`).toBe(true);
  return (await response.json()) as T;
}

interface DraftBody {
  revision: number;
  paragraphs: Record<string, { target?: string | null; layout?: Record<string, unknown> | null }>;
}

interface DetailBody {
  compile: {
    status: string;
    revision: number;
    stale: boolean;
    artifact: { name: string; revision: number } | null;
  };
  quality: { check: { verdict: string }; pipeline_ok: boolean };
}

async function cancelActiveJobs(request: APIRequestContext, did: string) {
  const response = await request.get(`${API}/documents/${did}/jobs`);
  if (!response.ok()) return;
  const jobs = (await response.json()) as { job_id: string; status: string }[];
  for (const job of jobs) {
    if (job.status === 'queued' || job.status === 'running') {
      await request.post(`${API}/jobs/${job.job_id}/cancel`);
    }
  }
}

test.beforeAll(() => {
  mkdirSync(SHOT_DIR, { recursive: true });
  if (!existsSync(FIXTURE)) return;
  // clonefile：秒级复制 8.3G 基线，块级共享（改副本不影响基线）
  execFileSync('cp', ['-Rc', FIXTURE, COPY_DIR], { timeout: 120_000 });
  cloned = true;
});

test.afterAll(async ({ request }) => {
  if (!cloned) return;
  await cancelActiveJobs(request, COPY_DID);
});

test('改译文 → 真编译 → 拖 bbox 更新草稿（真 serve + 真产物）', async ({ page, request }) => {
  test.skip(!cloned, `fixture ${FIXTURE} 不在本机 tmp/ 里（先按 web/README.md 准备），跳过`);
  test.setTimeout(300_000);

  const pageErrors: string[] = [];
  page.on('pageerror', (error) => pageErrors.push(String(error)));
  /** 产物请求 URL（断言「编译后预览真的重新取字节」用）。 */
  const artifactRequests: string[] = [];
  page.on('request', (outgoing) => {
    if (outgoing.url().includes('/artifacts/')) artifactRequests.push(outgoing.url());
  });

  // ---- 0) 前置事实：副本有可编译的产物 + 没有编译修订（compile.json 不存在）--------
  const artifacts = await apiJson<{ name: string; kind: string }[]>(
    request,
    `/documents/${COPY_DID}/artifacts`,
  );
  const mono = artifacts.find((item) => item.kind === 'pdf' && item.name.endsWith('.mono.pdf'));
  expect(mono, '副本里应有 mono PDF（apply/build 链路完整的 fixture）').toBeTruthy();

  const before = await apiJson<DetailBody>(request, `/documents/${COPY_DID}`);
  expect(before.compile.revision, '副本不该带着上一次跑的编译修订').toBe(0);
  expect(before.compile.artifact).toBeNull();
  const draftBefore = await apiJson<DraftBody>(request, `/documents/${COPY_DID}/draft`);
  expect(draftBefore.revision).toBe(0);

  // ---- 1) 翻译视图：点段选中 ------------------------------------------------------
  const geometry = await apiJson<{
    paragraphs: { id: string; page: number; layout_box: number[] }[];
  }>(request, `/documents/${COPY_DID}/geometry?kind=layout&page=1`);
  const target = geometry.paragraphs.find((row) => Array.isArray(row.layout_box));
  expect(target, '第 1 页应有带 layout_box 的段落').toBeTruthy();
  const targetId = target?.id as string;
  const baselineBox = target?.layout_box as number[];

  // 译文框图层（拖拽编辑层只在 pdf_native 几何上出现）；旧链接 /translate 合并后仍解析
  await page.addInitScript(() => localStorage.setItem('ieet.bboxMode', 'layout'));
  await page.goto(`/#/d/${COPY_DID}/translate`);
  await expect(page.locator('[data-od-id="preview-canvas"] canvas')).toBeVisible({ timeout: 30_000 });
  await expect(page.locator(`[data-bbox-id="${targetId}"]`)).toBeVisible({ timeout: 30_000 });
  await page.locator(`[data-bbox-id="${targetId}"]`).click();

  await expect(page.locator('[data-od-id="selected-paragraph-id"]')).toHaveText(targetId);
  await expect(page.locator('[data-od-id="paragraph-editor"]')).toBeVisible();
  // 编辑层手柄只对选中段出现（8 个）——顺带证明 layout 图层与逆变换接线就绪
  await expect(page.locator('[data-od-id^="bbox-handle-"]')).toHaveCount(8);
  await page.screenshot({ path: join(SHOT_DIR, 'e2e-edit-selected.png') });

  // 未编译时下载按钮必须是禁用的（不许把 fixture 里 `bdt run` 的旧 PDF 冒充下载产物）。
  // 下载槽已收口到右栏归档 tab（工具条上不再有）：切过去看那个按钮的状态。
  await page.locator('[data-od-id="inspector-tab-archive"]').click();
  await expect(page.locator('[data-od-id="archive-tab"]')).toBeVisible({ timeout: 30_000 });
  await expect(page.locator('[data-od-id="download-button"]')).toHaveAttribute(
    'data-enabled',
    'false',
  );
  // 回到段落 tab（后面要编辑该段译文）
  await page.locator('[data-od-id="inspector-tab-paragraph"]').click();
  await expect(page.locator('[data-od-id="paragraph-editor"]')).toBeVisible();

  // ---- 2) 改译文 → 保存（Cmd+S 立即存）--------------------------------------------
  const paragraphs = await apiJson<{ id: string; target: string | null }[]>(
    request,
    `/documents/${COPY_DID}/paragraphs`,
  );
  const baselineTarget = paragraphs.find((row) => row.id === targetId)?.target ?? '';
  const textarea = page.locator('[data-od-id="paragraph-target"]');
  await expect(textarea).toHaveValue(baselineTarget, { timeout: 20_000 });

  const edited = `E2E 手改译文 ${STAMP}`;
  await textarea.fill(edited);
  // 失焦存（等价于 Cmd+S 的手动存；不用快捷键是为了绕开浏览器自己的「保存网页」）
  await textarea.blur();

  await expect
    .poll(async () => (await apiJson<DraftBody>(request, `/documents/${COPY_DID}/draft`)).revision, {
      timeout: 20_000,
      message: 'PATCH 草稿后 revision 应 +1',
    })
    .toBe(1);
  const draftSaved = await apiJson<DraftBody>(request, `/documents/${COPY_DID}/draft`);
  expect(draftSaved.paragraphs[targetId]?.target).toBe(edited);
  // UI 侧：草稿已修改 chip + 修订号 + 内容回显
  await expect(page.locator('[data-od-id="paragraph-editor-modified"]')).toContainText('草稿已修改');
  await expect(page.locator('[data-od-id="draft-revision"]')).toContainText('草稿 r1');
  await page.screenshot({ path: join(SHOT_DIR, 'e2e-edit-saved.png') });

  // ---- 3) 自动编译：进 running（编辑禁用）-----------------------------------------
  await expect(page.locator('[data-od-id="compile-bar"]')).toHaveAttribute(
    'data-compile-status',
    'running',
    { timeout: 60_000 },
  );
  await expect(page.locator('[data-od-id="compile-bar"]')).toContainText('编辑已禁用');
  await expect(textarea).toHaveAttribute('readonly', '');
  await expect(page.locator('[data-od-id="editor-locked"]')).toBeVisible();
  await page.screenshot({ path: join(SHOT_DIR, 'e2e-edit-compiling.png') });

  // ---- 4) 等真编译成功（实测 ~180s）-----------------------------------------------
  await expect
    .poll(async () => (await apiJson<DetailBody>(request, `/documents/${COPY_DID}`)).compile.status, {
      timeout: 240_000,
      intervals: [3_000],
      message: '真 compile（apply+build）应在 4 分钟内落 ok',
    })
    .toBe('ok');

  const detail = await apiJson<DetailBody>(request, `/documents/${COPY_DID}`);
  expect(detail.compile.revision, 'compile.revision = 捕获的草稿 revision').toBe(1);
  expect(detail.compile.stale, '刚编译完 → 草稿不比 PDF 新').toBe(false);
  expect(detail.compile.artifact?.name).toContain('.mono.pdf');
  expect(detail.compile.artifact?.revision).toBe(1);

  // 服务端产物：发布回真 workdir，且清单里有它
  const published = await apiJson<{ name: string }[]>(request, `/documents/${COPY_DID}/artifacts`);
  expect(published.some((item) => item.name === `output/${detail.compile.artifact?.name}`)).toBe(true);

  // UI 侧：状态条 → ok（ok 且不 stale 时不渲染 compile-bar 本身，修订号在下载徽标上）；
  // 下载按钮启用且文件名带 r1；预览按新修订重新取字节
  await page.locator('[data-od-id="inspector-tab-archive"]').click();
  await expect(page.locator('[data-od-id="archive-tab"]')).toBeVisible({ timeout: 30_000 });
  await expect(page.locator('[data-od-id="compile-revision-badge"]')).toContainText('最新 · r1', {
    timeout: 30_000,
  });
  const download = page.locator('[data-od-id="download-button"]');
  await expect(download).toHaveAttribute('data-enabled', 'true');
  await expect(download).toHaveAttribute('download', /\.r1\.pdf$/);
  await expect(download).toHaveAttribute('href', /\/artifacts\/output\/.+\.pdf\?r=1$/);
  // 质量徽标不许冒充通过：pipeline_ok=true 才绿；本 fixture 的门禁结论按服务端为准
  const qualityBadge = page.locator('[data-od-id="quality-badge"]');
  if (detail.quality.pipeline_ok) {
    await expect(qualityBadge).toContainText('检查通过');
  } else {
    await expect(qualityBadge).not.toContainText('检查通过');
  }
  // 预览重新取字节：URL 带了新修订号（否则 pdf.js 还显示旧 PDF）。
  // 只有当**预览的那个 PDF 就是本次编译发布的产物**时才可能带 `?r=`（同名才复用 URL）。
  if (mono?.name === `output/${detail.compile.artifact?.name}`) {
    await expect
      .poll(
        () =>
          artifactRequests.some(
            (url) => url.includes('/artifacts/output/') && url.includes('r=1'),
          ),
        {
          timeout: 30_000,
          message: '编译完成后预览应按 ?r=1 重新拉产物',
        },
      )
      .toBe(true);
  } else {
    console.log(`[w10] 编译产物名 ${detail.compile.artifact?.name} 与预览产物 ${mono?.name} 不同名，跳过预览刷新断言`);
  }
  await page.screenshot({ path: join(SHOT_DIR, 'e2e-edit-compiled.png') });

  // ---- 5) 拖 bbox 手柄 → 草稿 layout.box 更新 -------------------------------------
  const handle = page.locator('[data-od-id="bbox-handle-se"]');
  const box = await handle.boundingBox();
  expect(box, 'se 手柄应可见（选中段的可拖拽编辑层）').toBeTruthy();
  const scaleText = (await page.locator('[data-od-id="preview-zoom"]').textContent()) ?? '';
  const scale = Number(/([0-9.]+)×/.exec(scaleText)?.[1] ?? '1');
  expect(scale).toBeGreaterThan(0);

  const startX = (box?.x ?? 0) + (box?.width ?? 0) / 2;
  const startY = (box?.y ?? 0) + (box?.height ?? 0) / 2;
  const dx = 40;
  const dy = 25;
  await page.mouse.move(startX, startY);
  await page.mouse.down();
  await page.mouse.move(startX + dx / 2, startY + dy / 2, { steps: 3 });
  await page.mouse.move(startX + dx, startY + dy, { steps: 3 });
  await page.screenshot({ path: join(SHOT_DIR, 'e2e-edit-box-dragging.png') });
  await page.mouse.up();

  await expect
    .poll(
      async () => (await apiJson<DraftBody>(request, `/documents/${COPY_DID}/draft`)).revision,
      { timeout: 20_000, message: '拖拽松手应 PATCH 草稿（revision 再来一个 +1）' },
    )
    .toBe(2);
  const dragged = await apiJson<DraftBody>(request, `/documents/${COPY_DID}/draft`);
  const newBox = dragged.paragraphs[targetId]?.layout?.box as number[] | undefined;
  expect(newBox, '草稿里应有该段的 layout.box').toBeTruthy();
  // 屏幕 → PDF 逆变换：右下角往右下拖 = x2 变大、y（PDF 底部）变小，量级 = 位移 / scale
  expect((newBox?.[2] ?? 0) - baselineBox[2]).toBeCloseTo(dx / scale, 0);
  expect((newBox?.[1] ?? 0) - baselineBox[1]).toBeCloseTo(-dy / scale, 0);
  expect(newBox?.[2]).toBeGreaterThan(newBox?.[0] ?? 0);
  expect(newBox?.[3]).toBeGreaterThan(newBox?.[1] ?? 0);
  // 译文没被这次拖拽弄丢（layout 是整对象替换，容易踩）
  expect(dragged.paragraphs[targetId]?.target).toBe(edited);

  // ---- 6) 拖拽触发的第二次编译：只断言进 running + 编辑禁用，然后取消 --------------
  await expect(page.locator('[data-od-id="compile-bar"]')).toHaveAttribute(
    'data-compile-status',
    'running',
    { timeout: 60_000 },
  );
  await expect(textarea).toHaveAttribute('readonly', '');
  await expect(page.locator('[data-od-id="compile-bar"]')).toContainText('编辑已禁用');
  await page.screenshot({ path: join(SHOT_DIR, 'e2e-edit-box-dragged.png') });
  await cancelActiveJobs(request, COPY_DID);

  expect(pageErrors, '页面不应有未捕获异常').toEqual([]);
});
