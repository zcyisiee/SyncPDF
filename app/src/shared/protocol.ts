/**
 * SyncPDF JSONL 协议类型（手写；之后由 Rust `syncpdf-protocol` 导出的
 * `protocol.schema.json` 校验，字段严格按 02-技术路径与架构.md §9）。
 *
 * - 请求：主进程 → 引擎 stdin，每行一个 JSON 对象；
 * - 事件：引擎 stdout → 主进程 → 渲染进程，每行一个 JSON 对象，`seq` 单调递增。
 */

// ---------- 基础 ----------

/** 段落 id：`P{page:02}-{seq:03}`（与现版协议一致，模型可见）。 */
export type ParagraphId = string;

/** 页索引（0 基）。 */
export type PageId = number;

/**
 * 段落框坐标系标注。服务端不转换坐标，换算全部在前端
 * （`viewport.convertToViewportRectangle` 是唯一换算点，规约 #6）：
 * - `pdf_native`：PDF user space（左下原点、y 向上）；
 * - `pdf_topleft`：页左上原点、y 向下。
 */
export type CoordSystem = 'pdf_native' | 'pdf_topleft';

/** bbox 输入框：`[x0, y0, x1, y1]`，含义由 `CoordSystem` 决定。 */
export type Box = [number, number, number, number];

/** 翻译提供方类型。 */
export type Provider = 'openai_compatible' | 'anthropic';

/** 任务模式。 */
export type RunMode = 'full' | 'bilingual';

// ---------- 请求（stdin，§9.2） ----------

/** 首条请求：下发引擎配置。api_key 只在此处出现，绝不写日志。 */
export interface ConfigureRequest {
  type: 'configure';
  provider: Provider;
  base_url: string;
  model: string;
  api_key: string;
  concurrency: number;
  cache_dir: string;
}

/** 启动任务。 */
export interface RunRequest {
  type: 'run';
  doc_id: string;
  input: string;
  output: string;
  source_lang: string;
  target_lang: string;
  pages?: number[];
  font_profile?: string;
  terminology?: Record<string, string>;
  mode: RunMode;
}

/** 局部重译。 */
export interface RetranslateRequest {
  type: 'retranslate';
  doc_id: string;
  paragraph_ids: ParagraphId[];
}

/** 手动编辑 → 重排版 + 局部回写。base_revision 不匹配返回 `error{code: conflict}`。 */
export interface ApplyEditRequest {
  type: 'apply_edit';
  doc_id: string;
  paragraph_id: ParagraphId;
  translated_html: string;
  base_revision: number;
}

/** 发布导出。 */
export interface ExportRequest {
  type: 'export';
  doc_id: string;
  output: string;
  mode: RunMode;
}

/** 取消当前任务（引擎 5s 内收尾退出；超时主进程 kill）。 */
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

// ---------- 事件（stdout，§9.3） ----------

/** 事件公共字段：每行 `{"seq":n,"ts":…,"type":"…",…}`，seq 单调递增。 */
export interface EngineEventBase {
  seq: number;
  ts: number;
}

export interface RunStartedEvent extends EngineEventBase {
  type: 'run_started';
  protocol_version: number;
  engine_version: string;
  doc_id: string;
  pages: number;
}

/** 阶段名（§3 数据流）：preflight → source_analysis → layout_analysis →
 * paragraph_analysis → translating → typesetting → validating → publishing。 */
export type StageName =
  | 'preflight'
  | 'source_analysis'
  | 'layout_analysis'
  | 'paragraph_analysis'
  | 'translating'
  | 'typesetting'
  | 'validating'
  | 'publishing';

export interface StageStartedEvent extends EngineEventBase {
  type: 'stage_started';
  stage: StageName;
}

export interface StageFinishedEvent extends EngineEventBase {
  type: 'stage_finished';
  stage: StageName;
  /** 耗时（毫秒）。 */
  elapsed_ms: number;
}

export interface ProgressEvent extends EngineEventBase {
  type: 'progress';
  stage: StageName;
  done: number;
  total: number;
}

/** 段落状态（§9.3 + 规约 #9：No(reason) → not_replaced）。 */
export type ParagraphStatus = 'translated' | 'typeset' | 'not_replaced' | 'fallback';

export interface ParagraphEvent extends EngineEventBase {
  type: 'paragraph';
  paragraph_id: ParagraphId;
  status: ParagraphStatus;
  /** 段落框为 null 表示未识别，[] 表示识别了但无框（规约 #12/#13）。 */
  boxes: Box[] | null;
  coord_system: CoordSystem;
  translated_html: string;
}

export interface PageReadyEvent extends EngineEventBase {
  type: 'page_ready';
  page: number;
  /**
   * 可选增量预览 PDF 路径。
   * 引擎就地重写 output（无独立预览产物）时发 `null`——语义同缺省：
   * 前端回落到重新加载 output 文件的该页（M2-06）。
   */
  preview_path?: string | null;
}

/** 问题等级。 */
export type IssueSeverity = 'info' | 'warning' | 'error';

export interface IssueEvent extends EngineEventBase {
  type: 'issue';
  severity: IssueSeverity;
  code: string;
  paragraph_id?: ParagraphId;
  page?: PageId;
  message: string;
}

export interface DocumentFinishedEvent extends EngineEventBase {
  type: 'document_finished';
  output: string;
  stats: {
    fonts: number;
    expansion_ratio: number;
    fallback_count: number;
  };
}

