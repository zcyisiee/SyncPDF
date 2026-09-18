import { useState } from 'react';
import { HARNESS_DEFAULT_KEY, useHarnessSelection } from '../lib/harnesses';
import { useProfiles } from '../lib/queries';
import { HarnessSelect } from '../components/jobs/HarnessSelect';
import { Button, LinkButton } from '../components/ui/Button';
import { ScrollArea } from '../components/ui/ScrollArea';
import { ScreenFrame } from '../components/shell/ScreenFrame';

export function SettingsScreen() {
  const profiles = useProfiles();
  const selection = useHarnessSelection(profiles.data ?? []);
  const [status, setStatus] = useState('');
  return <ScreenFrame>
    <ScrollArea className="h-full">
      <div className="w-full max-w-[900px] px-s7 pb-12 pt-s7">
        <h1 className="font-serif text-h1 font-medium text-ink">设置</h1>
        <h2 className="mt-s2 text-body font-medium text-ink-2">翻译配置</h2>
        <p className="mt-s2 text-body text-ink-2">使用本机已登录的 Pi 或 agy。选择模型和思考强度即可翻译。</p>
        <div className="mt-s5 flex flex-wrap items-end gap-s3 rounded border border-hair p-s4">
          <HarnessSelect selection={selection} />
          <Button disabled={!selection.profile || !profiles.isSuccess} onClick={() => {
            try {
              localStorage.setItem(HARNESS_DEFAULT_KEY, JSON.stringify({ profile: selection.profile, thinking: selection.thinking }));
              setStatus('默认选择已保存；未调用模型。');
            } catch { setStatus('无法保存默认选择，请检查浏览器存储权限。'); }
          }}>设为默认</Button>
        </div>
        <p className="mt-s3 text-tiny text-ink-3">思考强度仅显示模型支持的档位；DeepSeek Pro 默认 high。AI 审校默认关闭，本地检查仍会执行。</p>
        {profiles.isError && <p role="alert">模型读取失败。<Button onClick={() => void profiles.refetch()}>重试</Button></p>}
        {status && <p role="status" className="mt-s3 text-tiny">{status}</p>}
        <p className="mt-s5"><LinkButton href="#/library">返回文件库</LinkButton></p>
      </div>
    </ScrollArea>
  </ScreenFrame>;
}
