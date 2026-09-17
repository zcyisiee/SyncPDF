import { LinkButton } from '../components/ui/Button';
import { ScrollArea } from '../components/ui/ScrollArea';
import { ScreenFrame } from '../components/shell/ScreenFrame';

/** 设置占位屏：标题 + 「后续版本提供」说明（W13 已把词表从占位换成真屏）。 */
export function PlaceholderScreen({ screen }: { screen: 'glossary' | 'settings' }) {
  const label = screen === 'glossary' ? '词表' : '设置';
  const note =
    screen === 'glossary'
      ? '词表已是真屏（见 src/screens/GlossaryScreen.tsx）：全局术语表的增删改查与 CSV 导入导出。'
      : '模型 profile 与检查阈值设置将在后续版本接入。';
  return (
    <ScreenFrame>
      <ScrollArea className="h-full" data-od-id={`screen-${screen}`}>
        <div className="w-full max-w-[1220px] px-s7 pb-12 pt-s7">
          <h1 className="font-serif text-h1 font-medium leading-[1.3] text-ink">{label}</h1>
          <p className="mt-s3 max-w-[52ch] text-body text-ink-2">{note}</p>
          <p className="mt-s2 font-mono text-tiny text-ink-4">后续版本提供</p>
          <p className="mt-s5">
            <LinkButton href="#/library">返回文件库</LinkButton>
          </p>
        </div>
      </ScrollArea>
    </ScreenFrame>
  );
}

/** 未知 hash：给出明确出口，不留白屏。 */
export function UnknownScreen({ hash }: { hash: string }) {
  return (
    <ScreenFrame>
      <ScrollArea className="h-full" data-od-id="screen-unknown">
        <div className="w-full max-w-[1220px] px-s7 pb-12 pt-s7">
          <h1 className="font-serif text-h1 font-medium leading-[1.3] text-ink">页面不存在</h1>
          <p className="mt-s3 max-w-[52ch] text-body text-ink-2">
            这个地址没有对应的屏或视图。
          </p>
          <p className="mt-s2 break-all font-mono text-tiny text-ink-4">{hash === '' ? '(空 hash)' : hash}</p>
          <p className="mt-s5">
            <LinkButton href="#/library">返回文件库</LinkButton>
          </p>
        </div>
      </ScrollArea>
    </ScreenFrame>
  );
}