export interface RunFinishedEvent extends EngineEventBase {
  type: 'run_finished';
  ok: boolean;
  /** 耗时（毫秒）。 */
  elapsed_ms: number;
}

export interface ErrorEvent extends EngineEventBase {
  type: 'error';
  fatal: boolean;
  code: string;
  message: string;
}

export type EngineEvent =
  | RunStartedEvent
  | StageStartedEvent
  | StageFinishedEvent
  | ProgressEvent
  | ParagraphEvent
  | PageReadyEvent
  | IssueEvent
  | DocumentFinishedEvent
  | RunFinishedEvent
  | ErrorEvent;

export type EngineEventType = EngineEvent['type'];

// ---------- 类型守卫（供 fake-sidecar 与测试共用） ----------

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

function hasString(value: Record<string, unknown>, key: string): boolean {
  return typeof value[key] === 'string';
}

const STAGE_NAMES: readonly StageName[] = [
  'preflight',
  'source_analysis',
  'layout_analysis',
  'paragraph_analysis',
  'translating',
  'typesetting',
  'validating',
  'publishing',
];

export function isStageName(value: unknown): value is StageName {
  return typeof value === 'string' && (STAGE_NAMES as readonly string[]).includes(value);
}

/** 校验事件公共字段：seq 非负整数、ts 有限数、type 已知。 */
export function isEngineEventBase(value: unknown): value is EngineEventBase & { type: string } {
  if (!isRecord(value)) return false;
  if (!Number.isInteger(value.seq) || (value.seq as number) < 0) return false;
  if (typeof value.ts !== 'number' || !Number.isFinite(value.ts)) return false;
  return typeof value.type === 'string';
}

const EVENT_TYPES: readonly EngineEventType[] = [
  'run_started',
  'stage_started',
  'stage_finished',
  'progress',
  'paragraph',
  'page_ready',
  'issue',
  'document_finished',
  'run_finished',
  'error',
];

/** 完整事件判别：先公共字段，再按 type 校验各事件的必带字段。 */
export function isEngineEvent(value: unknown): value is EngineEvent {
  if (!isEngineEventBase(value)) return false;
  const record = value as unknown as Record<string, unknown>;
  const type: unknown = record.type;
  if (typeof type !== 'string' || !(EVENT_TYPES as readonly string[]).includes(type)) return false;
  switch (type) {
    case 'run_started':
      return (
        Number.isInteger(record.protocol_version) &&
        hasString(record, 'engine_version') &&
        hasString(record, 'doc_id') &&
        Number.isInteger(record.pages)
      );
    case 'stage_started':
    case 'stage_finished':
      return isStageName(record.stage);
    case 'progress':
      return (
        isStageName(record.stage) &&
        Number.isInteger(record.done) &&
        Number.isInteger(record.total)
      );
    case 'paragraph': {
      if (!hasString(record, 'paragraph_id') || !hasString(record, 'translated_html')) return false;
      if (!(record.coord_system === 'pdf_native' || record.coord_system === 'pdf_topleft')) return false;
      const status = record.status;
      if (
        status !== 'translated' &&
        status !== 'typeset' &&
        status !== 'not_replaced' &&
        status !== 'fallback'
      ) {
        return false;
      }
      const boxes = record.boxes;
      if (boxes !== null && !Array.isArray(boxes)) return false;
      return true;
    }
    case 'page_ready':
      return Number.isInteger(record.page);
    case 'issue':
      return (
        hasString(record, 'code') &&
        hasString(record, 'message') &&
        (record.severity === 'info' || record.severity === 'warning' || record.severity === 'error')
      );
    case 'document_finished':
      return hasString(record, 'output');
    case 'run_finished':
      return typeof record.ok === 'boolean';
    case 'error':
      return (
        typeof record.fatal === 'boolean' &&
        hasString(record, 'code') &&
        hasString(record, 'message')
      );
    default:
      return false;
  }
}

const REQUEST_TYPES = [
  'configure',
  'run',
  'retranslate',
  'apply_edit',
  'export',
  'cancel',
] as const;

/** 请求判别（主进程写 stdin 前自检；测试用）。 */
export function isRequest(value: unknown): value is Request {
  if (!isRecord(value)) return false;
  const type = value.type;
  switch (type) {
    case 'configure':
      return (
        hasString(value, 'provider') &&
        hasString(value, 'base_url') &&
        hasString(value, 'model') &&
        hasString(value, 'api_key') &&
        Number.isInteger(value.concurrency) &&
        hasString(value, 'cache_dir')
      );
    case 'run':
      return (
        hasString(value, 'doc_id') &&
        hasString(value, 'input') &&
        hasString(value, 'output') &&
        hasString(value, 'source_lang') &&
        hasString(value, 'target_lang') &&
        (value.mode === 'full' || value.mode === 'bilingual')
      );
    case 'retranslate':
      return (
        hasString(value, 'doc_id') &&
        Array.isArray(value.paragraph_ids) &&
        (value.paragraph_ids as unknown[]).every((id) => typeof id === 'string')
      );
    case 'apply_edit':
      return (
        hasString(value, 'doc_id') &&
        hasString(value, 'paragraph_id') &&
        hasString(value, 'translated_html') &&
        Number.isInteger(value.base_revision)
      );
    case 'export':
      return hasString(value, 'doc_id') && hasString(value, 'output');
    case 'cancel':
      return true;
    default:
      return false;
  }
}

export function isRequestType(value: unknown): value is Request['type'] {
  return typeof value === 'string' && (REQUEST_TYPES as readonly string[]).includes(value);
}
