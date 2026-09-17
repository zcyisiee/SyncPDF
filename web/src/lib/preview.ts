/**
 * 预览区的纯数据helper（无 React、无 pdf.js）：产物选择、geometry 响应 → bbox 输入、页与模式映射。
 * 坐标换算不在这里，在 `components/preview/BboxLayer.tsx` 的 `pdfToScreen`。
 */
import type { ArtifactItem, GeometryResponse } from '../api/types';
import type { WorkbenchView } from './routing';
import { API_BASE } from './api';

/** bbox 图层三态：识别框（parse 快照）/ 版面框（layout 几何）/ 关。 */
export type BboxMode = 'parse' | 'layout' | 'off';

/** geometry 的坐标系标注（api.md §3.1：服务端不转换，换算在前端）。 */
export type CoordSystem = 'pdf_topleft' | 'pdf_native';

/** bbox 输入框：`[x0, y0, x1, y1]`，含义由 `CoordSystem` 决定。 */
export type Box = [number, number, number, number];

/** 页面 cropbox（PDF 点，左下原点、y 向上）；用于把 bbox 输入坐标换算到 PDF user space。 */
export interface CropBox {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

export interface BboxItem {
  id: string;
  box: Box;
  /** parse 的 label（title/text/figure…）或 layout 的 layout_label；缺 → null。 */
  label: string | null;
}

export interface GeometryBboxes {
  coordSystem: CoordSystem;
  /** 该页 cropbox（来自 `page_info`）；parse 快照不带 → null（按原点 0 + 视图页高处理）。 */
  cropbox: CropBox | null;
  boxes: BboxItem[];
}

export interface PreviewArtifacts {
  /** 译文预览用产物（mono 首选 → dual → 任一 output/*.pdf；都没有 → null）。 */
  target: ArtifactItem | null;
  /** 原文预览用产物（`source.pdf`，kind=source）；workdir 没有 → null。 */
  source: ArtifactItem | null;
}

/** 预览 bbox 的默认模式（brief：只用 `translate` 看译文排版，其余看识别框）。 */
export function bboxModeForView(view: WorkbenchView): BboxMode {
  return view === 'translate' ? 'layout' : 'parse';
}

/** bbox 图层模式 → 坐标系标注（唯一映射点：`parse` 永远配 `pdf_topleft`）。 */
export function coordSystemOfMode(mode: Exclude<BboxMode, 'off'>): CoordSystem {
  return mode === 'layout' ? 'pdf_native' : 'pdf_topleft';
}

/** mono 首选、dual 次之，最后任一产物 PDF（名字排序保证确定性）；无产物 → 双 null。 */
export function pickPreviewArtifacts(artifacts: readonly ArtifactItem[]): PreviewArtifacts {
  const pdfs = artifacts
    .filter((artifact) => artifact.kind === 'pdf')
    .sort((left, right) => left.name.localeCompare(right.name));
  const target =
    pdfs.find((artifact) => artifact.name.endsWith('.mono.pdf')) ??
    pdfs.find((artifact) => artifact.name.endsWith('.dual.pdf')) ??
    pdfs[0] ??
    null;
  const source = artifacts.find((artifact) => artifact.kind === 'source') ?? null;
  return { target, source };
}

/** 产物下载/预览 URL（逐段 encode 保留 `/`；pdf.js 直接用它发 Range 请求）。 */
export function artifactUrl(did: string, name: string): string {
  const path = name.split('/').map(encodeURIComponent).join('/');
  return `${API_BASE}/documents/${encodeURIComponent(did)}/artifacts/${path}`;
}

export function clampPage(page: number, pageCount: number): number {
  if (!Number.isFinite(page)) return 1;
  return Math.min(Math.max(Math.round(page), 1), Math.max(pageCount, 1));
}

function asNumber(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null;
}

function asString(value: unknown): string | null {
  return typeof value === 'string' && value !== '' ? value : null;
}

function asBox(value: unknown): Box | null {
  if (!Array.isArray(value) || value.length !== 4) return null;
  const numbers = value.map(asNumber);
  if (numbers.some((item) => item === null)) return null;
  return numbers as Box;
}

function cropboxOf(response: GeometryResponse): CropBox | null {
  const pageInfo = response.page_info;
  if (pageInfo === undefined || pageInfo.length === 0) return null;
  const box = asBox(pageInfo[0].cropbox);
  if (box === null) return null;
  const [x0, y0, x1, y1] = box;
  return { x0, y0, x1, y1 };
}

/** parse 快照实体 → bbox（`box` 是 `{x0,y0,x1,y1}`，`pdf_topleft`）；形状不对的实体跳过。 */
function entityBbox(row: Record<string, unknown>): BboxItem | null {
  const id = asString(row.id);
  const raw = row.box;
  if (id === null || typeof raw !== 'object' || raw === null) return null;
  const box = raw as Record<string, unknown>;
  const boxValue = asBox([box.x0, box.y0, box.x1, box.y1]);
  if (boxValue === null) return null;
  return { id, box: boxValue, label: asString(row.label) };
}

/**
 * layout 几何行 → bbox。取 `layout_box`：它是该段的版面归属框（api.md §3.3 草稿
 * `layout.box` 覆盖的正是它），`src_box` 是源框、`rendered_box` 是实际落笔范围，
 * 本任务的「段落框」对齐前者。
 */
function paragraphBbox(row: Record<string, unknown>): BboxItem | null {
  const id = asString(row.id);
  const box = asBox(row.layout_box);
  if (id === null || box === null) return null;
  return { id, box, label: asString(row.layout_label) };
}

/**
 * `GET /geometry` 响应 → 该页 bbox 列表。`kind`/`coord_system` 由服务端标注，本函数
 * 只按 `coord_system` 选字段（parse=`entities`，layout=`paragraphs`），**不做换算**。
 */
export function geometryBboxes(response: GeometryResponse): GeometryBboxes {
  const coordSystem = response.coord_system;
  const rows =
    (coordSystem === 'pdf_topleft' ? response.entities : response.paragraphs) ?? [];
  const toBbox = coordSystem === 'pdf_topleft' ? entityBbox : paragraphBbox;
  const boxes: BboxItem[] = [];
  for (const row of rows) {
    const item = toBbox(row);
    if (item !== null) boxes.push(item);
  }
  return { coordSystem, cropbox: cropboxOf(response), boxes };
}
