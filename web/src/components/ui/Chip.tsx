import type { ReactNode } from 'react';

import { cn } from '../../lib/cn';

/** §4.3 `.chip` / `.chip--*`：h 19px / r 3px / mono 10px `0.03em`。 */
export type ChipTone = 'default' | 'accent' | 'accent-line' | 'run' | 'pass' | 'err';

const TONES: Record<ChipTone, string> = {
  default: 'bg-sand text-ink-3',
  accent: 'bg-accent-soft text-accent',
  'accent-line': 'border border-dashed border-accent bg-transparent text-accent',
  run: 'bg-run-soft text-run-ink',
  pass: 'bg-pass-soft text-pass-ink',
  err: 'bg-err-soft text-err-ink',
};

export function Chip({
  tone = 'default',
  className,
  title,
  children,
}: {
  tone?: ChipTone;
  className?: string;
  title?: string;
  children: ReactNode;
}) {
  return (
    <span
      title={title}
      className={cn(
        'inline-flex h-[19px] items-center gap-1 whitespace-nowrap rounded-[3px] px-[6px] font-mono text-micro leading-none tracking-[0.03em] [font-variant-numeric:tabular-nums]',
        TONES[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}
