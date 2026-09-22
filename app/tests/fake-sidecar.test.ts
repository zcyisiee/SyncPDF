/**
 * fake-sidecar 自身行为测试（协议正确性：JSONL / seq 单调 / cancel）。
 */
import { describe, expect, it } from 'vitest';
import { spawn } from 'node:child_process';
import { createInterface } from 'node:readline';
import { join } from 'node:path';
import { once } from 'node:events';
import { isEngineEvent } from '../src/shared/protocol';

const FAKE_SIDECAR = join(__dirname, '..', 'scripts', 'fake-sidecar.mjs');

interface RunResult {
  events: unknown[];
  code: number | null;
}

/** 跑完整个 fake-sidecar 会话：requests 全部写入后关 stdin 等退出。 */
function runFakeSidecar(requests: string[], closeAfterMs?: number): Promise<RunResult> {
  return new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [FAKE_SIDECAR]);
    const events: unknown[] = [];
    const rl = createInterface({ input: child.stdout });
    rl.on('line', (line) => {
      if (line.trim() === '') return;
      events.push(JSON.parse(line));
    });
    child.on('error', reject);
    child.on('exit', (code) => resolve({ events, code }));
    for (const request of requests) {
      child.stdin.write(`${request}\n`);
    }
    if (closeAfterMs !== undefined) {
      setTimeout(() => child.stdin.end(), closeAfterMs);
    } else {
      child.stdin.end();
    }
  });
}

