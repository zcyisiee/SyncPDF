import type { ReactNode } from 'react';

/** §8.1：顶栏只保留品牌与右侧「当前文档 + 状态徽标」，不承载屏切换；高度 44px。 */
export function Topbar({ meta }: { meta?: ReactNode }) {
  return (
    <header
      data-od-id="app-topbar"
      className="flex h-11 flex-none items-center gap-s4 border-b border-hair bg-parchment px-s5"
    >
      <div className="flex min-w-0 items-baseline gap-[7px]">
        <span className="font-serif text-md font-medium tracking-[-0.01em] text-ink">iee</span>
        <span className="font-serif text-md font-medium tracking-[-0.01em] text-ink">Translater</span>
        <span className="text-ink-4">·</span>
        <span className="whitespace-nowrap text-tiny tracking-[0.02em] text-ink-4">
          学术 PDF 翻译工作台
        </span>
      </div>
      {meta === undefined || meta === null ? null : (
        <div className="ml-auto flex min-w-0 items-center gap-s3 whitespace-nowrap">{meta}</div>
      )}
    </header>
  );
}
