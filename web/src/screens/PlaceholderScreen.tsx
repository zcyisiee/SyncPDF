import { ScrollArea } from '../components/ui/ScrollArea';
import { ScreenFrame } from '../components/shell/ScreenFrame';
import { SettingsScreen } from './SettingsScreen';
import { LinkButton } from '../components/ui/Button';
export function PlaceholderScreen({ screen }: { screen: 'glossary' | 'settings' }) { return screen === 'settings' ? <SettingsScreen /> : <SimplePlaceholder />; }
function SimplePlaceholder() { return <ScreenFrame><ScrollArea className="h-full"><div className="p-s7"><h1 className="font-serif text-h1">词表</h1><p>词表已是真屏。</p><LinkButton href="#/library">返回文件库</LinkButton></div></ScrollArea></ScreenFrame>; }
export function UnknownScreen({ hash }: { hash: string }) { return <ScreenFrame><ScrollArea className="h-full"><div className="p-s7"><h1 className="font-serif text-h1">页面不存在</h1><p className="text-body text-ink-3">{hash || '(空 hash)'}</p><LinkButton href="#/library">返回文件库</LinkButton></div></ScrollArea></ScreenFrame>; }
