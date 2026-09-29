// 端到端验收：真实 `bdt cloud serve` + 假引擎（e2e/fake-engine.py）+ vite preview 代理，
// 在 1440×900 与 390×844 下走完登录、上传、逐页预览、完成/部分完成/失败、排队、取消、
// 重新编译/重新翻译（含失败回滚）、缓存命中、历史抽屉删除与退出，逐步断言并截图。产物写到仓库 tmp/cloud-web/e2e-<时间>/。
import { execFileSync, spawn } from 'node:child_process';
import fs from 'node:fs';
import net from 'node:net';
import os from 'node:os';
import path from 'node:path';
import readline from 'node:readline';
import { fileURLToPath } from 'node:url';

import { chromium } from 'playwright';

const web = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const repo = path.resolve(web, '..');
const PY = process.env.BDT_PYTHON ?? path.join(os.homedir(), 'miniconda3/envs/bdt/bin/python');
const stamp = new Date().toISOString().replace(/\D/g, '').slice(0, 14);
const out = path.join(repo, 'tmp/cloud-web', `e2e-${stamp}`);
const shots = path.join(out, 'shots');
const hold = path.join(out, 'hold');
const failFlag = path.join(out, 'fail');
fs.mkdirSync(shots, { recursive: true });

const children = [];
const cleanup = () => children.forEach((c) => c.kill('SIGTERM'));
process.on('exit', cleanup);

function assert(cond, message) {
  if (!cond) throw new Error(`断言失败：${message}`);
}

function freePort() {
  return new Promise((resolve) => {
    const s = net.createServer().listen(0, '127.0.0.1', () => {
      const { port } = s.address();
      s.close(() => resolve(port));
    });
  });
}

function py(code, ...args) {
  return execFileSync(PY, ['-c', code, ...args], { cwd: repo, encoding: 'utf-8' });
}

// 每个视口一套内容不同的原文，互不命中缓存
function fixtures(tag) {
  const dir = path.join(out, 'fixtures', tag);
  fs.mkdirSync(dir, { recursive: true });
  py(
    `
import sys, pymupdf
dir, tag = sys.argv[1], sys.argv[2]
body = "We study attention mechanisms for document translation. " * 40
for name, mode, pages in [("attention-is-all-you-need", "success", 4), ("survey-partial", "partial", 3),
                          ("broken", "fail", 2), ("long-paper", "success", 3), ("queued-paper", "success", 2)]:
    doc = pymupdf.open()
    for n in range(pages):
        page = doc.new_page(width=612, height=792)
        page.insert_text((56, 80), f"{name} [{tag}] page {n + 1}", fontsize=18)
        page.insert_textbox(pymupdf.Rect(56, 110, 556, 740), body, fontsize=11)
    doc.set_metadata({"subject": "fake:" + mode})
    doc.save(f"{dir}/{name}.pdf")
blank = pymupdf.open(); blank.new_page(); blank.save(f"{dir}/scanned.pdf")
`,
    dir,
    tag,
  );
  return (name) => path.join(dir, `${name}.pdf`);
}

function invite(root, name) {
  const line = execFileSync(PY, ['-m', 'babeldoc_tools', 'cloud', 'invite', '--root', root, '--name', name], {
    cwd: repo,
    encoding: 'utf-8',
  });
  return JSON.parse(line).data.code;
}

async function startBackend(root) {
  const engine = path.join(out, 'fake-engine');
  fs.writeFileSync(engine, `#!${PY}\n` + fs.readFileSync(path.join(web, 'e2e/fake-engine.py'), 'utf-8'));
  fs.chmodSync(engine, 0o755);
  const child = spawn(
    PY,
    ['-m', 'babeldoc_tools', 'cloud', 'serve', '--root', root, '--port', '0', '--engine', engine, '--translator', 'fake:echo'],
    { cwd: repo, env: { ...process.env, FAKE_HOLD: hold, FAKE_FAIL: failFlag, FAKE_PAGE_DELAY: '0.8' }, stdio: ['ignore', 'pipe', 'pipe'] },
  );
  children.push(child);
  child.stderr.pipe(fs.createWriteStream(path.join(out, 'backend.log')));
  const first = await new Promise((resolve, reject) => {
    readline.createInterface({ input: child.stdout }).once('line', resolve);
    child.once('exit', (code) => reject(new Error(`bdt cloud serve 退出：${code}`)));
  });
  return JSON.parse(first).data.port;
}

