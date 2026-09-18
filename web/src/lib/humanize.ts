/**
 * 文案口径：7 个阶段名 + 状态徽标文案 + 事件 kind 短标签 + 耗时文案。
 * 事件 kind 标签是 W06 补的（`docs/frontend/api.md` §4 明确「level 推导表放前端」）。
 */
export const STAGE_NAMES = [
  'parse',
  'translate',
  'apply',
  'build',
  'check',
  'review',
  'report',
] as const;

export type StageName = (typeof STAGE_NAMES)[number];

export const STAGE_LABELS: Record<StageName, string> = {
  parse: '解析',
  translate: '翻译',
  apply: '套版',
  build: '编译',
  check: '检查',
  review: '复核',
  report: '报告',
};

export function stageLabel(stage: string): string {
  return STAGE_LABELS[stage as StageName] ?? stage;
}

/** 最后一个阶段：跑完它就说明 run 收尾（事件流的 live 判据，见 `lib/events.ts`）。 */
export const FINAL_STAGE: StageName = STAGE_NAMES[STAGE_NAMES.length - 1];

/**
 * 事件 kind → 中文短标签（优先 ≤4 字）。表里有两类：
 *
 * 1. **归档里真实出现过的 47 种 kind**（`tmp/<did>/debug/runs/<run>/events.jsonl` 全量统计）；
 * 2. 一个**虚拟 kind** `job_update`（W14：SSE 同一条流里的 job 状态变化通知，不在
 *    events.jsonl 里、没有 seq，见 api.md §1.4.1）—— 它进这张表只是为了有中文文案，
 *    并且 `EVENT_KINDS`（SSE 必须逐个 `addEventListener` 的清单）漏了它就收不到。
 *
 * 没有 kind 就原样显示英文，不编造。
 */
export const KIND_LABELS: Record<string, string> = {
  // 阶段骨架
  stage_started: '阶段开始',
  stage_finished: '阶段完成',
  stage_error: '阶段失败',
  replay_notice: '归档提示',
  // 模型调用
  call_started: '调用开始',
  call_finished: '调用完成',
  provider_artifacts: '调用产物',
  // 缓存
  cache_hit: '缓存命中',
  cache_miss: '缓存未命中',
  cache_write: '缓存写入',
  cache_bypass: '缓存绕过',
  // 候选 / 写回
  candidate_evaluated: '候选评估',
  candidate_selected: '候选采用',
  canonical_writeback: '写回规范',
  writeback_saved: '写回保存',
  text_version: '文本版本',
  // 编译
  compile_requests: '编译请求',
  compile_reuse: '编译复用',
  compile_fallback: '编译回退',
  compile_expand: '编译扩展',
  artifact_bundle: '产物打包',
  // 解析 / 版面
  pdf_prepared: 'PDF 准备',
  page_frames: '页面框',
  paragraphs_found: '段落发现',
  selection: '选区',
  native_chars: '原生字符',
  source_geometry: '原文几何',
  toc: '目录',
  enclosed_marker: '包围标记',
  inline_math: '行内公式',
  ocr_backfill: 'OCR 回填',
  links_snapshot: '链接快照',
  styles_formulas: '样式公式',
  // 套版 / 校验
  anchor_repair: '锚点修复',
  apply_validation: '套版校验',
  placeholder_validation: '占位校验',
  missing_ids: '缺失 id',
  layout_parsed: '版面解析',
  layout_coverage: '版面覆盖',
  typesetting_geometry: '排版几何',
  span_started: '片段开始',
  span_finished: '片段完成',
  // 公式（LaTeX）
  latex_capability: '公式能力',
  latex_candidates: '公式候选',
  latex_prepare: '公式准备',
  latex_stamp: '公式标记',
  latex_summary: '公式汇总',
  // 虚拟 kind（不在归档里）：SSE 的 job 状态变化通知（W14，api.md §1.4.1）
  job_update: '任务状态',
};

/**
 * SSE 必须逐个 `addEventListener(<kind>)` 的 kind 清单（见 `useEventStream` 注释：
 * `event: <kind>` 是命名分发，`onmessage` 只收默认类型）。表里没有的 kind 收不到 ——
 * 新 kind 落地时必须在这里补一行（W14 的虚拟 kind `job_update` 就是这样加进来的；
 * 它在 `useEventStream` 里走单独的分支：通知不进事件窗口）。
 */
export const EVENT_KINDS: readonly string[] = Object.keys(KIND_LABELS);

export function kindLabel(kind: string): string {
  return KIND_LABELS[kind] ?? kind;
}

/**
 * 秒 → 短耗时文案（live 段秒表口径）。`null`（阶段没跑 / 拿不到耗时）显示 `—`，不编造 0。
 * `<1s → 刚启动`：只用于 live 段秒表（还在走）；已完成阶段的耗时用 formatStageDuration
 * （`0.3s` 实测值），避免「已完成 + 刚启动」的矛盾文案。
 */