describe('fake-sidecar', () => {
  it('只发 configure 不 run：EOF 退出，1 个确认事件', async () => {
    const result = await runFakeSidecar([
      JSON.stringify({
        type: 'configure',
        provider: 'openai_compatible',
        base_url: 'x',
        model: 'm',
        api_key: 'k',
        concurrency: 1,
        cache_dir: '/tmp',
      }),
    ]);
    expect(result.code).toBe(0);
    // configure 确认：一条 preflight progress
    expect(result.events).toHaveLength(1);
    expect((result.events[0] as { type: string }).type).toBe('progress');
  });

  it('完整事件流通过 isEngineEvent 且 seq 从 1 单调', async () => {
    const configure = {
      type: 'configure',
      provider: 'openai_compatible',
      base_url: 'x',
      model: 'm',
      api_key: 'k',
      concurrency: 1,
      cache_dir: '/tmp',
    };
    const run = {
      type: 'run',
      doc_id: 'd',
      input: '/tmp/in.pdf',
      output: '/tmp/out.pdf',
      source_lang: 'en',
      target_lang: 'zh',
      mode: 'full',
    };
    // stdin 保持打开直到跑完（sidecar 长驻：stdin 开着不会自然退出），
    // 跑完后发 cancel 收尾退出
    const result = await new Promise<RunResult>((resolve, reject) => {
      const child = spawn(process.execPath, [FAKE_SIDECAR]);
      const events: unknown[] = [];
      const rl = createInterface({ input: child.stdout });
      rl.on('line', (line) => {
        if (line.trim() === '') return;
        events.push(JSON.parse(line));
        // 收到 run_finished 即发 cancel 让子进程退出
        if ((JSON.parse(line) as { type: string }).type === 'run_finished') {
          child.stdin.write(`${JSON.stringify({ type: 'cancel' })}\n`);
        }
      });
      child.on('error', reject);
      child.on('exit', (code) => resolve({ events, code }));
      child.stdin.write(`${JSON.stringify(configure)}\n`);
      child.stdin.write(`${JSON.stringify(run)}\n`);
    });
    expect(result.code).toBe(0);
    expect(result.events.length).toBeGreaterThanOrEqual(30);
    let lastSeq = 0;
    for (const event of result.events) {
      expect(isEngineEvent(event), JSON.stringify(event).slice(0, 80)).toBe(true);
      const seq = (event as { seq: number }).seq;
      expect(seq).toBe(lastSeq + 1);
      lastSeq = seq;
    }
    // 结尾两个事件
    const tail = result.events.slice(-2).map((event) => (event as { type: string }).type);
    expect(tail).toEqual(['document_finished', 'run_finished']);
  });

  it('cancel：正在 run 时发 cancel → 立即退出，不再发后续事件', async () => {
    const configure = JSON.stringify({
      type: 'configure',
      provider: 'openai_compatible',
      base_url: 'x',
      model: 'm',
      api_key: 'k',
      concurrency: 1,
      cache_dir: '/tmp',
    });
    const run = JSON.stringify({
      type: 'run',
      doc_id: 'd',
      input: '/tmp/in.pdf',
      output: '/tmp/out.pdf',
      source_lang: 'en',
      target_lang: 'zh',
      mode: 'full',
    });
    const result = await new Promise<RunResult>((resolve, reject) => {
      const child = spawn(process.execPath, [FAKE_SIDECAR]);
      const events: unknown[] = [];
      const rl = createInterface({ input: child.stdout });
      rl.on('line', (line) => {
        if (line.trim() === '') return;
        events.push(JSON.parse(line));
      });
      child.on('error', reject);
      child.on('exit', (code) => resolve({ events, code }));
      child.stdin.write(`${configure}\n`);
      child.stdin.write(`${run}\n`);
      // 收到若干事件后发 cancel
      const timer = setTimeout(() => {
        child.stdin.write(`${JSON.stringify({ type: 'cancel' })}\n`);
      }, 700);
      void once(child, 'exit').then(() => clearTimeout(timer));
    });
    expect(result.code).toBe(0);
    // cancel 后没有 run_finished（未跑完就退出了）
    const types = result.events.map((event) => (event as { type: string }).type);
    expect(types).not.toContain('run_finished');
    expect(types).not.toContain('document_finished');
  });

  it('run 事件序列含 page_ready 恰好 12 次（每页一次、preview_path=null、页号 1..12）', async () => {
    const result = await new Promise<RunResult>((resolve, reject) => {
      const child = spawn(process.execPath, [FAKE_SIDECAR]);
      const events: unknown[] = [];
      const rl = createInterface({ input: child.stdout });
      rl.on('line', (line) => {
        if (line.trim() === '') return;
        events.push(JSON.parse(line));
        if ((JSON.parse(line) as { type: string }).type === 'run_finished') {
          child.stdin.write(`${JSON.stringify({ type: 'cancel' })}\n`);
        }
      });
      child.on('error', reject);
      child.on('exit', (code) => resolve({ events, code }));
      child.stdin.write(
        `${JSON.stringify({
          type: 'run',
          doc_id: 'd12',
          input: '/tmp/in.pdf',
          output: '/tmp/out.pdf',
          source_lang: 'en',
          target_lang: 'zh',
          mode: 'full',
        })}\n`,
      );
    });
    expect(result.code).toBe(0);

    const pageReady = result.events.filter(
      (event) => (event as { type: string }).type === 'page_ready',
    );
    // 12 页，每页恰好一次
    expect(pageReady).toHaveLength(12);
    const pages = pageReady.map((event) => (event as { page: number }).page);
    expect(pages).toEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12]);
    // preview_path 显式 null（就地重写 output，无独立预览产物）
    for (const ready of pageReady) {
      expect((ready as { preview_path: unknown }).preview_path).toBeNull();
    }
    // 所有事件（含 page_ready）都过协议判别
    for (const event of result.events) {
      expect(isEngineEvent(event), JSON.stringify(event).slice(0, 80)).toBe(true);
    }

    // page_ready 之前该页段落都已发出（译文栏刷新时数据齐备）
    const types = result.events.map((event) => (event as { type: string }).type);
    for (let page = 1; page <= 12; page += 1) {
      // 找该页的 page_ready（第 page 次出现的 page_ready）
      let seen = 0;
      let readyAt = -1;
      for (let i = 0; i < types.length; i += 1) {
        if (types[i] === 'page_ready') {
          seen += 1;
          if (seen === page) {
            readyAt = i;
            break;
          }
        }
      }
      expect(readyAt).toBeGreaterThanOrEqual(0);
      // 该页 3 段都在它前面
      for (let index = 1; index <= 3; index += 1) {
        const id = `P${String(page).padStart(2, '0')}-${String(index).padStart(3, '0')}`;
        const paragraphEvents = result.events.filter(
          (event) =>
            (event as { type: string }).type === 'paragraph' &&
            (event as { paragraph_id: string }).paragraph_id === id,
        );
        expect(paragraphEvents).toHaveLength(1);
        const paragraphAt = result.events.indexOf(paragraphEvents[0]);
        expect(paragraphAt).toBeLessThan(readyAt);
      }
    }
  });

  it('apply_edit：base_revision 匹配 → paragraph + page_ready；不匹配 → error{conflict}', async () => {
    const run = JSON.stringify({
      type: 'run',
      doc_id: 'd-edit',
      input: '/tmp/in.pdf',
      output: '/tmp/out.pdf',
      source_lang: 'en',
      target_lang: 'zh',
      mode: 'full',
    });
    const result = await new Promise<RunResult>((resolve, reject) => {
      const child = spawn(process.execPath, [FAKE_SIDECAR]);
      const events: unknown[] = [];
      const rl = createInterface({ input: child.stdout });
      let finished = false;
      rl.on('line', (line) => {
        if (line.trim() === '') return;
        const event = JSON.parse(line);
        events.push(event);
        if (event.type === 'run_finished' && !finished) {
          finished = true;
          // run 结束时 fake-sidecar 的 revision = 12(page_ready) + 1(document_finished) = 13。
          // 先发一条 base_revision 过期的（=12，已过期）→ conflict；
          // 再发一条匹配的（=13）→ paragraph + page_ready。
          child.stdin.write(
            `${JSON.stringify({
              type: 'apply_edit',
              doc_id: 'd-edit',
              paragraph_id: 'P01-001',
              translated_html: '<p>手改</p>',
              base_revision: 12,
            })}\n`,
          );
          child.stdin.write(
            `${JSON.stringify({
              type: 'apply_edit',
              doc_id: 'd-edit',
              paragraph_id: 'P01-001',
              translated_html: '<p>手改</p>',
              base_revision: 13,
            })}\n`,
          );
          // 等两条回执都到了再退出
          setTimeout(() => {
            child.stdin.write(`${JSON.stringify({ type: 'cancel' })}\n`);
          }, 200);
        }
      });
      child.on('error', reject);
      child.on('exit', (code) => resolve({ events, code }));
      child.stdin.write(`${run}\n`);
    });
    expect(result.code).toBe(0);

    const after = result.events.filter((event) => {
      const index = result.events.indexOf(event);
      const runFinishedAt = result.events.findIndex(
        (item) => (item as { type: string }).type === 'run_finished',
      );
      return index > runFinishedAt;
    });
    const types = after.map((event) => (event as { type: string }).type);
    // 过期 base_revision：conflict；匹配：paragraph + page_ready（共 3 条回执）
    expect(types).toEqual(['error', 'paragraph', 'page_ready']);
    expect((after[0] as { code: string }).code).toBe('conflict');
    const paragraph = after[1] as { paragraph_id: string; translated_html: string; status: string };
    expect(paragraph.paragraph_id).toBe('P01-001');
    expect(paragraph.translated_html).toBe('<p>手改</p>');
    expect((after[2] as { page: number }).page).toBe(1);
    expect((after[2] as { preview_path: unknown }).preview_path).toBeNull();
  });

  it('retranslate：重发段落 + 相关页 page_ready', async () => {
    const run = JSON.stringify({
      type: 'run',
      doc_id: 'd-rt',
      input: '/tmp/in.pdf',
      output: '/tmp/out.pdf',
      source_lang: 'en',
      target_lang: 'zh',
      mode: 'full',
    });
    const result = await new Promise<RunResult>((resolve, reject) => {
      const child = spawn(process.execPath, [FAKE_SIDECAR]);
      const events: unknown[] = [];
      const rl = createInterface({ input: child.stdout });
      let finished = false;
      rl.on('line', (line) => {
        if (line.trim() === '') return;
        const event = JSON.parse(line);
        events.push(event);
        if (event.type === 'run_finished' && !finished) {
          finished = true;
          child.stdin.write(
            `${JSON.stringify({
              type: 'retranslate',
              doc_id: 'd-rt',
              paragraph_ids: ['P02-001', 'P05-002'],
            })}\n`,
          );
          setTimeout(() => {
            child.stdin.write(`${JSON.stringify({ type: 'cancel' })}\n`);
          }, 200);
        }
      });
      child.on('error', reject);
      child.on('exit', (code) => resolve({ events, code }));
      child.stdin.write(`${run}\n`);
    });
    expect(result.code).toBe(0);

    const runFinishedAt = result.events.findIndex(
      (item) => (item as { type: string }).type === 'run_finished',
    );
    const after = result.events.slice(runFinishedAt + 1).map((event) => event as { type: string; paragraph_id?: string; page?: number });
    // 两段重发 + 两次 page_ready（页 2、5 各一次，升序）
    expect(after.map((event) => event.type)).toEqual([
      'paragraph',
      'paragraph',
      'page_ready',
      'page_ready',
    ]);
    expect(after[0].paragraph_id).toBe('P02-001');
    expect(after[1].paragraph_id).toBe('P05-002');
    expect(after[2].page).toBe(2);
    expect(after[3].page).toBe(5);
  });
});
