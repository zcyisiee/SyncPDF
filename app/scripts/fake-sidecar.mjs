#!/usr/bin/env node
/**
 * 假 sidecar（测试与无引擎开发用），语义对齐 `syncpdf-cli run --protocol 1` 长驻会话：
 *
 * - `configure` 静默生效（可重复，后者覆盖）；
 * - `run` 进串行队列，逐个执行：先把 `input` 复制到 `output`（译文栏立即可渲染），
 *   再发 run_started → 各阶段 stage_started/stage_finished；layout_analysis 按页发 `layout`，
 *   paragraph_analysis 发 `doc_meta`，translating 逐段发 progress / paragraph、
 *   每页末尾 page_ready → document_finished → run_finished；
 * - `cancel` 取消当前 run（发 `run_finished{ok:false}`），队列继续；
 * - stdin EOF：取消当前 run，排队中的 run 也以 `ok:false` 收尾，然后退出码 0；
 * - retranslate / apply_edit / export：与真引擎一致，回 `error{code:"unsupported_request"}`。
 *
 * 环境变量 `FAKE_SIDECAR_TICK_MS` 控制每段间隔（默认 200ms）。
 */
import { copyFile } from 'node:fs/promises';
import readline from 'node:readline';

const PAGES = 12;
const PARAGRAPHS_PER_PAGE = 3;
const TICK_MS = Number.parseInt(process.env.FAKE_SIDECAR_TICK_MS ?? '200', 10);
const STAGES = [
  'preflight',
  'source_analysis',
  'layout_analysis',
  'paragraph_analysis',
  'translating',
  'typesetting',
  'validating',
  'publishing',
];
/** 每页三段的版面类型与位置（pdf_user：左下原点、y 向上）。 */
const PAGE_BLOCKS = [
  { kind: 'paragraph_title', top: 740, height: 20 },
  { kind: 'text', top: 700, height: 120 },
  { kind: 'formula', top: 560, height: 40 },
];

let seq = 0;
let configured = false;
let eof = false;
/** 当前 run 的取消标记（每个 run 一个对象）。 */
let current = null;
const queue = [];

/** 刷完 stdout 再退出（macOS 上管道写是异步的，直接 exit 会丢尾部事件）。 */
const exitClean = () => process.stdout.write('', () => process.exit(0));

function emit(event) {
  seq += 1;
  process.stdout.write(`${JSON.stringify({ seq, ts: Date.now() / 1000, ...event })}\n`);
}

const paragraphId = (page, index) =>
  `P${String(page).padStart(2, '0')}-${String(index).padStart(3, '0')}`;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

function rect(block) {
  return { x0: 56, y0: block.top - block.height, x1: 540, y1: block.top };
}

async function seedOutput({ input, output }) {
  if (typeof input !== 'string' || typeof output !== 'string' || input === output) return;
  try {
    await copyFile(input, output);
  } catch (error) {
    // 纯协议测试里源文件不存在，不阻断事件流
    process.stderr.write(`fake-sidecar: 复制 input→output 失败：${String(error)}\n`);
  }
}

/** 执行一个 run；取消时提前以 `run_finished{ok:false}` 收尾。 */
async function executeRun(request, token) {
  const started = Date.now();
  const finish = (ok) => emit({ type: 'run_finished', ok, elapsed_ms: Date.now() - started });
  emit({
    type: 'run_started',
    protocol_version: 1,
    engine_version: 'fake-sidecar 0.2.0',
    doc_id: String(request.doc_id),
    pages: PAGES,
  });
  if (!configured) {
    emit({ type: 'error', fatal: true, code: 'invalid_request', message: 'run 之前必须先给 configure' });
    return finish(false);
  }
  await seedOutput(request);
  let fallbacks = 0;
  for (const stage of STAGES) {
    if (token.cancelled) return finish(false);
    emit({ type: 'stage_started', stage });
    const stageStart = Date.now();
    if (stage === 'layout_analysis') {
      for (let page = 1; page <= PAGES; page += 1) {
        const regions = PAGE_BLOCKS.map((block) => ({ kind: block.kind, inline: false, bbox: rect(block) }));
        // 正文里的一个行内公式
        regions.push({ kind: 'formula', inline: true, bbox: { x0: 200, y0: 640, x1: 260, y1: 652 } });
        emit({ type: 'layout', page, regions });
      }
    } else if (stage === 'paragraph_analysis') {
      emit({ type: 'doc_meta', title: 'A Fake Paper for Protocol Testing', authors: 'Ada Lovelace, Alan Turing' });
    } else if (stage === 'translating') {
      const total = PAGES * PARAGRAPHS_PER_PAGE;
      let done = 0;
      for (let page = 1; page <= PAGES; page += 1) {
        for (let i = 1; i <= PARAGRAPHS_PER_PAGE; i += 1) {
          if (token.cancelled) return finish(false);
          const id = paragraphId(page, i);
          const block = PAGE_BLOCKS[i - 1];
          const fallback = page === 2 && i === 2;
          if (fallback) fallbacks += 1;
          emit({ type: 'progress', stage, done, total });
          emit({
            type: 'paragraph',
            paragraph_id: id,
            page,
            status: fallback ? 'fallback' : 'typeset',
            boxes: [rect(block)],
            coord_system: 'pdf_user',
            translated_html: fallback ? null : `<p>译文段落 ${id}</p>`,
            kind: block.kind,
            source_text: `Source paragraph ${id}.`,
          });
          if (fallback) {
            emit({
              type: 'issue',
              severity: 'warning',
              code: 'fallback',
              paragraph_id: id,
              page,
              message: '译文放不下，保留原文（fake-sidecar 演示）',
            });
          }
          done += 1;
          await sleep(TICK_MS);
        }
        emit({ type: 'page_ready', page, preview_path: null, revision: page });
      }
      emit({ type: 'progress', stage, done, total });
    }
    emit({ type: 'stage_finished', stage, elapsed_ms: Date.now() - stageStart });
  }
  emit({
    type: 'document_finished',
    output: String(request.output),
    stats: { fonts: 2, expansion_ratio: 1.12, fallbacks },
  });
  return finish(true);
}

async function drain() {
  while (queue.length > 0) {
    const request = queue.shift();
    current = { cancelled: eof };
    await executeRun(request, current);
    current = null;
  }
  if (eof) exitClean();
}

function handle(request) {
  switch (request.type) {
    case 'configure':
      configured = true;
      return;
    case 'cancel':
      if (current !== null) current.cancelled = true;
      return;
    case 'run':
      queue.push(request);
      if (current === null && queue.length === 1) void drain();
      return;
    default:
      emit({
        type: 'error',
        fatal: false,
        code: 'unsupported_request',
        message: `暂不支持的请求：${String(request.type)}`,
      });
  }
}

const rl = readline.createInterface({ input: process.stdin });
rl.on('line', (line) => {
  const text = line.trim();
  if (text === '') return;
  try {
    handle(JSON.parse(text));
  } catch {
    process.stderr.write(`fake-sidecar: 无法解析的行：${text.slice(0, 120)}\n`);
  }
});
rl.on('close', () => {
  eof = true;
  if (current !== null) current.cancelled = true;
  if (current === null && queue.length === 0) exitClean();
});
