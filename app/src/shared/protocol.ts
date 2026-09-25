/**
 * SyncPDF JSONL 协议类型。以 Rust `syncpdf-protocol`（request.rs / event.rs）与
 * `syncpdf-core`（geom.rs / ir.rs）的 serde 形状为准，手写镜像 + 运行时守卫。
 *
 * - 请求：主进程 → 引擎 stdin，每行一个 JSON 对象，`type` 做 tag；
 * - 事件：引擎 stdout → 主进程 → 渲染进程，`{seq, ts, type, ...}`。
 */

// ---------- 基础（syncpdf-core） ----------

/** 段落 id：`P{page:02}-{seq:03}`，页号 1 基。 */
export type ParagraphId = string;

/** 坐标系标注：引擎只标注不转换，前端 `geometry.boxToViewRect` 是唯一换算点。 */
export type CoordSystem = 'pdf_user' | 'image_top_left';

/** `syncpdf_core::Rect`：归一化角点（x0<=x1, y0<=y1）。 */
export interface Rect {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
}

/** `RegionKind`（PP-DocLayout 25 类归并后的 15 类）。 */
export const REGION_KINDS = [
  'text',
  'title',
  'paragraph_title',
  'list',
  'caption',
  'table',
  'figure',
  'formula',
  'header',
  'footer',
  'foot_note',
  'reference',
  'code',
  'abstract',
  'other',
] as const;
export type RegionKind = (typeof REGION_KINDS)[number];

/** 段落状态（`not_replaced` 是落定，不是失败）。 */
export type ParagraphStatus = 'pending' | 'translated' | 'typeset' | 'not_replaced' | 'fallback';

// ---------- 请求（request.rs） ----------

export type TranslateProvider = 'http' | 'pi' | 'agy';

export type TranslatorKind =
  | { kind: 'pi'; program: string; model: string; thinking: string }
  | { kind: 'agy'; program: string; model: string }
  | { kind: 'fake'; name: string }
  | { kind: 'http' };

export type Mode = 'full' | 'bilingual';

/** 首条请求。api_key 只在此出现，绝不写日志。 */
export interface ConfigureRequest {
  type: 'configure';
  provider: TranslateProvider;
  base_url: string | null;
  model: string;
  api_key: string | null;
  concurrency: number;
  cache_dir: string;
  translator: TranslatorKind;
}

export interface RunRequest {
  type: 'run';
  doc_id: string;
  input: string;
  output: string;
  source_lang: string;
  target_lang: string;
  /** 页子集（引擎内部 0 基）；null = 全部页。 */
  pages: number[] | null;
  font_profile: string | null;
  /** 规范化术语对 JSON 文件路径（`[["source","target"],...]`）。 */
  terminology: string | null;
  mode: Mode;
  /** 本篇阶段缓存库路径；null = 引擎默认的共享临时库。 */
  store: string | null;
}

export interface RetranslateRequest {
  type: 'retranslate';
  doc_id: string;
  paragraph_ids: ParagraphId[];
}

export interface ApplyEditRequest {
  type: 'apply_edit';
  doc_id: string;
  paragraph_id: ParagraphId;
  translated_html: string;
  base_revision: number;
}

export interface ExportRequest {
  type: 'export';
  doc_id: string;
  output: string;
  mode: Mode;
}

export interface CancelRequest {
  type: 'cancel';
}

export type Request =
  | ConfigureRequest
  | RunRequest
  | RetranslateRequest
  | ApplyEditRequest
  | ExportRequest
  | CancelRequest;

// ---------- 事件（event.rs） ----------

export interface EventEnvelope {
  seq: number;
  ts: number;
}

export const STAGES = [
  'preflight',
  'source_analysis',
  'layout_analysis',
  'paragraph_analysis',
  'translating',
  'typesetting',
  'validating',
  'publishing',
] as const;
export type Stage = (typeof STAGES)[number];

export type Severity = 'info' | 'warning' | 'error';

