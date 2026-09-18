/**
 * W06 真用例：事件流面板（真 SSE + 真分页）+ 真数据时间线（stage-state 基线）。
 *
 * fixture：`tmp/ccs3764-dyn`（同 `preview.spec.ts`；21 页、事件归档 12 条、7 段全 ok）。
 * 断言重点：
 * - 事件面板渲染出真归档里的行（≥10 行、seq 与 kind 来自服务端 events.jsonl，不是造的）；
 * - 时间线 7 段全是 `data-state=ok` + 真实耗时（stage-state 的 duration_s）+ 总用时 chip；
 * - 页面全程无 console error / pageerror（SSE 连接与 EventSource 生命周期不报错）。
 *
 * SSE **增量**（进行中的 run）不在 e2e 里做：W07 有真 job 之后由 W15 的集成验收覆盖；
 * 本任务的实时性用 `web/tmp-smoke/w06-sse-smoke.mjs` 手工模拟验证（见 web/README.md）。
 */
import { expect, test } from '@playwright/test';
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';

const DID = 'ccs3764-dyn';
const SHOT_DIR = 'tmp-smoke';

const STAGES = ['parse', 'translate', 'apply', 'build', 'check', 'review', 'report'] as const;

test.beforeAll(() => {
  mkdirSync(SHOT_DIR, { recursive: true });
});

test.beforeAll(async ({ request }) => {
  const events = await request.get(`/api/v1/documents/${DID}/events?after_seq=0&limit=2000`);
  expect(
    events.status(),
    `fixture tmp/${DID} 缺失（或没有 run 归档）：请先按 web/README.md 准备 tmp/ 下的真实 workdir`,
  ).toBe(200);
});

test('进度视图：事件面板真事件 + 时间线真阶段耗时 + 无 console error', async ({ page, request }) => {
  const consoleErrors: string[] = [];
  const pageErrors: string[] = [];
  page.on('console', (message) => {
    if (message.type() === 'error') consoleErrors.push(message.text());
  });
  page.on('pageerror', (error) => pageErrors.push(String(error)));

  // 服务端事实：本页事件条数与各阶段真实耗时（断言不靠猜）
  const page_ = await (
    await request.get(`/api/v1/documents/${DID}/events?after_seq=0&limit=2000`)
  ).json();
  const stageState = await (await request.get(`/api/v1/documents/${DID}/stage-state`)).json();
  expect(page_.events.length).toBeGreaterThanOrEqual(10);

  await page.goto(`/#/d/${DID}/progress`);
  // 事件流面板默认不挂载（默认 tab 是段落）：切过去看
  await page.getByRole('tab', { name: '事件流' }).click();
  await expect(page.locator('[data-od-id="event-stream"]')).toBeVisible();

  // 事件面板：行数 == 服务端本页条数（窗口 ≤200，本 fixture 12 条）
  const expectedRows = Math.min(page_.events.length, 200);
  await expect(page.locator('[data-od-id="event-row"]')).toHaveCount(expectedRows, {
    timeout: 20_000,
  });
  expect(expectedRows).toBeGreaterThanOrEqual(10);

  // 最新在上（§4.6）：第一行的 seq 是服务端最后一行的 seq
  const newestSeq = page_.events[page_.events.length - 1].seq as number;
  await expect(page.locator('[data-od-id="event-row"]').first()).toHaveAttribute(
    'data-seq',
    String(newestSeq),
  );
  // 行的 stage/kind 做人话映射（真事件 → 中文标签）
  await expect(page.locator('[data-od-id="event-row"]').first()).toContainText('阶段完成');
  await expect(page.locator('[data-od-id="event-stream"]')).toContainText(`run ${page_.run_id}`);

  // SSE 连接行：真连上（open → 「实时」）
  await expect(page.locator('[data-od-id="event-stream-status"]')).toHaveAttribute(
    'data-status',
    'open',
    { timeout: 20_000 },
  );
  await expect(page.locator('[data-od-id="event-stream-status"]')).toContainText('实时');

  // 展开一条：原始 JSON 里有服务端的字段
  await page.locator('[data-od-id="event-row"] button').first().click();
  await expect(page.locator('[data-od-id="event-row-json"]')).toContainText('"kind"');
  await page.locator('[data-od-id="event-row"] button').first().click();

  // 时间线：7 段全 ok + 真实耗时（stage-state 的 duration_s + formatDuration 口径）
  for (const stage of STAGES) {
    const state = stageState.stages.find((item: { stage: string }) => item.stage === stage);
    expect(state, `stage-state 缺 ${stage}`).toBeTruthy();
    expect(state.status).toBe('ok');
    await expect(page.locator(`[data-od-id="timeline-stage-${stage}"]`)).toHaveAttribute(
      'data-state',
      'ok',
    );
  }
  // 最长段 translate（248.93s → 4m 8s）与最短段 report（0s → 刚启动）
  await expect(page.locator('[data-od-id="timeline-stage-translate"]')).toContainText('4m 8s');
  await expect(page.locator('[data-od-id="timeline-stage-parse"]')).toContainText('15s');
  const total = page.locator('[data-od-id="timeline-total"]');
  await expect(total).toContainText(/总用时 \d/);
  // 总用时 == 服务端 7 段 duration_s 之和（同一口径：floor 到秒）
  const totalSeconds = stageState.stages.reduce(
    (sum: number, item: { duration_s: number | null }) => sum + (item.duration_s ?? 0),
    0,
  );
  const minutes = Math.floor(Math.floor(totalSeconds) / 60);
  await expect(total).toContainText(`${minutes}m`);

  // 段不可点击（视图合并后时间线只是状态图，不再是链接）
  await expect(page.locator('[data-od-id="timeline-stage-check"] a')).toHaveCount(0);

  await page.screenshot({ path: join(SHOT_DIR, 'e2e-progress.png'), fullPage: false });
  await page.locator('[data-od-id="event-stream"]').screenshot({
    path: join(SHOT_DIR, 'e2e-progress-events.png'),
  });
  await page.locator('[data-od-id="timeline"]').screenshot({
    path: join(SHOT_DIR, 'e2e-progress-timeline.png'),
  });

  expect(pageErrors).toEqual([]);
  expect(consoleErrors).toEqual([]);
});
