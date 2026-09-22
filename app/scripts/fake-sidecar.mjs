#!/usr/bin/env node
/**
 * 假 sidecar（测试与开发用，替代尚不存在的 syncpdf-cli 二进制）。
 *
 * 行为：
 * - 读 stdin JSONL：收到 `configure` 回一个 `progress` 确认（stage=preflight）；
 * - 收到 `run` 后：
 *   1. 先把 `input` 复制到 `output`——这样译文栏一开始就有一份可渲染的 PDF，
 *      之后每页"就绪"都是在这份文件上原地更新（真引擎也是就地重写 output）；
 *   2. 按协议 §9.3 发完整事件流：run_started → 每阶段 stage_started/stage_finished →
 *      每 TICK_MS 发 progress / paragraph，每页末尾发一次
 *      `page_ready{ preview_path: null }`（null = 无独立预览产物，前端重新加载
 *      output 文件的该页），12 页正好 12 次 → document_finished → run_finished；
 * - `retranslate`：对每个段落重发 paragraph，并给相关页补 page_ready（revision 推进）；
 * - `apply_edit`：`base_revision` 与当前修订号不符 → `error{code:"conflict"}`；
 *   相符 → 回一条 paragraph（译文换成提交的 HTML）+ 该页 page_ready；
 * - 收到 `cancel` 或 stdin EOF 立即退出（退出码 0）。
 *
 * 输出格式严格按 02-技术路径与架构.md §9.3：每行
 * `{"seq":n,"ts":…,"type":"…",…}`，seq 从 1 单调递增。
 */
import { once } from 'node:events';
import { copyFile } from 'node:fs/promises';
import readline from 'node:readline';

const PAGES = 12;
const PARAGRAPHS_PER_PAGE = 3; // 12 页 × 3 段 = 36 ≥ 30 段
const TICK_MS = 200;

let seq = 0;
let cancelled = false;
/** 当前文档修订号：与渲染进程 documentStore.revision 同构（page_ready / document_finished +1）。 */
let revision = 0;
/** 最近一次 run 的上下文（retranslate / apply_edit 要用）。 */
let currentRun = null;
/** paragraph_id → 最近一次发出的译文 HTML。 */
const translations = new Map();

function emit(event) {
  seq += 1;
  if (event.type === 'page_ready' || event.type === 'document_finished') {
    revision += 1;
  }
  const line = JSON.stringify({ seq, ts: Date.now() / 1000, ...event });
  process.stdout.write(`${line}\n`);
}

function paragraphId(page, index) {
  return `P${String(page).padStart(2, '0')}-${String(index).padStart(3, '0')}`;
}

