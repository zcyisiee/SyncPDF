/**
 * 引擎事件 → 事件流里的一行（图标、文案、所属页、可跳转的段落）。
 */
import type { EngineEvent, ParagraphId, Stage } from '@shared/protocol';
import { REGION_STYLE } from '@/pdf/regionKinds';

export interface EventLine {
  icon: string;
  tone: 'muted' | 'ok' | 'warning' | 'error' | 'accent';
  text: string;
  page: number | null;
  paragraphId: ParagraphId | null;
}

export const STAGE_LABEL: Record<Stage, string> = {
  preflight: '预检',
  source_analysis: '解析源文件',
  layout_analysis: '版面分析',
  paragraph_analysis: '段落分析',
  translating: '翻译',
  typesetting: '排版',
  validating: '校验',
  publishing: '发布',
};

const PARAGRAPH_TONE = {
  pending: 'muted',
  translated: 'accent',
  typeset: 'ok',
  not_replaced: 'muted',
  fallback: 'warning',
} as const;

export function describeEvent(event: EngineEvent): EventLine {
  const line = (icon: string, tone: EventLine['tone'], text: string, page: number | null = null): EventLine => ({
    icon,
    tone,
    text,
    page,
    paragraphId: null,
  });
  switch (event.type) {
    case 'run_started':
      return line('play', 'accent', `开始翻译（${event.pages} 页，引擎 ${event.engine_version}）`);
    case 'stage_started':
      return line('debug-step-into', 'muted', `${STAGE_LABEL[event.stage]}开始`);
    case 'stage_finished':
      return line('debug-step-out', 'muted', `${STAGE_LABEL[event.stage]}完成 · ${(event.elapsed_ms / 1000).toFixed(1)} 秒`);
    case 'progress':
      return line('loading', 'muted', `${STAGE_LABEL[event.stage]} ${event.done}/${event.total}`);
    case 'layout':
      return line('layout', 'muted', `识别 ${event.regions.length} 个版面区域`, event.page);
    case 'doc_meta':
      return line('book', 'muted', `标题：${event.title ?? '未识别'}${event.authors === null ? '' : ` · 作者：${event.authors}`}`);
    case 'block_edits':
      return line('edit', 'muted', `应用 ${event.edits.length} 处单块编辑`);
    case 'paragraph':
      return {
        icon: 'symbol-text',
        tone: PARAGRAPH_TONE[event.status],
        text: `${event.paragraph_id} ${REGION_STYLE[event.kind].label} · ${event.status}`,
        page: event.page,
        paragraphId: event.paragraph_id,
      };
    case 'page_ready':
      return line('file', 'ok', `第 ${event.page} 页译文已写入`, event.page);
    case 'issue':
      return {
        icon: event.severity === 'error' ? 'error' : event.severity === 'warning' ? 'warning' : 'info',
        tone: event.severity === 'error' ? 'error' : event.severity === 'warning' ? 'warning' : 'muted',
        text: `${event.code}：${event.message}`,
        page: event.page,
        paragraphId: event.paragraph_id,
      };
    case 'document_finished':
      return line('check-all', 'ok', `译文完成 · ${event.stats.fallbacks} 段回退 · 扩展比 ${event.stats.expansion_ratio.toFixed(2)}`);
    case 'run_finished':
      // ok 还要求零质量问题；失败本身另有致命 error 事件，这里只说明检查未全部通过
      return line(
        event.ok ? 'pass' : 'warning',
        event.ok ? 'ok' : 'warning',
        `${event.ok ? '运行结束' : '运行结束 · 质量检查未全部通过'} · ${(event.elapsed_ms / 1000).toFixed(1)} 秒`,
      );
    case 'error':
      return line('error', event.fatal ? 'error' : 'warning', `${event.code}：${event.message}`);
  }
}
