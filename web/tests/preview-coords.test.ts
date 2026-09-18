/**
 * 两套坐标系换算的纯函数单测（契约红线：服务端不转换，换算在前端）。
 *
 * 核心断言：同一样本段落的 **parse（`pdf_topleft`，y 向下）** 与 **layout（`pdf_native`，y 向上）**
 * 框，换算到屏幕必须**完全重合**——样本 `tmp/ccs3764-dyn` 的 P01-001 两个框正好互为 `792 - y` 镜像。
 */
import { describe, expect, it } from 'vitest';

import { cropBoxFromViewport, pdfToScreen } from '../src/components/preview/BboxLayer';
import type { ScreenViewport } from '../src/components/preview/BboxLayer';
import type { GeometryResponse } from '../src/api/types';
import {
  artifactUrl,
  bboxModeForView,
  clampPage,
  geometryBboxes,
  pickPreviewArtifacts,
} from '../src/lib/preview';
import type { ArtifactItem } from '../src/api/types';
import { makeViewport } from './helpers';

/** 真实样本（tmp/ccs3764-dyn，21 页，page 1）：同一段落的两个坐标系框。 */
const P01_PARSE_BOX = [66.585, 78.40999999999997, 544.64, 119.64700000000005] as const;
const P01_LAYOUT_BOX = [66.585, 672.353, 544.64, 713.59] as const;
const LETTER_CROPBOX = { x0: 0, y0: 0, x1: 612, y1: 792 };

function expectRectClose(
  actual: { x: number; y: number; width: number; height: number },
  expected: { x: number; y: number; width: number; height: number },
) {
  expect(actual.x).toBeCloseTo(expected.x, 6);
  expect(actual.y).toBeCloseTo(expected.y, 6);
  expect(actual.width).toBeCloseTo(expected.width, 6);
  expect(actual.height).toBeCloseTo(expected.height, 6);
}

