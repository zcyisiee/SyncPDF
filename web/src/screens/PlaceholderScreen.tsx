import { ScrollArea } from '../components/ui/ScrollArea';
import { LinkButton } from '../components/ui/Button';

/** 未知 hash 的明确出口（中栏内容；左栏导航由 App 常驻）。 */
export function UnknownScreen({ hash }: { hash: string }) {
  return (
    <ScrollArea className="h-full" data-od-id="screen-unknown">
      <div className="p-s7">
        <h1 className="font-serif text-h1">页面不存在</h1>
        <p className="text-body text-ink-3">{hash || '(空 hash)'}</p>
        <LinkButton href="#/library">返回文件库</LinkButton>
      </div>
    </ScrollArea>
  );
}