async function startPreview(apiPort) {
  const port = await freePort();
  const child = spawn(path.join(web, 'node_modules/.bin/vite'), ['preview', '--port', String(port), '--strictPort'], {
    cwd: web,
    env: { ...process.env, BDT_CLOUD_PORT: String(apiPort) },
    stdio: ['ignore', 'pipe', 'pipe'],
  });
  children.push(child);
  const base = `http://127.0.0.1:${port}`;
  for (let i = 0; i < 100; i++) {
    try {
      if ((await fetch(base)).ok) return base;
    } catch {}
    await new Promise((r) => setTimeout(r, 100));
  }
  throw new Error('vite preview 没有启动');
}

// 预期内的失败请求：未登录时的 /api/me、错误邀请码、扫描件上传
const EXPECTED = [
  [401, /\/api\/me$/],
  [401, /\/api\/jobs$/],
  [401, /\/api\/login$/],
  [422, /\/api\/jobs$/],
];

function watch(page, errors) {
  page.on('pageerror', (e) => errors.push(`pageerror ${e.message}`));
  page.on('console', (m) => {
    if (m.type() === 'error' && !m.text().startsWith('Failed to load resource')) errors.push(`console ${m.text()}`);
  });
  page.on('response', (r) => {
    const url = new URL(r.url()).pathname;
    if (r.status() >= 400 && !EXPECTED.some(([s, re]) => s === r.status() && re.test(url)))
      errors.push(`http ${r.status()} ${r.request().method()} ${url}`);
  });
}