/** 从 `P05-002` 取页号。 */
function pageOf(id) {
  const match = /^P(\d+)-/.exec(String(id));
  return match === null ? null : Number.parseInt(match[1], 10);
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * 段落在页面上的框（pdf_native：左下原点、y 向上）。
 * 按 A4/Letter 通用可读区放：x 56..540，自上而下排三段。
 */
function boxesFor(index) {
  const top = 700 - (index - 1) * 90;
  return [[56, top - 56, 540, top]];
}

/** 发一个段落事件，并记住译文。 */
function emitParagraph(id, status, html) {
  translations.set(id, html);
  emit({
    type: 'paragraph',
    paragraph_id: id,
    status,
    boxes: boxesFor(Number.parseInt(id.slice(-3), 10)),
    coord_system: 'pdf_native',
    translated_html: html,
  });
}

/** `page_ready`：preview_path 显式为 null（就地重写 output，无独立预览产物）。 */
function emitPageReady(page) {
  emit({ type: 'page_ready', page, preview_path: null });
}

/** run 开始前把源 PDF 复制成 output（译文栏的初始可渲染内容）。 */
async function seedOutput(request) {
  const { input, output } = request;
  if (typeof input !== 'string' || typeof output !== 'string') return;
  if (input === '' || output === '' || input === output) return;
  try {
    await copyFile(input, output);
  } catch (error) {
    // 源文件不存在（纯协议测试）时不阻断事件流
    process.stderr.write(`fake-sidecar: 复制 input→output 失败：${String(error)}\n`);
  }
}

async function emitRun(request) {
  const pages = typeof request.pages === 'number' && request.pages > 0 ? request.pages : PAGES;
  currentRun = { docId: request.doc_id, pages, output: request.output };
  await seedOutput(request);
  if (cancelled) return;

  emit({
    type: 'run_started',
    protocol_version: 1,
    engine_version: 'fake-sidecar 0.1.0',
    doc_id: request.doc_id,
    pages,
  });
  // run_started 会让渲染进程把 revision 归零，这里保持同步
  revision = 0;

  const stages = [
    'preflight',
    'source_analysis',
    'layout_analysis',
    'paragraph_analysis',
    'translating',
    'typesetting',
    'validating',
    'publishing',
  ];
  for (const stage of stages) {
    if (cancelled) return;
    emit({ type: 'stage_started', stage });
    const stageStart = Date.now();
    if (stage === 'translating') {
      // 翻译阶段逐页逐段发 progress / paragraph，每页末尾一次 page_ready
      const totalParagraphs = pages * PARAGRAPHS_PER_PAGE;
      let done = 0;
      for (let page = 1; page <= pages; page += 1) {
        if (cancelled) return;
        for (let i = 1; i <= PARAGRAPHS_PER_PAGE; i += 1) {
          if (cancelled) return;
          const id = paragraphId(page, i);
          const paragraphIndex = (page - 1) * PARAGRAPHS_PER_PAGE + i;
          // 覆盖四种段落状态
          const statusPool = ['translated', 'translated', 'typeset', 'not_replaced', 'fallback'];
          const status = statusPool[paragraphIndex % statusPool.length];
          emit({ type: 'progress', stage, done, total: totalParagraphs });
          emitParagraph(id, status, `<p id="${id}">译文段落 ${paragraphIndex}</p>`);
          done += 1;
          await sleep(TICK_MS);
        }
        if (cancelled) return;
        emitPageReady(page);
      }
      emit({ type: 'progress', stage, done, total: totalParagraphs });
    } else if (stage === 'validating') {
      // 演示一条非致命 issue
      emit({
        type: 'issue',
        severity: 'warning',
        code: 'font_missing',
        paragraph_id: paragraphId(1, 1),
        page: 1,
        message: '回退 Noto Sans（fake-sidecar 演示）',
      });
    }
    emit({ type: 'stage_finished', stage, elapsed_ms: Date.now() - stageStart });
  }

  emit({
    type: 'document_finished',
    output: typeof request.output === 'string' ? request.output : '/tmp/syncpdf-fake-output.pdf',
    stats: { fonts: 3, expansion_ratio: 1.12, fallback_count: 1 },
  });
  emit({ type: 'run_finished', ok: true, elapsed_ms: 1000 });
}

/** 局部重译：重发段落 + 相关页 page_ready。 */
function emitRetranslate(request) {
  const ids = Array.isArray(request.paragraph_ids) ? request.paragraph_ids : [];
  const pages = new Set();
  for (const id of ids) {
    const previous = translations.get(id) ?? `<p id="${id}"></p>`;
    emitParagraph(id, 'translated', `${previous.replace(/<\/p>$/, '')}（重译）</p>`);
    const page = pageOf(id);
    if (page !== null) pages.add(page);
  }
  for (const page of [...pages].sort((a, b) => a - b)) {
    emitPageReady(page);
  }
}

/** 手动编辑：base_revision 不匹配 → conflict。 */
function emitApplyEdit(request) {
  const base = request.base_revision;
  if (!Number.isInteger(base) || base !== revision) {
    emit({
      type: 'error',
      fatal: false,
      code: 'conflict',
      message: `base_revision=${String(base)} 与当前 revision=${revision} 不符`,
    });
    return;
  }
  const id = String(request.paragraph_id ?? '');
  emitParagraph(id, 'typeset', String(request.translated_html ?? ''));
  const page = pageOf(id);
  if (page !== null) emitPageReady(page);
}

async function main() {
  const rl = readline.createInterface({ input: process.stdin });
  let runStarted = false;
  const runFinished = once(process.stdout, 'drain').catch(() => undefined);

  rl.on('line', (line) => {
    if (cancelled) return;
    const text = line.trim();
    if (text === '') return;
    let request;
    try {
      request = JSON.parse(text);
    } catch {
      process.stderr.write(`fake-sidecar: 无法解析的行：${text.slice(0, 120)}\n`);
      return;
    }
    if (request.type === 'cancel') {
      // 协议 §9.1：cancel = 取消，引擎收尾退出
      cancelled = true;
      setImmediate(() => process.exit(0));
      return;
    }
    if (request.type === 'configure') {
      // 确认配置（api_key 绝不回显、不写日志）
      emit({ type: 'progress', stage: 'preflight', done: 0, total: 1 });
      return;
    }
    if (request.type === 'run' && !runStarted) {
      runStarted = true;
      emitRun(request).catch((error) => {
        process.stderr.write(`fake-sidecar: ${String(error)}\n`);
        process.exitCode = 1;
      });
      return;
    }
    if (request.type === 'retranslate') {
      emitRetranslate(request);
      return;
    }
    if (request.type === 'apply_edit') {
      emitApplyEdit(request);
      return;
    }
    if (request.type === 'export') {
      const output =
        typeof request.output === 'string'
          ? request.output
          : (currentRun?.output ?? '/tmp/syncpdf-fake-output.pdf');
      emit({
        type: 'document_finished',
        output,
        stats: { fonts: 3, expansion_ratio: 1.12, fallback_count: 1 },
      });
    }
  });

  rl.on('close', async () => {
    // stdin EOF：与 cancel 同语义，立即退出
    cancelled = true;
    await runFinished;
    process.exit(0);
  });
}

main().catch((error) => {
  process.stderr.write(`fake-sidecar: ${String(error)}\n`);
  process.exit(1);
});