export function formatDuration(seconds: number | null | undefined): string {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds)) return '—';
  const total = Math.max(0, Math.floor(seconds));
  if (total < 1) return '刚启动';
  if (total < 60) return `${total}s`;
  if (total < 3600) return `${Math.floor(total / 60)}m ${total % 60}s`;
  return `${Math.floor(total / 3600)}h ${Math.floor((total % 3600) / 60)}m`;
}

/**
 * 已完成阶段的实测耗时文案：`<1s` 如实显示一位小数（`0.3s` / `0s`），
 * 不用「刚启动」（那是 live 段的口径）。
 */
export function formatStageDuration(seconds: number | null | undefined): string {
  if (typeof seconds !== 'number' || !Number.isFinite(seconds)) return '—';
  if (seconds < 1) return `${Math.round(seconds * 10) / 10}s`.replace('.0s', 's');
  return formatDuration(seconds);
}

export type StatusTone = 'pass' | 'run' | 'err' | 'idle';

const RUNNING_STATUSES = new Set(['running', 'waiting', 'queued', 'in_progress']);
const FAILED_STATUSES = new Set(['error', 'failed', 'failure', 'needs_fix']);
const DONE_STATUSES = new Set(['ok', 'done', 'pass', 'passed']);

/** 唯一会触发 pulse 动效的状态（DESIGN.md §4.3：脉冲只出现在真正运行中的元素上）。 */
export function isRunningStatus(status: string): boolean {
  return status === 'running';
}

export function stageTone(status: string): StatusTone {
  if (RUNNING_STATUSES.has(status)) return 'run';
  if (FAILED_STATUSES.has(status)) return 'err';
  if (DONE_STATUSES.has(status)) return 'pass';
  return 'idle';
}

export function stageStatusLabel(status: string): string {
  if (status === 'not_run') return '未运行';
  switch (stageTone(status)) {
    case 'run':
      return '进行中';
    case 'err':
      return '失败';
    case 'pass':
      return '已完成';
    default:
      return status;
  }
}

export interface DocumentStatus {
  label: string;
  tone: StatusTone;
  /** true 只代表 run_state 里真有 `running` 阶段（waiting 不算运行中）。 */
  running: boolean;
}

/** 文档整体状态：取 7 个阶段里最"响"的一个（运行中 > 失败 > 未完成 > 已完成 > 未运行）。 */
export function documentStatus(stageSummary: Record<string, string> | undefined): DocumentStatus {
  const statuses = STAGE_NAMES.map((stage) => stageSummary?.[stage] ?? 'not_run');
  const running = statuses.some(isRunningStatus);
  if (statuses.some((status) => RUNNING_STATUSES.has(status))) {
    return { label: '进行中', tone: 'run', running };
  }
  if (statuses.some((status) => FAILED_STATUSES.has(status))) {
    return { label: '失败', tone: 'err', running: false };
  }
  if (statuses.every((status) => DONE_STATUSES.has(status))) {
    return { label: '已完成', tone: 'pass', running: false };
  }
  if (statuses.every((status) => status === 'not_run')) {
    return { label: '未运行', tone: 'idle', running: false };
  }
  return { label: '未完成', tone: 'idle', running: false };
}

/**
 * 列表页的自动刷新判据：`GET /documents` 的 `stage_summary` 里有任一阶段真是 `running`。
 * （正在跑的 run 才会让列表/详情按 2–3s 轮询；全完成的文档不轮询。）
 */
export function hasRunningDocument(
  documents: readonly { stage_summary: Record<string, string> }[],
): boolean {
  return documents.some((doc) =>
    STAGE_NAMES.some((stage) => isRunningStatus(doc.stage_summary[stage] ?? 'not_run')),
  );
}

/** `updated_at`（UTC ISO8601）→ 中文相对时间；拿不到时间戳显示 `—`。 */
export function humanizeUpdatedAt(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return '—';
  const at = new Date(iso);
  if (Number.isNaN(at.getTime())) return '—';
  const minutes = Math.floor((now.getTime() - at.getTime()) / 60_000);
  if (minutes < 1) return '刚刚';
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  const days = Math.floor(hours / 24);
  if (days < 30) return `${days} 天前`;
  return at.toISOString().slice(0, 10);
}

/** 产物缺失时计数是 `null`（不是 0），显示 `—` 而不是编造 0（docs/frontend/api.md §3.1）。 */
export function countLabel(value: number | null | undefined, unit: string): string {
  return typeof value === 'number' ? `${value} ${unit}` : '—';
}

export function progressLabel(
  translated: number | null | undefined,
  total: number | null | undefined,
): string {
  return `${translated ?? '—'}/${total ?? '—'}`;
}
