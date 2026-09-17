/**
 * 底部时间线（§4.7 / §8.3）：**真数据**——7 段、条宽 = 该段耗时 / 最长段（同一线性标尺）、
 * 未开始段固定 64px 虚线不参与比例、进行中段 run 色 + 脉冲 + 本地秒表、失败段朱红点。
 *
 * 数据来自 `useTimelineStages`（stage-state 基线 + 事件流的 live 段），本组件只负责渲染。
 * 点击整列 → 跳到该阶段对应的视图（映射表在 `lib/timeline.ts::STAGE_VIEWS`，注释里写了理由）。
 * 两行结构 + 压缩态（`--tlh < 94px` 时耗时内联到名称右侧）。
 */
import { cn } from '../../lib/cn';
import {
  formatDuration,
  formatStageDuration,
  STAGE_LABELS,
  stageStatusLabel,
} from '../../lib/humanize';
import {
  barWidthPercent,
  maxSegmentDuration,
  timelineStatus,
  totalDurationS,
  type SegmentState,
  type TimelineSegment,
} from '../../lib/timeline';
import { useUiStore } from '../../stores/ui';
import { StatusBadge } from '../ui/StatusBadge';

/** 压缩态阈值（§8.3：`--tlh < 94px`）。 */
const COMPACT_HEIGHT = 94;

/** 段名文字色（状态 ink 变体，§1.3：状态文字不能用饱和原色）。 */
const TEXT: Record<SegmentState, string> = {
  ok: 'text-pass-ink',
  err: 'text-err-ink',
  live: 'text-run-ink',
  not_run: 'text-ink-4',
};

/** 时长条轨道底色（§4.7 的软底）。 */
const TRACK: Record<SegmentState, string> = {
  ok: 'bg-pass-soft',
  err: 'bg-err-soft',
  live: 'bg-run-soft',
  not_run: 'bg-transparent',
};

/** 填充条：图形对象用饱和原色（§1.3 只需 3:1）；live 段 55% 不透明度表示「批内进度」。 */
const BAR: Record<SegmentState, string> = {
  ok: 'bg-pass',
  err: 'bg-err',
  live: 'bg-run opacity-55',
  not_run: '',
};

function segmentText(segment: TimelineSegment): string {
  if (segment.state === 'not_run') return '未运行';
  if (segment.state === 'live') return formatDuration(segment.elapsedS);
  return formatStageDuration(segment.durationS);
}

export function Timeline({
  did,
  segments,
  unavailable = false,
}: {
  did: string;
  segments: readonly TimelineSegment[];
  /** stage-state 查询失败（时间线只有「未运行」时不能假装这是真实状态）。 */
  unavailable?: boolean;
}) {
  const timelineHeight = useUiStore((state) => state.timelineHeight);
  const compact = timelineHeight < COMPACT_HEIGHT;
  const maxDuration = maxSegmentDuration(segments);
  const total = totalDurationS(segments);
  const status = timelineStatus(segments);

  return (
    <section
      aria-label="阶段时间线"
      data-od-id="timeline"
      className="col-span-full row-start-3 flex min-w-0 flex-col gap-[5px] overflow-hidden border-t border-hair bg-ivory px-s4 pb-[6px] pt-[7px]"
    >
      <div className="flex h-5 flex-none items-center gap-s4 whitespace-nowrap font-mono text-micro tracking-[0.03em] text-ink-4">
        <span className="min-w-0 truncate">
          <b className="font-medium text-ink-2">{did}</b> · 阶段时间线
        </span>
        <StatusBadge tone={status.tone} running={status.running}>
          {status.label}
        </StatusBadge>
        <span
          data-od-id="timeline-total"
          className="ml-auto flex-none [font-variant-numeric:tabular-nums]"
        >
          总用时 {formatStageDuration(total)}
        </span>
      </div>
      <div className="flex min-h-0 flex-1 overflow-x-auto overflow-y-hidden">
        <ol className="grid flex-1 grid-cols-[repeat(7,minmax(136px,1fr))] gap-x-[10px]">
          {segments.map((segment) => {
            const width = barWidthPercent(segment, maxDuration);
            return (
              <li
                key={segment.stage}
                data-od-id={`timeline-stage-${segment.stage}`}
                data-state={segment.state}
                data-view={segment.view}
                className="min-w-0"
              >
                <a
                  href={`#/d/${encodeURIComponent(did)}/${segment.view}`}
                  title={`${STAGE_LABELS[segment.stage]} · ${stageStatusLabel(segment.status)}（${segment.status}） · 点击去对应视图`}
                  className="grid min-w-0 grid-rows-[minmax(0,1fr)_14px] gap-y-[3px] rounded-[2px] hover:brightness-[.97]"
                >
                  <span className="flex min-w-0 flex-col justify-center">
                    <span
                      className={cn(
                        'flex items-center gap-[5px] whitespace-nowrap text-tiny font-medium leading-[1.4] tracking-[0.02em]',
                        TEXT[segment.state],
                      )}
                    >
                      {segment.state === 'live' ? (
                        <span
                          aria-hidden="true"
                          className="h-[5px] w-[5px] flex-none rounded-full bg-current pulse-dot"
                        />
                      ) : null}
                      {STAGE_LABELS[segment.stage]}
                      {segment.state === 'err' ? (
                        <span
                          aria-hidden="true"
                          className="h-[6px] w-[6px] flex-none rounded-full bg-err"
                        />
                      ) : null}
                      <span className="sr-only">{stageStatusLabel(segment.status)}</span>
                      {compact ? (
                        <span className="ml-auto font-mono text-micro font-normal text-ink-4 [font-variant-numeric:tabular-nums]">
                          {segmentText(segment)}
                        </span>
                      ) : null}
                    </span>
                    {compact ? null : (
                      <span className="mt-[2px] font-mono text-micro text-ink-4 [font-variant-numeric:tabular-nums]">
                        {segmentText(segment)}
                      </span>
                    )}
                    <span aria-hidden="true" className="mt-[2px] h-[5px] w-px flex-none bg-hair-2" />
                  </span>
                  <span className={cn('flex h-[14px] items-end', TRACK[segment.state])}>
                    {width === null ? (
                      <span
                        aria-hidden="true"
                        className="h-[10px] w-16 flex-none rounded-t-[2px] border border-dashed border-b-0 border-hair-2"
                      />
                    ) : (
                      <span
                        aria-hidden="true"
                        style={{ width: `${Math.round(width * 10) / 10}%` }}
                        className={cn('h-[10px] flex-none rounded-t-[2px]', BAR[segment.state])}
                      />
                    )}
                  </span>
                </a>
              </li>
            );
          })}
        </ol>
      </div>
      {unavailable ? (
        <p data-od-id="timeline-note" className="font-mono text-micro text-ink-4">
          阶段状态暂时读不到（stage-state 请求失败），下面的条不反映真实进度
        </p>
      ) : null}
    </section>
  );
}