describe('pdfToScreen：parse（pdf_topleft）与 layout（pdf_native）', () => {
  it('cropbox 原点为 0：y 向下直接落到屏幕、layout 的 y 向上被翻转', () => {
    const viewport = makeViewport({ scale: 1 });
    const parse = pdfToScreen([...P01_PARSE_BOX], viewport, 'pdf_topleft', LETTER_CROPBOX);
    const layout = pdfToScreen([...P01_LAYOUT_BOX], viewport, 'pdf_native', LETTER_CROPBOX);

    // 屏幕 y 向下，所以 parse 的 y 直接用（78.41）；layout 需 792 - y
    expectRectClose(parse, { x: 66.585, y: 78.41, width: 478.055, height: 41.237 });
    expectRectClose(layout, parse);
  });

  it('scale 只影响像素大小：1.5× 时坐标与尺寸同乘 1.5', () => {
    const viewport = makeViewport({ scale: 1.5 });
    const parse = pdfToScreen([...P01_PARSE_BOX], viewport, 'pdf_topleft', LETTER_CROPBOX);
    expectRectClose(parse, { x: 99.8775, y: 117.615, width: 717.0825, height: 61.8555 });
  });

  it('cropbox 原点 ≠ 0：两套坐标系各自补偏移后仍重合（偏移在换算里抵消）', () => {
    const viewport = makeViewport({ viewBox: [10, 20, 622, 812] });
    const cropbox = { x0: 10, y0: 20, x1: 622, y1: 812 };
    const parse = pdfToScreen([...P01_PARSE_BOX], viewport, 'pdf_topleft', cropbox);
    const layout = pdfToScreen([...P01_LAYOUT_BOX], viewport, 'pdf_native', cropbox);
    expectRectClose(layout, parse);
    // 与原点 0 的 cropbox 等价（边框随 cropbox 一起平移）
    expectRectClose(parse, { x: 66.585, y: 78.41, width: 478.055, height: 41.237 });
  });

  it('没有 page_info 时用 pdf.js viewBox 当 cropbox（parse 页高 = viewBox[3]-viewBox[1]）', () => {
    const viewport = makeViewport({ scale: 2 });
    const viaViewBox = pdfToScreen([...P01_PARSE_BOX], viewport, 'pdf_topleft');
    expectRectClose(viaViewBox, { x: 133.17, y: 156.82, width: 956.11, height: 82.474 });
  });

  it('cropbox 与 viewBox 都拿不到时退回「原点 0 + 视口高/scale」', () => {
    const viewport: ScreenViewport = {
      width: 612,
      height: 792,
      scale: 1,
      convertToViewportRectangle: (rect) => [rect[0], 792 - rect[3], rect[2], 792 - rect[1]],
    };
    expect(cropBoxFromViewport(viewport)).toEqual({ x0: 0, y0: 0, x1: 612, y1: 792 });
  });

  it('rotation ≠ 0 时交给 viewport 变换（结果仍在视口内，不重复翻转）', () => {
    // rotation=90：viewBox [0,0,612,792] → 视口 792×612
    const viewport: ScreenViewport = {
      width: 792,
      height: 612,
      scale: 1,
      viewBox: [0, 0, 612, 792],
      convertToViewportRectangle: (rect) => [rect[1], 612 - rect[2], rect[3], 612 - rect[0]],
    };
    const rect = pdfToScreen([...P01_LAYOUT_BOX], viewport, 'pdf_native', LETTER_CROPBOX);
    expect(rect.x).toBeGreaterThanOrEqual(0);
    expect(rect.y).toBeGreaterThanOrEqual(0);
    expect(rect.x + rect.width).toBeLessThanOrEqual(792);
    expect(rect.y + rect.height).toBeLessThanOrEqual(612);
  });

  it('输入框两端颠倒也能归一成同一屏幕矩形', () => {
    const viewport = makeViewport({ scale: 1 });
    const normal = pdfToScreen([...P01_PARSE_BOX], viewport, 'pdf_topleft', LETTER_CROPBOX);
    const flipped = pdfToScreen(
      [P01_PARSE_BOX[2], P01_PARSE_BOX[3], P01_PARSE_BOX[0], P01_PARSE_BOX[1]],
      viewport,
      'pdf_topleft',
      LETTER_CROPBOX,
    );
    expectRectClose(flipped, normal);
  });
});

/** parse 快照响应（真实字段形状；page 1 只留一条）。 */
function parseResponse(): GeometryResponse {
  return {
    did: 'ccs3764-dyn',
    kind: 'parse',
    coord_system: 'pdf_topleft',
    page: 1,
    run_id: '20260916T132829Z-000183',
    entities: [
      {
        id: 'P01-001',
        kind: 'paragraph',
        label: 'title',
        page: 1,
        box: { x0: 66.585, y0: 78.41, x1: 544.64, y1: 119.647 },
        attrs: { unicode: 'Merge at Your Own Risk?', replayed: true },
      },
      { id: 'broken', kind: 'paragraph', label: 'text', page: 1 },
    ],
    relations: [],
    paragraphs: [],
    page_info: [],
  };
}

/** layout 几何响应（真实字段形状；`layout_box` 是版面归属框）。 */
function layoutResponse(): GeometryResponse {
  return {
    did: 'ccs3764-dyn',
    kind: 'layout',
    coord_system: 'pdf_native',
    page: 1,
    pages: 21,
    paragraphs: [
      {
        id: 'P01-001',
        page: 1,
        layout_label: 'title',
        src_box: [66.585, 672.353, 544.64, 713.59],
        layout_box: [66.585, 672.353, 544.64, 713.59],
        rendered_box: [66.585, 696.375, 410.893, 713.59],
        scale: 1,
        n_lines: 1,
        text: '自行合并，风险自负？',
      },
      { id: 'no-box', page: 1, layout_label: 'text' },
    ],
    page_info: [{ page: 1, cropbox: [0, 0, 612, 792], layout_regions: [] }],
    entities: [],
    relations: [],
  };
}

