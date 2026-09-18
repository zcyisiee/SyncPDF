import type { ScreenId } from '../../lib/routing';
import { hashForScreen } from '../../lib/routing';
import { cn } from '../../lib/cn';
import { useGlossary } from '../../lib/queries';
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
 *
 * W13：词表项带**条数徐标**（有词表时才显示）—— 条数来自 `GET /glossary`，拿不到（服务
 * 未就绪/请求失败）就不显示徐标，不诈报 0 条。
 */
export function IconRail() {
  const screen = useUiStore((state) => state.screen);
  const glossaryCount = useGlossary().data?.count ?? 0;
  return (
    <nav
      aria-label="全局导航"
      data-od-id="icon-rail"
      className="flex w-14 flex-none flex-col items-center gap-s2 border-r border-hair bg-sand py-s3"
    >
      {RAIL_ITEMS.map((item) => {
        const active = item.matches.includes(screen);
        const badge = item.screen === 'glossary' ? glossaryCount : 0;
        return (
          <a
            key={item.screen}
            href={hashForScreen(item.screen)}
            title={badge > 0 ? `${item.label}（${badge} 条）` : item.label}
            aria-label={item.label}
            aria-current={active ? 'page' : undefined}
            data-rail-item={item.screen}
            data-badge={badge > 0 ? String(badge) : undefined}
            className={cn(
              'relative grid h-9 w-9 place-items-center rounded text-ink-3 transition-colors',
              "before:absolute before:-inset-1 before:content-['']",
              'hover:bg-sand-2 hover:text-ink',
              active && 'bg-ivory text-accent shadow-ring hover:bg-ivory hover:text-accent',
            )}
          >
            <Icon name={item.icon} />
            {badge > 0 ? (
              <span
                data-od-id="rail-glossary-badge"
                className="absolute -right-1 -top-1 min-w-[14px] rounded-full bg-accent px-[3px] text-center font-mono text-micro leading-[14px] text-accent-on"
              >
                {badge}
              </span>
            ) : null}
          </a>
        );
      })}
    </nav>
  );
}
