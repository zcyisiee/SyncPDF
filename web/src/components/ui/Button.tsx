import type { ButtonHTMLAttributes, ReactNode } from 'react';

import { cn } from '../../lib/cn';

export type ButtonVariant = 'default' | 'primary' | 'danger' | 'ghost';
export type ButtonSize = 'md' | 'sm' | 'icon';

// DESIGN.md §4.1：hover 只动背景/边框，不降前景对比；disabled 是全站唯一允许降对比度的状态。
const BASE =
  'inline-flex items-center justify-center gap-[6px] rounded border leading-none tracking-[0.02em] transition-colors active:translate-y-px disabled:cursor-not-allowed disabled:opacity-45';

const VARIANTS: Record<ButtonVariant, string> = {
  default:
    'border-hair bg-ivory text-ink-2 hover:border-hair-2 hover:bg-sand hover:text-ink disabled:border-hair disabled:bg-ivory disabled:text-ink-4',
  primary:
    'border-accent bg-accent text-accent-on hover:border-transparent hover:bg-[color-mix(in_oklch,var(--accent)_88%,var(--fg))] disabled:border-hair disabled:bg-ivory disabled:text-ink-4',
  danger:
    'border-[color-mix(in_oklch,var(--err)_34%,transparent)] bg-ivory text-err hover:border-err hover:bg-err-soft',
  ghost: 'border-transparent bg-transparent text-ink-3 hover:bg-sand hover:text-ink',
};

const SIZES: Record<ButtonSize, string> = {
  md: 'h-7 px-[10px] text-sm',
  sm: 'h-6 px-2 text-tiny',
  icon: 'h-7 w-7 p-0',
};

function buttonClasses(
  variant: ButtonVariant = 'default',
  size: ButtonSize = 'md',
  className?: string,
): string {
  return cn(BASE, VARIANTS[variant], SIZES[size], className);
}

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: ButtonSize;
}

export function Button({ variant = 'default', size = 'md', className, ...rest }: ButtonProps) {
  return <button type="button" className={buttonClasses(variant, size, className)} {...rest} />;
}

export function LinkButton({
  href,
  variant = 'default',
  size = 'md',
  className,
  children,
}: {
  href: string;
  variant?: ButtonVariant;
  size?: ButtonSize;
  className?: string;
  children: ReactNode;
}) {
  return (
    <a href={href} className={buttonClasses(variant, size, className)}>
      {children}
    </a>
  );
}
