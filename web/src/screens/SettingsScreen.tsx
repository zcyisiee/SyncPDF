import { useState } from 'react';
import {
  readTranslationDefaults,
  saveTranslationDefaults,
  useHarnessSelection,
} from '../lib/harnesses';
import { useGlossary as useGlossaryQuery, useProfiles } from '../lib/queries';
import { HarnessSelect } from '../components/jobs/HarnessSelect';
import { Button, LinkButton } from '../components/ui/Button';
import { ScrollArea } from '../components/ui/ScrollArea';
import { ScreenFrame } from '../components/shell/ScreenFrame';

/**
 * `#/settings` 设置屏：翻译的全部默认偏好都在这里设好（工作台顶栏的「开始翻译」直接用），
 * 不在工作台重复展示配置：
 *
 * - 翻译模型 + 思考强度（`useHarnessSelection`，只列内置且带 translator 的 profile）；
 * - 生成 dual（双语 PDF）/ 使用全局词表 / AI 审校 三个开关；
 * - 「设为默认」把整套偏好写进 `localStorage[ieet.translation-default]`（不调用模型，
 *   服务端在提交 job 时仍会校验 profile/thinking 组合）。
 */
export function SettingsScreen() {
  const profiles = useProfiles();
  const glossary = useGlossaryQuery();
  const selection = useHarnessSelection(profiles.data ?? []);
  const [initial] = useState(readTranslationDefaults);
  const [dual, setDual] = useState(initial.dual);
  const [useGlossary, setUseGlossary] = useState(initial.useGlossary);
  const [reviewer, setReviewer] = useState(initial.reviewer);
  const [status, setStatus] = useState('');
  const glossaryCount = glossary.data?.count ?? 0;
  const glossaryEmpty = glossary.data !== undefined && glossaryCount === 0;
  return <ScreenFrame>
    <ScrollArea className="h-full">
      <div className="w-full max-w-[900px] px-s7 pb-12 pt-s7">
        <h1 className="font-serif text-h1 font-medium text-ink">设置</h1>
        <h2 className="mt-s2 text-body font-medium text-ink-2">翻译偏好</h2>
        <p className="mt-s2 text-body text-ink-2">
          使用本机已登录的 Pi 或 agy。这里设好默认配置后，工作台点「开始翻译」即可；每次任务不再重复选。
        </p>
        <div className="mt-s5 flex flex-col gap-s4 rounded border border-hair p-s4">
          <div className="flex flex-wrap items-end gap-s3">
            <HarnessSelect selection={selection} />
          </div>
          <div className="flex flex-col gap-s2 text-body text-ink-2">
            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                data-od-id="settings-dual"
                checked={dual}
                onChange={(event) => setDual(event.target.checked)}
              />
              生成 dual（双语 PDF）
            </label>
            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                data-od-id="settings-use-glossary"
                checked={useGlossary}
                onChange={(event) => setUseGlossary(event.target.checked)}
              />
              使用全局词表（<a className="underline" href="#/glossary">编辑词表</a>；
              <span data-od-id="settings-glossary-count" className="font-mono text-ink-4">
                {glossaryEmpty ? '（词表为空）' : `${glossaryCount} 条`}
              </span>
              ）
            </label>
            <label className="flex items-center gap-2">
              <input
                type="checkbox"
                data-od-id="settings-reviewer"
                checked={reviewer}
                onChange={(event) => setReviewer(event.target.checked)}
              />
              AI 审校（使用相同模型与思考强度额外审查文本，不检查 PDF 视觉效果）
            </label>
          </div>
          <div>
            <Button
              disabled={!selection.profile || !profiles.isSuccess}
              onClick={() => {
                const ok = saveTranslationDefaults({
                  profile: selection.profile,
                  thinking: selection.thinking,
                  dual,
                  useGlossary,
                  reviewer,
                });
                setStatus(ok ? '默认偏好已保存；未调用模型。' : '无法保存默认偏好，请检查浏览器存储权限。');
              }}
            >
              设为默认
            </Button>
          </div>
        </div>
        <p className="mt-s3 text-tiny text-ink-3">
          思考强度仅显示模型支持的档位；DeepSeek Pro 默认 high。页码范围与起点阶段由服务端自动判断（默认全部页、已有解析产物时从翻译续跑）。
        </p>
        {profiles.isError && <p role="alert">模型读取失败。<Button onClick={() => void profiles.refetch()}>重试</Button></p>}
        {status && <p role="status" className="mt-s3 text-tiny">{status}</p>}
        <p className="mt-s5"><LinkButton href="#/library">返回文件库</LinkButton></p>
      </div>
    </ScrollArea>
  </ScreenFrame>;
}