export interface Stats {
  fonts: number;
  expansion_ratio: number;
  fallbacks: number;
}

/** 版面区域（layout_analysis 后按页发出）。 */
export interface LayoutRegion {
  kind: RegionKind;
  /** 公式区域位于可译区域内（行内公式，按 KEEP 原子处理）。 */
  inline: boolean;
  bbox: Rect;
}

export type EngineEventBody =
  | {
      type: 'run_started';
      protocol_version: number;
      engine_version: string;
      doc_id: string;
      pages: number;
    }
  | { type: 'stage_started'; stage: Stage }
  | { type: 'stage_finished'; stage: Stage; elapsed_ms: number }
  | { type: 'progress'; stage: Stage; done: number; total: number }
  | { type: 'layout'; page: number; regions: LayoutRegion[] }
  | { type: 'doc_meta'; title: string | null; authors: string | null }
  | {
      type: 'paragraph';
      paragraph_id: ParagraphId;
      page: number;
      status: ParagraphStatus;
      /** null = 未识别；[] = 识别了但无框。 */
      boxes: Rect[] | null;
      coord_system: CoordSystem;
      translated_html: string | null;
      kind: RegionKind;
      source_text: string;
    }
  | { type: 'page_ready'; page: number; preview_path: string | null; revision: number }
  | {
      type: 'issue';
      severity: Severity;
      code: string;
      paragraph_id: ParagraphId | null;
      page: number | null;
      message: string;
    }
  | { type: 'document_finished'; output: string; stats: Stats }
  | { type: 'run_finished'; ok: boolean; elapsed_ms: number }
  | { type: 'error'; fatal: boolean; code: string; message: string };

export type EngineEvent = EventEnvelope & EngineEventBody;
export type EngineEventType = EngineEvent['type'];
export type EventOf<T extends EngineEventType> = Extract<EngineEvent, { type: T }>;

// ---------- 守卫 ----------

type Obj = Record<string, unknown>;

const isObj = (v: unknown): v is Obj => typeof v === 'object' && v !== null && !Array.isArray(v);
const isStr = (v: unknown): v is string => typeof v === 'string';
const isNum = (v: unknown): v is number => typeof v === 'number' && Number.isFinite(v);
const isUint = (v: unknown): v is number => isNum(v) && Number.isInteger(v) && v >= 0;
const isBool = (v: unknown): v is boolean => typeof v === 'boolean';
const orNull =
  <T>(guard: (v: unknown) => v is T) =>
  (v: unknown): v is T | null =>
    v === null || guard(v);
const arrayOf =
  <T>(guard: (v: unknown) => v is T) =>
  (v: unknown): v is T[] =>
    Array.isArray(v) && v.every(guard);

const PARAGRAPH_ID = /^P\d{2,}-\d{3,}$/;
export const isParagraphId = (v: unknown): v is ParagraphId => isStr(v) && PARAGRAPH_ID.test(v);

export function isRect(v: unknown): v is Rect {
  return isObj(v) && isNum(v.x0) && isNum(v.y0) && isNum(v.x1) && isNum(v.y1);
}

export const isStage = (v: unknown): v is Stage => (STAGES as readonly unknown[]).includes(v);
export const isRegionKind = (v: unknown): v is RegionKind =>
  (REGION_KINDS as readonly unknown[]).includes(v);
const isSeverity = (v: unknown): v is Severity => v === 'info' || v === 'warning' || v === 'error';
const isCoordSystem = (v: unknown): v is CoordSystem => v === 'pdf_user' || v === 'image_top_left';
const isStatus = (v: unknown): v is ParagraphStatus =>
  v === 'pending' ||
  v === 'translated' ||
  v === 'typeset' ||
  v === 'not_replaced' ||
  v === 'fallback';
const isLayoutRegion = (v: unknown): v is LayoutRegion =>
  isObj(v) && isRegionKind(v.kind) && isBool(v.inline) && isRect(v.bbox);

