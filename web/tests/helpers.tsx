import { QueryClientProvider } from '@tanstack/react-query';
import { render } from '@testing-library/react';
import type { ReactElement } from 'react';
import { vi } from 'vitest';

import { createQueryClient } from '../src/app/App';
import type { ScreenViewport } from '../src/components/preview/BboxLayer';
import type { EventFeed } from '../src/components/events/useEventWindow';
import type { RunEvent } from '../src/lib/events';
import type { JobRecord } from '../src/api/types';
import { createUiStore, uiStore } from '../src/stores/ui';

/** 最小 fetch 响应替身：api.ts 只读 ok/status/text()，不依赖全局 Response。 */
export function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body),
  } as unknown as Response;
}

export function textResponse(body: string, status: number): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => body,
  } as unknown as Response;
}

/** 按路径分发 mock fetch；未命中的路径返回 404 错误信封。 */
export function mockApiFetch(routes: Record<string, () => Response | Promise<Response>>) {
  // 参数签名与 `fetch` 对齐（含 init）：调用方要按 method/body 断言（W08 的上传/提交）。
  const fetchMock = vi.fn(
    async (input: RequestInfo | URL, _init?: RequestInit): Promise<Response> => {
      const url = String(input);
      const route = routes[url];
      if (route) return await route();
      return jsonResponse({ error: { code: 'not_found', message: `未 mock 的路径：${url}` } }, 404);
    },
  );
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

/** 组件用的是模块级 store 单例：每个用例前恢复数据字段（保留单例自己的 action）。 */
export function resetUiStore(): void {
  const fresh = createUiStore().getState();
  uiStore.setState({
    screen: 'library',
    viewrailWidth: fresh.viewrailWidth,
    inspectorWidth: fresh.inspectorWidth,
    timelineHeight: fresh.timelineHeight,
    inspectorCollapsed: fresh.inspectorCollapsed,
    previewMode: 'target',
    previewPage: 1,
    previewDid: null,
    bboxMode: 'parse',
    selectedParagraphId: null,
    dragging: null,
  });
}

export function renderWithQuery(ui: ReactElement) {
  return render(<QueryClientProvider client={createQueryClient()}>{ui}</QueryClientProvider>);
}

/**
 * 事件窗口 feed 的替身（W06）：组件只需要这份数据结构，SSE 生命周期由
 * `useEventWindow` 负责（那个用 stub EventSource 单测）。
 */
export function makeEventFeed(overrides: Partial<EventFeed> = {}): EventFeed {
  return {
    events: [],
    runId: null,
    isPending: false,
    hasArchive: true,
    error: null,
    connection: 'open',
    hasEarlier: false,
    isLoadingEarlier: false,
    loadEarlier: () => {},
    retry: () => {},
    ...overrides,
  };
}

/** 一条事件（`{seq, at, stage, kind, data}`）。 */
export function makeEvent(seq: number, overrides: Partial<RunEvent> = {}): RunEvent {
  return {
    seq,
    at: `2026-09-16T13:28:${String(seq % 60).padStart(2, '0')}.000+00:00`,
    stage: 'translate',
    kind: 'call_started',
    data: { attempt: 1 },
    ...overrides,
  };
}

/** pdf.js `PageViewport`（rotation=0）的替身：变换公式照抄 pdf.js 源码
 * （build/pdf.mjs `PageViewport` 构造器，rotation=0 时 `transform = [s,0,0,-s,-s*x0,s*y1]`）。
 * 单测不需要真 pdf.js（jsdom 无 canvas），但公式必须与库一致，否则换算测试自说自话。
 */
export function makeViewport({
  scale = 1,
  viewBox = [0, 0, 612, 792],
}: { scale?: number; viewBox?: [number, number, number, number] } = {}): ScreenViewport {
  const [x0, y0, x1, y1] = viewBox;
  const a = scale;
  const d = -scale;
  const e = -scale * x0;
  const f = scale * y1;
  return {
    width: (x1 - x0) * scale,
    height: (y1 - y0) * scale,
    scale,
    viewBox,
    convertToViewportRectangle(rect) {
      const map = (x: number, y: number) => [a * x + e, d * y + f];
      const [ax, ay] = map(rect[0], rect[1]);
      const [bx, by] = map(rect[2], rect[3]);
      return [ax, ay, bx, by];
    },
  };
}

/**
 * 造一个「像真浏览器里的」PDF File：**补上 jsdom 缺失的 `Blob#arrayBuffer`**
 * （`lib/uploads.ts::readPdfMagic` 用 `file.slice(0,5).arrayBuffer()`，浏览器原生支持，
 * jsdom 26 还没实现）。不补的话单测里每个上传都会被误判成"不是 PDF"。
 */
export function makePdfFile(
  name = 'sample.pdf',
  bytes: number[] = [0x25, 0x50, 0x44, 0x46, 0x2d],
): File {
  const file = new File([new Uint8Array(bytes)], name, { type: 'application/pdf' });
  const slice = file.slice.bind(file);
  file.slice = ((start?: number, end?: number) => {
    const part = slice(start, end);
    Object.defineProperty(part, 'arrayBuffer', {
      value: async () => new Uint8Array(bytes.slice(start ?? 0, end ?? bytes.length)).buffer,
    });
    return part;
  }) as typeof file.slice;
  return file;
}

/** 非 PDF 的 File（魔数不对，用于预检分支）。 */
export function makeTextFile(name = 'notes.pdf', text = 'not a pdf'): File {
  return makePdfFile(name, Array.from(new TextEncoder().encode(text)));
}

/** 一条 job 记录（`GET /documents/{did}/jobs` 的元素；字段与生成的 OpenAPI 类型一致）。 */
export function makeJob(overrides: Partial<JobRecord> = {}): JobRecord {
  return {
    job_id: 'j_01M2RDB312K20Q280DHCTX7N19',
    did: 'alpha',
    action: 'run',
    status: 'queued',
    created_at: '2026-09-17T19:26:20.194Z',
    started_at: null,
    finished_at: null,
    from_stage: 'translate',
    profile: 'echo-t',
    pages: null,
    dual: false,
    run_id: null,
    exit_code: null,
    envelope: null,
    error_code: null,
    error_message: null,
    pid: null,
    pgid: null,
    cancel_requested_at: null,
    boot_id: '50462-1789673180186',
    spawn_marker: '1320e5e8435c2710',
    interrupted_reason: null,
    ...overrides,
  } as JobRecord;
}
