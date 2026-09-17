import type { ReactNode } from 'react';

import { IconRail } from './IconRail';
import { Topbar } from './Topbar';

/**
 * 屏外壳：顶栏（44px）+ 图标栏（56px，一级导航：文件库/词表/设置）+ 内容区。
 * 文件库/词表/设置只有内容区（视图栏、右侧面板、时间线属于工作台，见 DESIGN.md §3）。
 */
export function ScreenFrame({ meta, children }: { meta?: ReactNode; children: ReactNode }) {
  return (
    <div className="flex h-full min-h-0 flex-col">
      <Topbar meta={meta} />
      <div className="flex min-h-0 flex-1">
        <IconRail />
        <div className="flex min-h-0 min-w-0 flex-1 flex-col">{children}</div>
      </div>
    </div>
  );
}
