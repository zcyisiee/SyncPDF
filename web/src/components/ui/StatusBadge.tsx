import type { ReactNode } from 'react';

import { cn } from '../../lib/cn';
import {
  documentStatus,
  isRunningStatus,
  stageLabel,
  stageStatusLabel,
  stageTone,
  type StatusTone,
} from '../../lib/humanize';

/** §4.3 `.st`：h 20px / r 3px / 6px 圆点 + 文案（状态永不只靠颜色，§7.3）。 */
const TONES: Record<StatusTone, string> = {
  pass: 'bg-pass-soft text-pass-ink',
  run: 'bg-run-soft text-run-ink',
  err: 'bg-err-soft text-err-ink',
  idle: 'bg-sand text-ink-4',
};

export function StatusBadge({
  tone = 'idle',
  running = false,
  className,
  title,
  children,
}: {
  tone?: StatusTone;
  /** 只有真实 running 才给圆点加脉冲（§4.3：全站唯一动效）。 */
  running?: boolean;
  className?: string;
  title?: string;
  children: ReactNode;
}) {
  return (
    <span
      title={title}
      className={cn(
        'inline-flex h-5 items-center gap-[5px] whitespace-nowrap rounded-[3px] px-[7px] text-tiny leading-none tracking-[0.02em]',
        TONES[tone],
        className,
      )}
    >
      <span
        aria-hidden="true"
        className={cn('h-[6px] w-[6px] flex-none rounded-full bg-current', running && 'pulse-dot')}
      />
      {children}
    </span>
  );
}

/** 文档整体状态徽标（顶栏右侧 / 视图栏文档头）。 */
export function DocumentStatusBadge({ stageSummary }: { stageSummary?: Record<string, string> }) {
  const status = documentStatus(stageSummary);
  return (
    <StatusBadge tone={status.tone} running={status.running}>
      {status.label}
    </StatusBadge>
  );
}

/** 单阶段徽标（文件库卡片）：文案用阶段中文名，标题给出原始状态串。 */
export function StageBadge({ stage, status }: { stage: string; status: string }) {
  return (
    <StatusBadge
      tone={stageTone(status)}
      running={isRunningStatus(status)}
      title={`${stage} · ${stageStatusLabel(status)}（${status}）`}
    >
      {stageLabel(stage)}
    </StatusBadge>
  );
}