async function scenario(browser, base, root, tag, viewport) {
  const file = fixtures(tag);
  const codeA = invite(root, `${tag}-甲`);
  const codeB = invite(root, `${tag}-乙`);
  const errors = [];
  const opts = { viewport, deviceScaleFactor: tag === 'm' ? 2 : 1, acceptDownloads: true };
  const ctxA = await browser.newContext(opts);
  const a = await ctxA.newPage();
  watch(a, errors);
  // 截图前等已挂上的预览图都解码完、入场动画结束；动画中间帧（now）立即截
  async function shot(p, name, now = false) {
    if (!now) {
      await p.waitForFunction(() => [...document.images].every((img) => img.complete && img.naturalWidth > 0), null, {
        timeout: 15000,
      });
      await p.waitForTimeout(400);
    }
    // 整页截图时窗口若已滚动，吸顶栏会被拍在中间
    if (tag === 'm') await p.evaluate(() => window.scrollTo(0, 0));
    await p.screenshot({ path: path.join(shots, `${tag}-${name}.png`), fullPage: tag === 'm' });
  }
  const chip = (p) => p.locator('.events .ev-head .status');
  const quota = async (p) => Number((await p.locator('.quota').innerText()).match(/\d+/)[0]);
  // 顶栏额度在事件后异步刷新：轮询到期望值
  async function expectQuota(p, n, message) {
    for (let i = 0; i < 30 && (await quota(p)) !== n; i++) await p.waitForTimeout(100);
    assert((await quota(p)) === n, `${message}：期望 ${n}，实际 ${await quota(p)}`);
  }

  async function login(p, code) {
    await p.goto(base);
    await p.locator('#invite').fill(code);
    await p.getByRole('button', { name: '进入镜译' }).click();
    await p.locator('.v-home .hero').waitFor();
  }
  async function submit(p, pdf) {
    await p.locator('input[type=file]').setInputFiles(pdf);
    await p.getByRole('button', { name: '开始翻译' }).click();
    await p.waitForURL(/#\/job\/[0-9a-f]+$/);
    await chip(p).waitFor();
  }
  const home = (p) => p.locator('.brand').click();

  // 登录：错误邀请码给出提示，正确的进入首页
  await a.goto(base);
  await a.locator('.v-login .login-card').waitFor();
  await shot(a, 'login');
  await a.locator('#invite').fill('YJ-0000-0000');
  await a.getByRole('button', { name: '进入镜译' }).click();
  await a.locator('.v-login .hint', { hasText: '邀请码' }).first().waitFor();
  await shot(a, 'login-error');
  await login(a, codeA);
  await expectQuota(a, 5, '初始额度');
  await shot(a, 'idle');

  // 模型：显示 harness / provider / model id / reasoning_effort；agy 只支持 low
  const spec = async (p, scope) =>
    Object.fromEntries(
      await p.locator(`${scope} .spec > div`).evaluateAll((rows) =>
        rows.map((r) => [r.querySelector('dt').textContent, r.querySelector('dd').textContent]),
      ),
    );
  const specOf = (s) => `${s.harness}/${s.provider}/${s['model id']}/${s.reasoning_effort}`;
  const effort = (p, level) => p.locator('.effort .seg button', { hasText: level });
  await effort(a, 'high').click();
  let shown = specOf(await spec(a, '.new-card'));
  assert(shown === 'pi/deepseek/deepseek-flash/high', `默认模型配置，实际 ${shown}`);
  await a.getByRole('radio', { name: /Gemini 3.8 Flash（官方）/ }).click();
  shown = specOf(await spec(a, '.new-card'));
  assert(shown === 'agy/gemini/gemini-3.8-flash-low/low', `agy 模型配置，实际 ${shown}`);
  assert(await effort(a, 'medium').isDisabled(), 'agy 不能选 medium');
  assert(await effort(a, 'high').isDisabled(), 'agy 不能选 high');
  await shot(a, 'model-agy');
  await a.getByRole('radio', { name: /DeepSeek Flash/ }).click();
  assert(!(await effort(a, 'high').isDisabled()), '换回 pi 模型后可选 high');
  assert((await effort(a, 'low').getAttribute('class')) === 'on', '换到 agy 时思考强度退回 low，换回后保持 low');

  // 扫描件：服务端拒绝并提示
  await a.locator('input[type=file]').setInputFiles(file('scanned'));
  await a.getByRole('button', { name: '开始翻译' }).click();
  await a.locator('.toast.show', { hasText: '文字版' }).waitFor();
  await a.locator('.picked .btn-ghost').waitFor();

  // 成功：逐页扫过、完成、预览宽 1600、下载译文与对照
  await a.locator('input[type=file]').setInputFiles(file('attention-is-all-you-need'));
  await shot(a, 'picked');
  await a.getByRole('button', { name: '开始翻译' }).click();
  await a.waitForURL(/#\/job\//);
  await a.locator('.sheet.trans[data-s="anim"]').first().waitFor({ timeout: 30000 });
  await shot(a, 'translating-anim', true);
  await a.locator('.sheet.trans[data-s="zh"]').first().waitFor();
  await shot(a, 'translating');
  await chip(a).filter({ hasText: '已完成' }).waitFor({ timeout: 60000 });
  await a.waitForTimeout(1300); // 最后一页的扫过动画
  await shot(a, 'done');
  await expectQuota(a, 4, '完成一篇扣 1 篇额度');
  shown = specOf(await spec(a, '.pv-title'));
  assert(shown === 'pi/deepseek/deepseek-flash/low', `工作区显示本次配置，实际 ${shown}`);
  const width = await a.locator('.sheet.trans img.zh').first().evaluate((img) => img.naturalWidth);
  assert(width === 1600, `译文预览宽 1600，实际 ${width}`);
  for (const [name, suffix] of [['下载译文', '-中文.pdf'], ['下载中英对照', '-中英对照.pdf']]) {
    const [download] = await Promise.all([a.waitForEvent('download'), a.getByRole('link', { name }).click()]);
    const saved = path.join(out, `${tag}-${download.suggestedFilename()}`);
    await download.saveAs(saved);
    assert(download.suggestedFilename() === `attention-is-all-you-need${suffix}`, `下载文件名 ${download.suggestedFilename()}`);
    assert(fs.readFileSync(saved).subarray(0, 4).toString() === '%PDF', `${name} 是 PDF`);
  }
  for (const mode of ['原文', '对照']) {
    await a.locator('.pv-head .seg button', { hasText: mode }).click();
    await shot(a, `done-${mode === '原文' ? 'orig' : 'dual'}`);
  }
  await a.locator('.pv-head .seg button', { hasText: '译文' }).click();

  // 重新编译：沿用已存译文重排，进度从这一轮重新开始，不占额度
  const firstEvent = (p) => p.locator('.events .ev').first();
  await a.getByRole('button', { name: '重新编译', exact: true }).click();
  await firstEvent(a).filter({ hasText: '用已保存的译文重新编译' }).waitFor();
  await shot(a, 'recompiling', true);
  await chip(a).filter({ hasText: '已完成' }).waitFor({ timeout: 60000 });
  await expectQuota(a, 4, '重新编译不占额度');

  // 重新翻译：二次确认后从头翻一遍，同样不占额度
  await a.getByRole('button', { name: '重新翻译', exact: true }).click();
  await a.locator('.rerun', { hasText: '确定从头重新翻译？' }).waitFor();
  await shot(a, 'retranslate-confirm');
  await a.locator('.rerun').getByRole('button', { name: '重新翻译', exact: true }).click();
  await firstEvent(a).filter({ hasText: '从头完整重新翻译' }).waitFor();
  await chip(a).filter({ hasText: '已完成' }).waitFor({ timeout: 60000 });
  await expectQuota(a, 4, '重新翻译不占额度');

  // 重跑失败：回滚成原来的译文，仍是已完成、可下载，阶段条不停在出错那一步
  fs.writeFileSync(failFlag, '');
  await a.getByRole('button', { name: '重新翻译', exact: true }).click();
  await a.locator('.rerun').getByRole('button', { name: '重新翻译', exact: true }).click();
  await a.locator('.events .ev', { hasText: '已保留原来的译文' }).waitFor({ timeout: 60000 });
  fs.rmSync(failFlag);
  await chip(a).filter({ hasText: '已完成' }).waitFor();
  await a.getByRole('link', { name: '下载译文' }).waitFor();
  assert((await a.locator('.stepper .step.fail').count()) === 0, '回滚后阶段条不显示出错');
  await a.locator('.sheet.trans img.zh').first().waitFor();
  await shot(a, 'rerun-rolled-back');
  await expectQuota(a, 4, '重跑失败不占额度');

  // 部分完成：黄色提醒、第 2 页回退框
  await home(a);
  await submit(a, file('survey-partial'));
  await chip(a).filter({ hasText: '处提醒' }).waitFor({ timeout: 60000 });
  assert((await a.locator('.fb-box').count()) >= 1, '部分完成页上有回退框');
  await a.locator('.page-row[data-page="2"]').scrollIntoViewIfNeeded();
  await a.locator('.pill.fb', { hasText: '保留原文' }).first().waitFor();
  await shot(a, 'partial');

  // 失败：红色，不扣额度
  const before = await quota(a);
  await home(a);
  await submit(a, file('broken'));
  await chip(a).filter({ hasText: '未完成' }).waitFor({ timeout: 60000 });
  await a.locator('.pv-empty').waitFor();
  await shot(a, 'failed');
  await expectQuota(a, before, '失败不扣额度');

  // 排队：甲的那篇停在第 1 页，乙提交后看到「前面还有 1 篇」
  fs.writeFileSync(hold, '');
  await home(a);
  const beforeLong = await quota(a);
  await submit(a, file('long-paper'));
  await a.locator('.sheet.trans[data-s="zh"]').first().waitFor({ timeout: 30000 });
  const ctxB = await browser.newContext(opts);
  const b = await ctxB.newPage();
  watch(b, errors);
  await login(b, codeB);
  await submit(b, file('queued-paper'));
  await b.locator('.queue .q1', { hasText: '前面还有 1 篇' }).waitFor();
  await shot(b, 'queued');

  // 甲回首页：后台继续；「最近翻译」与抽屉里都有进行中条目，点它回到工作区
  await home(a);
  await a.locator('.toast.show', { hasText: '翻译仍在进行' }).waitFor();
  await a.locator('.recent .rc-item.active', { hasText: 'long-paper' }).filter({ hasText: '查看进度' }).waitFor();
  await shot(a, 'home-running');
  await a.locator('.logo-btn').click();
  await a.locator('.drawer .hi', { hasText: 'long-paper' }).waitFor();
  await shot(a, 'drawer-running');
  await a.locator('.drawer .hi', { hasText: 'long-paper' }).click();
  await chip(a).filter({ hasText: '翻译中' }).waitFor();
  // 换上译文图与开始扫过在同一次渲染里：图一出现就看状态
  const resumed = await a
    .locator('.page-row[data-page="1"] .sheet.trans:has(img.zh)')
    .evaluate((el) => el.dataset.s);
  assert(resumed === 'zh', `回到进行中的任务不重播已完成页的动画，实际 ${resumed}`);

  // 取消：二次确认后回首页、退回运行中占用的额度；乙随即开始
  await expectQuota(a, beforeLong - 1, '运行中先占 1 篇额度');
  await a.getByRole('button', { name: '取消翻译' }).click();
  await shot(a, 'cancel-confirm');
  await a.getByRole('button', { name: '确定取消' }).click();
  await a.locator('.toast.show', { hasText: '已取消' }).waitFor();
  await a.locator('.v-home .hero').waitFor();
  await expectQuota(a, beforeLong, '取消不扣额度');
  await chip(b).filter({ hasText: '翻译中' }).waitFor({ timeout: 30000 });
  fs.rmSync(hold);
  await chip(b).filter({ hasText: '已完成' }).waitFor({ timeout: 60000 });

  // 缓存命中：乙上传甲翻过的同一篇，秒出且不占额度，所有页一起变中文
  const quotaB = await quota(b);
  await home(b);
  await b.locator('input[type=file]').setInputFiles(file('attention-is-all-you-need'));
  await b.getByRole('button', { name: '开始翻译' }).click();
  await b.locator('.sheet.trans[data-s="anim"]').first().waitFor();
  await shot(b, 'cache-mid', true);
  await b.locator('.result .rs', { hasText: '已从译文库直接加载' }).waitFor();
  await b.waitForTimeout(1200);
  await shot(b, 'cache-end');
  await expectQuota(b, quotaB, '缓存命中不占额度');
  await b.getByRole('button', { name: '换个思考强度重新翻译' }).click();
  await b.locator('.toast.show', { hasText: '思考强度' }).waitFor();
  assert((await b.locator('.picked .name').innerText()) === 'attention-is-all-you-need.pdf', '重试保留已选文件');

  // 历史抽屉：删除本人的失败记录
  await a.locator('.logo-btn').click();
  await a.locator('.drawer .hi', { hasText: 'broken' }).waitFor();
  await shot(a, 'drawer');
  await a.locator('.drawer .hi', { hasText: 'broken' }).locator('.more').click();
  await shot(a, 'drawer-menu');
  await a.getByRole('button', { name: /删除记录/ }).click();
  await a.locator('.toast.show', { hasText: '已从你的历史中删除' }).waitFor();
  assert((await a.locator('.drawer .hi', { hasText: 'broken' }).count()) === 0, '删除后不再出现');
  await a.locator('.scrim').click({ force: true });

  // 刷新保留登录与当前页；退出后回登录页
  await a.reload();
  await a.locator('.v-home .hero').waitFor();
  await a.locator('.avatar').click();
  await shot(a, 'account-menu');
  await a.getByRole('button', { name: '退出登录' }).click();
  await a.locator('.v-login .login-card').waitFor();

  await ctxA.close();
  await ctxB.close();
  return errors;
}

execFileSync(path.join(web, 'node_modules/.bin/vite'), ['build'], { cwd: web, stdio: 'inherit' });
const root = path.join(out, 'root');
const base = await startPreview(await startBackend(root));
const browser = await chromium.launch();
let failed = false;
try {
  for (const [tag, viewport] of [['d', { width: 1440, height: 900 }], ['m', { width: 390, height: 844 }]]) {
    const errors = await scenario(browser, base, root, tag, viewport);
    console.log(`${tag}: ${errors.length ? errors.join('\n  ') : '无控制台/请求错误'}`);
    failed ||= errors.length > 0;
  }
} catch (e) {
  failed = true;
  console.error(e);
} finally {
  await browser.close();
  cleanup();
}
console.log(`截图：${shots}`);
process.exit(failed ? 1 : 0);
