import type { ReactNode } from 'react';

import { cn } from '../../lib/cn';

/**
 * 禁用态说明用的 tooltip（§4.1：tooltip 说明「W08 接入」这类禁用原因）。
 * `content: attr(data-tip)` 只能用 CSS 表达，样式在 src/app/globals.css 的 `.tip`。
 */
export function Tooltip({
  content,
  wide = false,
  className,
  children,
}: {
  content: string;
  wide?: boolean;
  className?: string;
  children: ReactNode;
}) {
  return (
    <span data-tip={content} className={cn('tip', wide && 'tip--w', 'inline-flex', className)}>
      {children}
    </span>
  );
}
