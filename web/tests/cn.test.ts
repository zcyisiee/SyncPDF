import { describe, expect, it } from 'vitest';

import { cn } from '../src/lib/cn';

// 设计令牌（DESIGN.md §2.2 字号阶梯 / §3 圆角）是自定义 tailwind 值：
// 未在 cn 的 merge 配置里登记时会被误判成 text-color / 未知值，静默丢掉字号。
describe('cn（clsx + tailwind-merge 令牌登记）', () => {
  it('字号 token 与文字色 token 不互斥', () => {
    expect(cn('text-micro text-ink-4')).toBe('text-micro text-ink-4');
    expect(cn('text-h2 leading-[1.35] text-ink')).toBe('text-h2 leading-[1.35] text-ink');
  });

  it('同组后者覆盖（字号 vs 字号、背景 vs 背景）', () => {
    expect(cn('text-micro text-ink-4', 'text-tiny text-ink-3')).toBe('text-tiny text-ink-3');
    expect(cn('bg-run-soft text-run-ink', 'bg-sand')).toBe('text-run-ink bg-sand');
  });

  it('自定义圆角参与冲突消解', () => {
    expect(cn('rounded-[3px] rounded-card')).toBe('rounded-card');
    expect(cn('rounded-card rounded-[3px]')).toBe('rounded-[3px]');
  });
});
