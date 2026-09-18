import { useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import type { ModelConfiguration } from '../api/types';
import { apiDelete, apiPost, apiPut } from '../lib/api';
import { modelError, modelsKey, useModels } from '../lib/models';
import { queryKeys, useProfiles } from '../lib/queries';
import { Button, LinkButton } from '../components/ui/Button';
import { ScrollArea } from '../components/ui/ScrollArea';
import { ScreenFrame } from '../components/shell/ScreenFrame';

const empty = { id: '', label: '', base_url: 'https://api.openai.com/v1', model: '', api_key: '' };
const fieldLabels = { id: '配置标识', label: '显示名称', base_url: 'Base URL', model: '模型名称', api_key: 'API Key' };
const inputClass = 'h-8 min-w-0 rounded border border-hair bg-ivory px-2 text-sm';

export function SettingsScreen() {
  const client = useQueryClient();
  const models = useModels();
  const profiles = useProfiles();
  const [form, setForm] = useState(empty);
  const [editing, setEditing] = useState<string | null>(null);
  const [status, setStatus] = useState('');
  const [busy, setBusy] = useState(false);
  const [script, setScript] = useState({ id: '', label: '', translator_script: '', reviewer_script: '' });
  const scripts = (profiles.data ?? []).filter(p => !(models.data ?? []).some(m => m.id === p.id));

  const refresh = () => Promise.all([
    client.invalidateQueries({ queryKey: modelsKey }),
    client.invalidateQueries({ queryKey: queryKeys.profiles }),
  ]);
  const perform = async (operation: () => Promise<unknown>, message: string, after?: () => void) => {
    setBusy(true);
    setStatus('正在处理…');
    try {
      await operation();
      after?.();
      await refresh();
      setStatus(message);
    } catch (error) {
      setStatus(modelError(error));
    } finally {
      setBusy(false);
    }
  };
  const reset = () => { setEditing(null); setForm(empty); };
  const save = () => perform(() => apiPut('/models', {
    id: form.id.trim(), label: form.label.trim(), base_url: form.base_url.trim(), model: form.model.trim(),
    ...(form.api_key ? { api_key: form.api_key } : {}),
  }), '配置已保存；未调用模型。', reset);
  const edit = (model: ModelConfiguration) => {
    setEditing(model.id);
    setForm({ id: model.id, label: model.label, base_url: model.base_url, model: model.model, api_key: '' });
    setStatus('编辑已保存配置；API Key 留空将保留原值。');
  };
  const saveScript = () => perform(() => apiPut('/profiles', {
    id: script.id.trim(), label: script.label.trim() || script.id.trim(),
    translator_script: script.translator_script.trim(),
    ...(script.reviewer_script.trim() ? { reviewer_script: script.reviewer_script.trim() } : {}),
  }), '高级脚本配置已保存。');
  const modelValid = /^[a-z0-9-]{1,64}$/.test(form.id) && !!form.label.trim() && !!form.model.trim() && !!form.base_url.trim();
  const scriptValid = /^[a-z0-9-]{1,64}$/.test(script.id) && script.translator_script.startsWith('scripts/') &&
    !(models.data ?? []).some(m => m.id === script.id);

  return (
    <ScreenFrame>
      <ScrollArea className="h-full">
        <div className="w-full max-w-[900px] px-s7 pb-12 pt-s7">
          <h1 className="font-serif text-h1 font-medium text-ink">设置</h1>
          <h2 className="mt-s2 text-body font-medium text-ink-2">翻译配置</h2>
          <p className="mt-s2 text-body text-ink-2">选择翻译使用的模型服务。支持 OpenAI 兼容接口；高级用户也可使用服务器脚本。</p>
          <p className="mt-s2 text-tiny text-ink-3">API Key 由本机后端保存，文件仅当前系统用户可读，不会回传明文；文件权限保护不等于加密。</p>
          <section className="mt-s5 grid gap-s3 rounded border border-hair bg-parchment p-s4">
            <h3 className="font-medium">{editing ? '编辑模型配置' : '新建模型配置'}</h3>
            {(Object.keys(empty) as (keyof typeof empty)[]).map(key => (
              <label key={key} className="flex flex-col gap-1 text-tiny text-ink-3">
                {fieldLabels[key]}
                <input type={key === 'api_key' ? 'password' : 'text'} value={form[key]}
                  disabled={busy || (key === 'id' && editing !== null)}
                  autoComplete={key === 'api_key' ? 'new-password' : 'off'}
                  placeholder={key === 'api_key' ? (editing ? '留空保留已保存 Key' : '本地免密服务可留空') : key === 'id' ? '小写字母、数字和连字符' : ''}
                  onChange={event => setForm({ ...form, [key]: event.target.value })} className={inputClass} />
              </label>
            ))}
            <div className="flex gap-s3">
              <Button disabled={busy || !modelValid} variant="primary" onClick={() => void save()}>保存配置</Button>
              {editing && <Button disabled={busy} onClick={reset}>取消编辑</Button>}
            </div>
            <p className="text-tiny text-ink-3">保存不会调用模型。手动测试连接会发送短生成请求，可能产生费用。</p>
          </section>
          {status && <p role="status" className="mt-s3 text-tiny text-ink-2">{status}</p>}
          <section className="mt-s5 flex flex-col gap-s3">
            <h3 className="font-medium">已保存模型</h3>
            {models.isPending && <p>正在读取配置…</p>}
            {models.isError && <div role="alert">配置读取失败。<Button onClick={() => void models.refetch()}>重试读取模型</Button></div>}
            {models.data?.map(model => (
              <div key={model.id} className="flex flex-wrap items-center gap-s3 rounded border border-hair p-s3" aria-label={`配置 ${model.label}`}>
                <span>{model.label}</span>
                <span className="break-all font-mono text-tiny text-ink-3">{model.model} · {model.has_api_key ? 'API Key 已保存' : '未设置 Key'}</span>
                <Button size="sm" disabled={busy} onClick={() => edit(model)}>编辑</Button>
                <Button size="sm" disabled={busy} onClick={() => void perform(
                  () => apiPost(`/models/${encodeURIComponent(model.id)}/test`), '连接测试成功。',
                )}>测试连接</Button>
                <Button size="sm" variant="danger" disabled={busy} onClick={() => void perform(
                  () => apiDelete(`/models/${encodeURIComponent(model.id)}`), '配置已删除。',
                  () => { if (editing === model.id) reset(); },
                )}>删除</Button>
              </div>
            ))}
            {models.isSuccess && models.data.length === 0 && <p>还没有翻译配置，请先创建模型配置。</p>}
          </section>
          <details className="mt-s6 rounded border border-hair p-s4">
            <summary className="cursor-pointer font-medium">高级：脚本配置</summary>
            <p className="mt-2 text-tiny text-ink-3">脚本必须已存在于服务器允许的 scripts/ 目录。这里只接受脚本路径，不接受命令。相同标识会更新配置；留空审校路径保留原设置。</p>
            {(profiles.isPending || models.isPending) && <p>正在读取脚本配置…</p>}
            {(profiles.isError || models.isError) && <p role="alert">无法确认脚本清单，请重试读取配置。</p>}
            {profiles.isSuccess && models.isSuccess && scripts.map(profile => <p key={profile.id} className="font-mono text-tiny">{profile.label} · {profile.id}</p>)}
            <div className="mt-s3 grid gap-s3">
              {([['id', '脚本配置标识'], ['label', '脚本显示名称'], ['translator_script', '翻译脚本路径'], ['reviewer_script', '审校脚本路径（可选）']] as const).map(([key, label]) => (
                <label key={key} className="flex flex-col gap-1 text-tiny">{label}
                  <input value={script[key]} disabled={busy} onChange={event => setScript({ ...script, [key]: event.target.value })} className={inputClass} />
                </label>
              ))}
              <Button disabled={busy || !scriptValid || !models.isSuccess} onClick={() => void saveScript()}>保存脚本配置</Button>
            </div>
          </details>
          <p className="mt-s5"><LinkButton href="#/library">返回文件库</LinkButton></p>
        </div>
      </ScrollArea>
    </ScreenFrame>
  );
}
