import type { HTMLAttributes, ReactNode } from 'react';

import { cn } from '../../lib/cn';
import { describeApiError } from '../../lib/api';

/**
 * §4.9 错误卡：`border-left: 3px --err` + **圆角 0**（避开「彩色竖条 + 圆角卡片」反模式），
 * 标题 serif 500 14px `--err-ink`，详情 mono 12px，按钮组由调用方传入。
 */
export function ErrorCard({
  error,
  title,
  message,
  detail,
  className,
  children,
  ...rest
}: {
  error?: unknown;
  title?: string;
  message?: string;
  detail?: string;
  className?: string;
  children?: ReactNode;
} & Omit<HTMLAttributes<HTMLDivElement>, 'title'>) {
  const described = error === undefined ? null : describeApiError(error);
  const heading = title ?? described?.title ?? '请求失败';
  const body = message ?? described?.message ?? '';
  const meta = detail ?? described?.detail;
  return (
    <div
      role="alert"
      className={cn(
        'flex flex-col gap-s3 rounded-none border border-hair border-l-[3px] border-l-err bg-err-soft p-s5',
        className,
      )}
      {...rest}
    >
      <p className="font-serif text-md font-medium leading-[1.4] text-err-ink">{heading}</p>
      {body === '' ? null : <p className="text-body text-ink-2">{body}</p>}
      {meta === undefined || meta === '' ? null : (
        <p className="break-all font-mono text-sm text-ink-3">{meta}</p>
      )}
      {children === undefined ? null : <div className="flex flex-wrap items-center gap-s2">{children}</div>}
    </div>
  );
}
