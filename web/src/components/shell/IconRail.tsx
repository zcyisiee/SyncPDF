import type { ScreenId } from '../../lib/routing';
import { hashForScreen } from '../../lib/routing';
import { cn } from '../../lib/cn';
import { useUiStore } from '../../stores/ui';
import { Icon, type IconName } from '../icons';

const RAIL_ITEMS: { screen: ScreenId; label: string; icon: IconName; matches: ScreenId[] }[] = [
  { screen: 'library', label: '文件库', icon: 'library', matches: ['library', 'workbench'] },
  { screen: 'glossary', label: '词表', icon: 'glossary', matches: ['glossary'] },
  { screen: 'settings', label: '设置', icon: 'settings', matches: ['settings'] },
];

/**
 * §8.1 一级导航：56px 图标栏，36px 视觉按钮 + 8px `::before` 命中扩展（§7.4）。
 * 高亮表示所在分组（工作台沿用「文件库」高亮）。
 */
export function IconRail() {
  const screen = useUiStore((state) => state.screen);
  return (
    <nav
      aria-label="全局导航"
      data-od-id="icon-rail"
      className="flex w-14 flex-none flex-col items-center gap-s2 border-r border-hair bg-sand py-s3"
    >
      {RAIL_ITEMS.map((item) => {
        const active = item.matches.includes(screen);
        return (
          <a
            key={item.screen}
            href={hashForScreen(item.screen)}
            title={item.label}
            aria-label={item.label}
            aria-current={active ? 'page' : undefined}
            data-rail-item={item.screen}
            className={cn(
              'relative grid h-9 w-9 place-items-center rounded text-ink-3 transition-colors',
              "before:absolute before:-inset-1 before:content-['']",
              'hover:bg-sand-2 hover:text-ink',
              active && 'bg-ivory text-accent shadow-ring hover:bg-ivory hover:text-accent',
            )}
          >
            <Icon name={item.icon} />
          </a>
        );
      })}
    </nav>
  );
}
