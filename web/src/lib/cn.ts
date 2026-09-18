import { clsx, type ClassValue } from 'clsx';
import { extendTailwindMerge } from 'tailwind-merge';

/**
 * class 合并。自定义 token 必须显式登记，否则 tailwind-merge 会把 `text-micro` 当成 text-color
 * 与 `text-ink-4` 互斥、把 `rounded-card` 当成未知值不与 `rounded-[3px]` 消解（见 tests/cn.test.ts）。
 */
const twMerge = extendTailwindMerge({
  extend: {
    classGroups: {
      'font-size': [{ text: ['micro', 'tiny', 'sm', 'body', 'md', 'lg', 'h2', 'h1'] }],
      rounded: [{ rounded: ['card'] }],
    },
  },
});

export function cn(...inputs: ClassValue[]): string {
  return twMerge(clsx(inputs));
}
