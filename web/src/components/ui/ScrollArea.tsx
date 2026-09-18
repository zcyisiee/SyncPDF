import type { HTMLAttributes, ReactNode } from 'react';

import { cn } from '../../lib/cn';

/** 滚动容器（DESIGN.md §6：ScrollArea → 事件流 / 纸页画布 / 词表主体）。原生滚动，不引 Radix。 */
export interface ScrollAreaProps extends HTMLAttributes<HTMLDivElement> {
  children?: ReactNode;
}

export function ScrollArea({ className, children, ...rest }: ScrollAreaProps) {
  return (
    <div className={cn('min-h-0 min-w-0 overflow-auto', className)} {...rest}>
      {children}
    </div>
  );
}
