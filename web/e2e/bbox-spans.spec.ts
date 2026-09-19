import { expect, test } from '@playwright/test';
import { copyFileSync, mkdirSync } from 'node:fs';
import { resolve } from 'node:path';

// Reuse the real offline MinerU fixture used by preview.spec.ts, in an isolated workdir.
const did = `bbox-spans-${Date.now()}`;
const root = resolve('..', 'tmp');
test.beforeAll(() => {
  const dir = resolve(root, did);
  mkdirSync(resolve(dir, 'agent/source/mineru'), { recursive: true });
  copyFileSync(resolve(root, 'ccs3764-dyn/agent/source/mineru/provider_ir.json'), resolve(dir, 'agent/source/mineru/provider_ir.json'));
  copyFileSync(resolve(root, 'ccs3764-dyn/ccs2026b-paper3764/input.pdf'), resolve(dir, 'source.pdf'));
  mkdirSync(resolve(dir, 'output'));
  copyFileSync(resolve(root, 'ccs3764-dyn/output/ccs2026b-paper3764.no_watermark.zh.mono.pdf'), resolve(dir, 'output/paper.mono.pdf'));
});

test('real provider spans align with original PDF and labels persist across pages and compare mode', async ({ page, request }) => {
  await page.addInitScript(() => {
    localStorage.setItem('ieet.bboxMode', 'parse');
  });
  await page.goto(`/#/d/${did}/progress`);
  await page.getByRole('button', { name: '原文', exact: true }).click();
  await expect(page.locator('[data-reader-pane="primary"] [data-reader-page="1"] canvas')).toBeVisible();
  const response = await request.get(`/api/v1/documents/${did}/geometry?kind=parse`);
  expect(response.ok()).toBe(true);
  const data = await response.json();
  const inline = data.recognition_entities.filter((row: { label: string }) => row.label === 'inline_equation');
  expect(inline).toHaveLength(129);
  const first = inline[0];
  const input = page.getByRole('spinbutton', { name: '页码' });
  await input.fill(String(first.page));
  await input.press('Enter');
  const slot = page.locator(`[data-reader-pane="primary"] [data-reader-page="${first.page}"]`);
  await expect(slot.locator('canvas')).toBeVisible();
  const frames = slot.locator('[data-bbox-label="inline_equation"]');
  await expect(frames).toHaveCount(inline.filter((row: { page: number }) => row.page === first.page).length);
  const frame = slot.locator(`[data-bbox-id="${first.id}"]`);
  expect(await frame.getAttribute('tabindex')).toBeNull();
  expect(await frame.getAttribute('rx')).toBe('3');
  // PDF.js viewport and the span use the same PDF points, including at fit width.
  const rect = await frame.evaluate((node) => ({ x: Number(node.getAttribute('x')), y: Number(node.getAttribute('y')), width: Number(node.getAttribute('width')), height: Number(node.getAttribute('height')) }));
  const scale = rect.width / (first.box.x1 - first.box.x0);
  expect(rect.x).toBeCloseTo(first.box.x0 * scale, 3);
  expect(rect.y).toBeCloseTo(first.box.y0 * scale, 3);
  const darkPixels = await slot.locator('canvas').evaluate((canvas, box) => {
    const el = canvas as HTMLCanvasElement;
    const ratio = el.width / el.clientWidth;
    const pixels = el.getContext('2d')!.getImageData(Math.floor(box.x * ratio), Math.floor(box.y * ratio), Math.ceil(box.width * ratio), Math.ceil(box.height * ratio)).data;
    let dark = 0;
    for (let i = 0; i < pixels.length; i += 4) if (pixels[i] < 140 && pixels[i + 1] < 140 && pixels[i + 2] < 140) dark++;
    return dark;
  }, rect);
  expect(darkPixels).toBeGreaterThan(5);
  const legend = page.getByLabel('框类别设置');
  await expect(legend.getByRole('checkbox')).toHaveCount(data.labels.length);
  await expect(legend.getByRole('slider')).toHaveCount(0);
  await expect(legend.getByRole('button')).toHaveCount(0);
  await frame.scrollIntoViewIfNeeded();
  await page.screenshot({ path: test.info().outputPath('source-spans.png') });
  await page.getByLabel('inline_equation', { exact: true }).uncheck();
  await expect(page.locator('[data-bbox-label="inline_equation"]')).toHaveCount(0);
  await expect(slot.locator('[data-bbox-label="text"]').first()).toBeVisible();
  await input.fill('1');
  await input.press('Enter');
  await expect(page.getByLabel('inline_equation', { exact: true })).not.toBeChecked();
  await page.reload();
  await page.getByRole('button', { name: '原文', exact: true }).click();
  await expect(page.getByLabel('inline_equation', { exact: true })).not.toBeChecked();
  await input.fill(String(first.page));
  await input.press('Enter');
  await page.getByLabel('inline_equation', { exact: true }).check();
  await expect(frames).toHaveCount(inline.filter((row: { page: number }) => row.page === first.page).length);
  await page.getByRole('button', { name: '对照', exact: true }).click();
  await expect(page.locator('[data-reader-pane="source"] [data-bbox-label="inline_equation"]').first()).toBeVisible();
  await expect(page.locator('[data-reader-pane="primary"] [data-bbox-kind="span"]')).toHaveCount(0);
  await expect(page.locator('[data-reader-pane="primary"] canvas').first()).toBeVisible();
  await page.screenshot({ path: test.info().outputPath('compare-spans.png') });
  await page.getByRole('button', { name: '原文', exact: true }).click();
  await expect(input).toHaveAttribute('max', '21');
  await expect(page.locator('[data-reader-pane="primary"] [data-bbox-kind="block"]').first()).toBeVisible();
  await input.fill('4');
  await input.press('Enter');
  const formulaPage = page.locator('[data-reader-pane="primary"] [data-reader-page="4"]');
  await expect(formulaPage.locator('canvas')).toBeVisible();
  await expect(formulaPage.locator('[data-bbox-label="inline_equation"]')).toHaveCount(49);
  await page.setViewportSize({ width: 1280, height: 1500 });
  await formulaPage.scrollIntoViewIfNeeded();
  await formulaPage.screenshot({ path: test.info().outputPath('source-formulas.png') });
});