describe('geometryBboxes：响应 → bbox 输入（不换算）', () => {
  it('parse：读 entities 的 box 对象，缺 box 的行跳过；没有 page_info → cropbox null', () => {
    const parsed = geometryBboxes(parseResponse());
    expect(parsed.coordSystem).toBe('pdf_topleft');
    expect(parsed.cropbox).toBeNull();
    expect(parsed.boxes).toEqual([
      { id: 'P01-001', box: [66.585, 78.41, 544.64, 119.647], label: 'title' },
    ]);
  });

  it('layout：读 paragraphs 的 layout_box（不是 src_box/rendered_box）并带 page_info cropbox', () => {
    const parsed = geometryBboxes(layoutResponse());
    expect(parsed.coordSystem).toBe('pdf_native');
    expect(parsed.cropbox).toEqual({ x0: 0, y0: 0, x1: 612, y1: 792 });
    expect(parsed.boxes).toEqual([
      { id: 'P01-001', box: [66.585, 672.353, 544.64, 713.59], label: 'title' },
    ]);
  });

  it('两套响应经 pdfToScreen 后落到同一屏幕矩形（端到端换算闭环）', () => {
    const viewport = makeViewport({ scale: 1 });
    const parsed = geometryBboxes(parseResponse());
    const layouted = geometryBboxes(layoutResponse());
    const parseRect = pdfToScreen(
      parsed.boxes[0].box,
      viewport,
      parsed.coordSystem,
      parsed.cropbox,
    );
    const layoutRect = pdfToScreen(
      layouted.boxes[0].box,
      viewport,
      layouted.coordSystem,
      layouted.cropbox,
    );
    expectRectClose(parseRect, layoutRect);
  });
});

describe('预览产物与页/模式映射', () => {
  const artifact = (name: string, kind: ArtifactItem['kind'] = 'pdf'): ArtifactItem => ({
    name,
    path: name,
    kind,
    size: 1,
    mtime: '2026-09-15T17:03:14.105Z',
  });

  it('译文产物 mono 首选 → dual 次之 → 其他 pdf；source.pdf 单独识别', () => {
    const dual = artifact('output/paper.no_watermark.zh.dual.pdf');
    const mono = artifact('output/paper.no_watermark.zh.mono.pdf');
    const source = artifact('source.pdf', 'source');
    expect(pickPreviewArtifacts([dual, mono, source])).toEqual({ target: mono, source });
    expect(pickPreviewArtifacts([dual])).toEqual({ target: dual, source: null });
    expect(pickPreviewArtifacts([source])).toEqual({ target: null, source });
    expect(pickPreviewArtifacts([artifact('output/other.pdf')]).target?.name).toBe(
      'output/other.pdf',
    );
    expect(pickPreviewArtifacts([])).toEqual({ target: null, source: null });
    expect(pickPreviewArtifacts([artifact('preview/run-1.pdf')])).toEqual({ target: null, source: null });
  });

  it('artifactUrl 逐段 encode 且保留 `/`（Range 下载键）', () => {
    expect(artifactUrl('ccs3764-dyn', 'output/paper #1.mono.pdf')).toBe(
      '/api/v1/documents/ccs3764-dyn/artifacts/output/paper%20%231.mono.pdf',
    );
    expect(artifactUrl('a b', 'source.pdf')).toBe('/api/v1/documents/a%20b/artifacts/source.pdf');
  });

  it('clampPage 与视图默认 bbox 模式', () => {
    expect(clampPage(0, 21)).toBe(1);
    expect(clampPage(30, 21)).toBe(21);
    expect(clampPage(2.6, 21)).toBe(3);
    expect(clampPage(Number.NaN, 21)).toBe(1);
    expect(bboxModeForView('progress')).toBe('parse');
    expect(bboxModeForView('layout')).toBe('parse');
    expect(bboxModeForView('translate')).toBe('layout');
    expect(bboxModeForView('check')).toBe('parse');
  });
});
