import { expect, test, type Page } from '@playwright/test';

/** Small generated document: alternating page dimensions exercise scroll anchors
 * and compare-by-page rather than equal absolute scroll offsets. */
function pdf(pages: number, source: boolean): Buffer {
  const objects: string[] = ['', '', '<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>'];
  const kids: string[] = [];
  for (let page = 1; page <= pages; page += 1) {
    const id = objects.length + 1;
    kids.push(`${id} 0 R`);
    const height = source ? 700 : page % 2 ? 800 : 900;
    const stream = `BT /F1 24 Tf 40 ${height - 70} Td (Reader page ${page}) Tj ET`;
    objects.push(`<< /Type /Page /Parent 2 0 R /MediaBox [0 0 600 ${height}] /Resources << /Font << /F1 3 0 R >> >> /Contents ${id + 1} 0 R >>`);
    objects.push(`<< /Length ${stream.length} >>\nstream\n${stream}\nendstream`);
  }
  objects[0] = '<< /Type /Catalog /Pages 2 0 R >>';
  objects[1] = `<< /Type /Pages /Kids [${kids.join(' ')}] /Count ${pages} >>`;
  let text = '%PDF-1.4\n';
  const offsets = [0];
  objects.forEach((object, index) => {
    offsets.push(Buffer.byteLength(text));
    text += `${index + 1} 0 obj\n${object}\nendobj\n`;
  });
  const xref = Buffer.byteLength(text);
  text += `xref\n0 ${objects.length + 1}\n0000000000 65535 f \n`;
  text += offsets.slice(1).map((offset) => `${String(offset).padStart(10, '0')} 00000 n \n`).join('');
  text += `trailer\n<< /Size ${objects.length + 1} /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  return Buffer.from(text);
}

async function fixture(page: Page, compilable = false) {
  // 合并视图后不再按视图切 bbox 默认值：这个 fixture 的拖拽编辑层需要译文框（pdf_native）
  await page.addInitScript(() => localStorage.setItem('ieet.bboxMode', 'layout'));
  let revision = 1;
  const target = pdf(40, false);
  const source = pdf(38, true);
  await page.route('**/api/**', async (route) => {
    const url = new URL(route.request().url());
    const path = url.pathname;
    if (path.includes('/artifacts/')) {
      return route.fulfill({ contentType: 'application/pdf', body: path.endsWith('source.pdf') ? source : target });
    }
    if (path.endsWith('/jobs') && route.request().method() === 'POST') {
      revision += 1;
      return route.fulfill({ json: { job_id: 'fixture-compile', status: 'queued' } });
    }
    let body: unknown = [];
    if (path.endsWith('/documents/reader')) body = {
      did: 'reader', title: 'Reader fixture', pages: 40, paragraph_count: 40, translated_count: 40,
      stage_summary: {}, updated_at: null, pdf: { source: 'source.pdf', outputs: [] }, config: null,
      quality: {}, compile: compilable ? { status: 'ok', stale: revision === 1, revision, artifact: { name: 'test.mono.pdf', revision } } : {}, available: {},
    };
    if (path.endsWith('/artifacts')) body = [
      { name: 'source.pdf', kind: 'source', size: source.length, mtime: null },
      { name: 'output/test.mono.pdf', kind: 'pdf', size: target.length, mtime: null },
    ];
    if (path.endsWith('/draft')) body = { revision: 0, paragraphs: {} };
    if (path.endsWith('/geometry')) {
      const number = Number(url.searchParams.get('page'));
      const kind = url.searchParams.get('kind');
      const height = number % 2 ? 800 : 900;
      body = {
        did: 'reader', kind, page: number, coord_system: kind === 'parse' ? 'pdf_topleft' : 'pdf_native',
        entities: [{ id: `P${number}`, label: 'text', box: { x0: 40, y0: 40, x1: 300, y1: 80 } }],
        paragraphs: [{ id: `P${number}`, layout_label: 'text', layout_box: [40, height - 80, 300, height - 40] }],
      };
    }
    return route.fulfill({ json: body });
  });
  await page.goto('/#/d/reader/progress');
  await expect(page.locator('[data-reader-page="1"] canvas')).toBeVisible();
}

async function readingPosition(page: Page, pane: string) {
  return page.locator(`[data-reader-pane="${pane}"]`).evaluate((element) => {
    const slots = [...element.querySelectorAll<HTMLElement>('[data-reader-page]')];
    const slot = slots.find((item) => item.offsetTop + item.offsetHeight > element.scrollTop) ?? slots.at(-1)!;
    return { page: Number(slot.dataset.readerPage), fraction: Math.max(0, (element.scrollTop - slot.offsetTop) / slot.offsetHeight) };
  });
}

async function scrollToPage(page: Page, pane: string, number: number, fraction: number) {
  await page.locator(`[data-reader-pane="${pane}"]`).evaluate((element, value) => {
    const slot = element.querySelector<HTMLElement>(`[data-reader-page="${value.number}"]`)!;
    element.scrollTop = slot.offsetTop + slot.offsetHeight * value.fraction;
  }, { number, fraction });
}

test('continuous reading, navigation, zoom alignment, scoped gestures and narrow toolbar', async ({ page }) => {
  const pdfRequests: string[] = [];
  page.on('request', (request) => { if (request.url().includes('/artifacts/')) pdfRequests.push(request.url()); });
  await fixture(page);
  const pane = page.locator('[data-reader-pane="primary"]');
  expect(await pane.locator('canvas').count()).toBeLessThan(8);
  await page.getByRole('spinbutton', { name: '页码', exact: true }).fill('20');
  await page.getByRole('spinbutton', { name: '页码', exact: true }).press('Enter');
  await expect(pane.locator('[data-reader-page="20"] canvas')).toBeVisible();
  await scrollToPage(page, 'primary', 20, 0.3);
  await expect.poll(async () => (await readingPosition(page, 'primary')).fraction).toBeCloseTo(0.3, 1);
  const slot = pane.locator('[data-reader-page="20"]');
  const before = {
    width: await slot.locator('canvas').evaluate((canvas) => canvas.clientWidth),
    x: Number(await slot.locator('[data-bbox-id="P20"]').getAttribute('x')),
  };
  // 工具条上的缩放控件（−/百分比/＋/适宽）已删除：Ctrl+滚轮 = 缩放。
  // 一次放大后：画布变宽、阅读位置（fraction）不变、bbox 与画布同步放大。
  await pane.evaluate((element) => {
    element.dispatchEvent(new WheelEvent('wheel', { deltaY: -60, ctrlKey: true, bubbles: true, cancelable: true }));
  });
  await expect.poll(() => slot.locator('canvas').evaluate((canvas) => canvas.clientWidth)).toBeGreaterThan(before.width);
  await expect.poll(async () => (await readingPosition(page, 'primary')).fraction).toBeCloseTo(0.3, 1);
  const after = {
    width: await slot.locator('canvas').evaluate((canvas) => canvas.clientWidth),
    x: Number(await slot.locator('[data-bbox-id="P20"]').getAttribute('x')),
  };
  expect(after.x / after.width).toBeCloseTo(before.x / before.width, 1);
  await pane.evaluate((element) => { element.scrollLeft = 100; });
  await expect.poll(() => pane.evaluate((element) => element.scrollLeft)).toBe(100);
  const wheel = await pane.evaluate((element) => {
    const ordinary = new WheelEvent('wheel', { deltaY: 50, bubbles: true, cancelable: true });
    element.dispatchEvent(ordinary);
    const pinch = new WheelEvent('wheel', { deltaY: -10, ctrlKey: true, bubbles: true, cancelable: true });
    element.dispatchEvent(pinch);
    return { ordinary: ordinary.defaultPrevented, pinch: pinch.defaultPrevented };
  });
  expect(wheel).toEqual({ ordinary: false, pinch: true });
  expect(await page.locator('[data-od-id="preview-toolbar"]').evaluate((element) => {
    const event = new WheelEvent('wheel', { ctrlKey: true, cancelable: true });
    element.dispatchEvent(event);
    return event.defaultPrevented;
  })).toBe(false);
  await page.setViewportSize({ width: 1100, height: 800 });
  const toolbar = page.locator('[data-od-id="preview-toolbar"]');
  expect(await toolbar.evaluate((element) => element.scrollWidth <= element.clientWidth)).toBe(true);
  // bbox 三态与下载已从《更多》移出，直接平铺（不需要点开任何菜单）
  await expect(page.getByRole('group', { name: 'bbox 图层' })).toBeVisible();
  await expect(page.getByRole('button', { name: '译文框', exact: true })).toBeVisible();
  await page.getByRole('spinbutton', { name: '页码', exact: true }).fill('20');
  await page.getByRole('spinbutton', { name: '页码', exact: true }).press('Enter');
  await expect(pane.locator('[data-reader-page="20"] canvas')).toBeVisible();
  await expect(pane.locator('[data-od-id="preview-loading"]')).toHaveCount(0);
  expect(pdfRequests).toHaveLength(1);
  expect(await pane.locator('canvas').count()).toBeLessThan(8);
  await scrollToPage(page, 'primary', 20, 0);
  await page.screenshot({ path: test.info().outputPath('reader-narrow.png') });
});

test('compare defaults linked by page and relative position and can unlink', async ({ page }) => {
  await fixture(page);
  await page.getByRole('button', { name: '对照', exact: true }).click();
  await expect(page.locator('[data-reader-pane="source"] canvas').first()).toBeVisible();
  await scrollToPage(page, 'primary', 2, 0.35);
  await expect.poll(async () => (await readingPosition(page, 'source')).page).toBe(2);
  await expect.poll(async () => (await readingPosition(page, 'source')).fraction).toBeCloseTo(0.35, 1);
  await page.getByRole('button', { name: '解除联动' }).click();
  await scrollToPage(page, 'primary', 3, 0.2);
  await expect.poll(async () => (await readingPosition(page, 'primary')).page).toBe(3);
  expect((await readingPosition(page, 'source')).page).toBe(2);
  // 上一页/下一页按钮已删除：跳页用页码输入
  const nextInput = page.getByRole('spinbutton', { name: '页码' });
  await nextInput.fill('4');
  await nextInput.press('Enter');
  await expect.poll(async () => (await readingPosition(page, 'primary')).page).toBe(4);
  expect((await readingPosition(page, 'source')).page).toBe(2);
  await scrollToPage(page, 'primary', 3, 0.2);
  await expect.poll(async () => (await readingPosition(page, 'primary')).page).toBe(3);
  await page.getByRole('button', { name: '联动阅读' }).click();
  await expect.poll(async () => (await readingPosition(page, 'source')).page).toBe(3);
  await scrollToPage(page, 'source', 3, 0.4);
  await expect.poll(async () => (await readingPosition(page, 'primary')).fraction).toBeCloseTo(0.4, 1);
  await page.screenshot({ path: test.info().outputPath('reader-compare.png') });
});

test('draft box editing stays usable; hash changes keep the user\'s toolbar choices', async ({ page }) => {
  await fixture(page);
  await expect(page.getByRole('button', { name: '译文', exact: true })).toHaveAttribute('aria-pressed', 'true');
  const box = page.locator('[data-reader-page="1"] [data-bbox-id="P1"]');
  await box.click();
  await expect(page.locator('[data-od-id="bbox-editor"]')).toHaveAttribute('data-editable', 'true');
  const patch = page.waitForRequest((request) => request.method() === 'PATCH' && request.url().endsWith('/draft'));
  const handle = page.locator('[data-od-id="bbox-handle-se"]');
  const rect = (await handle.boundingBox())!;
  await page.mouse.move(rect.x + rect.width / 2, rect.y + rect.height / 2);
  await page.mouse.down();
  await page.mouse.move(rect.x + rect.width / 2 + 15, rect.y + rect.height / 2 + 10);
  await page.mouse.up();
  expect((await patch).postDataJSON().paragraphs.P1.layout.box).toHaveLength(4);
  // 视图合并后没有按视图记默认值：hash 变化（旧链接也落到同一工作台）不再重置用户的工具条选择
  await page.evaluate(() => { location.hash = '#/d/reader/layout'; });
  await expect(page.getByRole('button', { name: '译文', exact: true })).toHaveAttribute('aria-pressed', 'true');
  // bbox 三态直接平铺（《更多》菜单已删除）
  await page.getByRole('button', { name: '关', exact: true }).click();
  await page.getByRole('button', { name: '对照', exact: true }).click();
  await page.evaluate(() => { location.hash = '#/d/reader/check'; });
  await expect(page.getByRole('button', { name: '对照', exact: true })).toHaveAttribute('aria-pressed', 'true');
  await expect(page.getByRole('button', { name: '关', exact: true })).toHaveAttribute('aria-pressed', 'true');
});


test('URL remount preserves the same pane page and fraction and toolbar state', async ({ page }) => {
  await fixture(page);
  const pageInput = page.getByRole('spinbutton', { name: '页码', exact: true });
  await pageInput.fill('20');
  await pageInput.press('Enter');
  await expect(page.locator('[data-reader-page="20"] canvas')).toBeVisible();
  await scrollToPage(page, 'primary', 20, 0.3);
  await expect.poll(async () => (await readingPosition(page, 'primary')).fraction).toBeCloseTo(0.3, 1);
  // A manual scroll supersedes the last explicit navigation command.
  await scrollToPage(page, 'primary', 21, 0.25);
  await expect(pageInput).toHaveValue('21');
  for (const mode of ['原文', '译文']) {
    await page.getByRole('button', { name: mode, exact: true }).click();
    await expect.poll(async () => (await readingPosition(page, 'primary')).page).toBe(21);
    await expect.poll(async () => (await readingPosition(page, 'primary')).fraction).toBeCloseTo(0.25, 1);
    await expect(pageInput).toHaveValue('21');
    expect(await page.locator('[data-reader-pane="primary"] canvas').count()).toBeLessThan(8);
  }
  const lastInput = page.getByRole('spinbutton', { name: '页码' });
  await lastInput.fill('22');
  await lastInput.press('Enter');
  await expect.poll(async () => (await readingPosition(page, 'primary')).page).toBe(22);
  await expect(pageInput).toHaveValue('22');
});

test('unlinked compare uses the active pane page count', async ({ page }) => {
  await fixture(page);
  await page.getByRole('button', { name: '对照', exact: true }).click();
  await expect(page.locator('[data-reader-pane="source"] canvas').first()).toBeVisible();
  await page.getByRole('button', { name: '解除联动' }).click();
  await scrollToPage(page, 'source', 2, 0.3);
  const pageInput = page.getByRole('spinbutton', { name: '页码', exact: true });
  await expect(pageInput).toHaveAttribute('max', '38');
  await scrollToPage(page, 'primary', 2, 0.3);
  await expect(pageInput).toHaveAttribute('max', '40');
});


test('compiled revision remount restores an unlinked pane without moving its peer', async ({ page }) => {
  await fixture(page, true);
  await page.getByRole('button', { name: '对照', exact: true }).click();
  await expect(page.locator('[data-reader-pane="source"] canvas').first()).toBeVisible();
  await scrollToPage(page, 'primary', 2, 0.3);
  await expect.poll(async () => (await readingPosition(page, 'source')).fraction).toBeCloseTo(0.3, 1);
  await page.getByRole('button', { name: '解除联动' }).click();
  await scrollToPage(page, 'primary', 3, 0.25);
  await expect(page.getByRole('spinbutton', { name: '页码', exact: true })).toHaveValue('3');
  // 全文编译入口在右栏操作区（工具条上的下载槽与旧「手动编译」按钮均已删除）：
  // 提交后产物修订号变化 → 预览按 `?r=2` 重新取字节
  const refreshed = page.waitForRequest((request) => request.url().includes('/artifacts/') && new URL(request.url()).searchParams.get('r') === '2');
  await page.locator('[data-od-id="action-bar"] [data-od-id="compile-full"]').click();
  await refreshed;
  await expect.poll(async () => (await readingPosition(page, 'primary')).page).toBe(3);
  await expect.poll(async () => (await readingPosition(page, 'primary')).fraction).toBeCloseTo(0.25, 1);
  await expect(page.getByRole('spinbutton', { name: '页码', exact: true })).toHaveValue('3');
  expect((await readingPosition(page, 'source')).page).toBe(2);
  expect((await readingPosition(page, 'source')).fraction).toBeCloseTo(0.3, 1);
});