/** 每种事件的字段守卫（除 seq/ts/type）。 */
const EVENT_FIELDS: Record<EngineEventType, Record<string, (v: unknown) => boolean>> = {
  run_started: {
    protocol_version: isUint,
    engine_version: isStr,
    doc_id: isStr,
    pages: isUint,
  },
  stage_started: { stage: isStage },
  stage_finished: { stage: isStage, elapsed_ms: isUint },
  progress: { stage: isStage, done: isUint, total: isUint },
  layout: { page: isUint, regions: arrayOf(isLayoutRegion) },
  doc_meta: { title: orNull(isStr), authors: orNull(isStr) },
  paragraph: {
    paragraph_id: isParagraphId,
    page: isUint,
    status: isStatus,
    boxes: orNull(arrayOf(isRect)),
    coord_system: isCoordSystem,
    translated_html: orNull(isStr),
    kind: isRegionKind,
    source_text: isStr,
  },
  page_ready: { page: isUint, preview_path: orNull(isStr), revision: isUint },
  issue: {
    severity: isSeverity,
    code: isStr,
    paragraph_id: orNull(isParagraphId),
    page: orNull(isUint),
    message: isStr,
  },
  document_finished: {
    output: isStr,
    stats: (v) => isObj(v) && isUint(v.fonts) && isNum(v.expansion_ratio) && isUint(v.fallbacks),
  },
  run_finished: { ok: isBool, elapsed_ms: isUint },
  error: { fatal: isBool, code: isStr, message: isStr },
};

export function isEngineEvent(value: unknown): value is EngineEvent {
  if (!isObj(value) || !isUint(value.seq) || !isNum(value.ts) || !isStr(value.type)) return false;
  const fields = (EVENT_FIELDS as Record<string, Record<string, (v: unknown) => boolean>>)[
    value.type
  ];
  if (fields === undefined) return false;
  return Object.entries(fields).every(([key, guard]) => guard(value[key]));
}

const isTranslatorKind = (v: unknown): v is TranslatorKind => {
  if (!isObj(v)) return false;
  switch (v.kind) {
    case 'pi':
      return isStr(v.program) && isStr(v.model) && isStr(v.thinking);
    case 'agy':
      return isStr(v.program) && isStr(v.model);
    case 'fake':
      return isStr(v.name);
    case 'http':
      return true;
    default:
      return false;
  }
};
const isMode = (v: unknown): v is Mode => v === 'full' || v === 'bilingual';

const REQUEST_FIELDS: Record<Request['type'], Record<string, (v: unknown) => boolean>> = {
  configure: {
    provider: (v) => v === 'http' || v === 'pi' || v === 'agy',
    base_url: orNull(isStr),
    model: isStr,
    api_key: orNull(isStr),
    concurrency: isUint,
    cache_dir: isStr,
    translator: isTranslatorKind,
  },
  run: {
    doc_id: isStr,
    input: isStr,
    output: isStr,
    source_lang: isStr,
    target_lang: isStr,
    pages: orNull(arrayOf(isUint)),
    font_profile: orNull(isStr),
    terminology: orNull(isStr),
    mode: isMode,
    store: orNull(isStr),
  },
  retranslate: { doc_id: isStr, paragraph_ids: arrayOf(isParagraphId) },
  apply_edit: {
    doc_id: isStr,
    paragraph_id: isParagraphId,
    translated_html: isStr,
    base_revision: isUint,
  },
  export: { doc_id: isStr, output: isStr, mode: isMode },
  cancel: {},
};

export function isRequest(value: unknown): value is Request {
  if (!isObj(value) || !isStr(value.type)) return false;
  const fields = (REQUEST_FIELDS as Record<string, Record<string, (v: unknown) => boolean>>)[
    value.type
  ];
  if (fields === undefined) return false;
  return Object.entries(fields).every(([key, guard]) => guard(value[key]));
}
