#!/usr/bin/env node
/**
 * 假 sidecar（测试与开发用，替代尚不存在的 syncpdf-cli 二进制）。
 *
 * 行为：
 * - 读 stdin JSONL：收到 `configure` 回一个 `progress` 确认（stage=preflight）；
 * - 收到 `run` 后按协议 §9.3 发完整事件流：
 *   run_started → 每阶段 stage_started/stage_finished → 每 200ms 发
 *   progress / paragraph / page_ready（12 页 30 段）→ document_finished → run_finished；
 * - 收到 `cancel` 或 stdin EOF 立即退出（退出码 0）。
 *
 * 输出格式严格按 02-技术路径与架构.md §9.3：每行
 * `{"seq":n,"ts":…,"type":"…",…}`，seq 从 1 单调递增。
 */
import { once } from 'node:events';
import readline from 'node:readline';

const PAGES = 12;
const PARAGRAPHS_PER_PAGE = 3; // 12 页 × 3 段 ≈ 36 ≥ 30 段
const TICK_MS = 200;

let seq = 0;
let cancelled = false;

function emit(event) {
  seq += 1;
  const line = JSON.stringify({ seq, ts: Date.now() / 1000, ...event });
  process.stdout.write(`${line}\n`);
}

function paragraphId(page, index) {
  return `P${String(page).padStart(2, '0')}-${String(index).padStart(3, '0')}`;
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function emitRun(request) {
  const pages = typeof request.pages === 'number' && request.pages > 0 ? request.pages : PAGES;
  emit({
    type: 'run_started',
    protocol_version: 1,
    engine_version: 'fake-sidecar 0.1.0',
    doc_id: request.doc_id,
    pages,
  });

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
      // 翻译阶段逐页逐段发 progress / paragraph / page_ready
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
          emit({
            type: 'paragraph',
            paragraph_id: id,
            status,
            boxes: [
              [72, 72 + i * 14, 540, 84 + i * 14],
            ],
            coord_system: 'pdf_native',
            translated_html: `<p id="${id}">译文段落 ${paragraphIndex}</p>`,
          });
          done += 1;
          await sleep(TICK_MS);
        }
        emit({ type: 'page_ready', page, preview_path: `/tmp/syncpdf-fake-preview-${page}.pdf` });
      }
      emit({ type: 'progress', stage, done, total: totalParagraphs });
    } else if (stage === 'publishing') {
      emit({ type: 'page_ready', page: 1, preview_path: '/tmp/syncpdf-fake-final.pdf' });
    } else if (stage === 'validating') {
      // 演示一条非致命 issue
      emit({
        type: 'issue',
        severity: 'warning',
        code: 'font_missing',
        paragraph_id: paragraphId(1, 1),
        page: 0,
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
    if (request.type === 'retranslate' || request.type === 'apply_edit' || request.type === 'export') {
      // 演示错误事件（编辑冲突）
      emit({
        type: 'error',
        fatal: false,
        code: request.type === 'apply_edit' ? 'conflict' : 'unsupported',
        message: `fake-sidecar 不支持 ${request.type}`,
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
