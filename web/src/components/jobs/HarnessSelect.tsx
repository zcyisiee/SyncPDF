import type { useHarnessSelection } from '../../lib/harnesses';

export function HarnessSelect({ selection, disabled = false, label = '翻译模型', idPrefix = 'translation' }: {
  selection: ReturnType<typeof useHarnessSelection>;
  disabled?: boolean;
  label?: string;
  idPrefix?: string;
}) {
  const { profiles, selected, profile, thinking, setProfile, setThinking } = selection;
  return <>
    <label className="flex min-w-0 flex-col gap-1 text-tiny text-ink-3">{label}
      <select data-od-id={`${idPrefix}-profile`} value={profile} disabled={disabled || !profiles.length}
        onChange={event => setProfile(event.target.value)} className="h-7 rounded border border-hair bg-ivory px-2 text-sm">
        {!profiles.length && <option value="">正在读取模型…</option>}
        {profiles.map(item => <option key={item.id} value={item.id}>{item.label}</option>)}
      </select>
    </label>
    <label className="flex min-w-0 flex-col gap-1 text-tiny text-ink-3">思考强度
      <select data-od-id={`${idPrefix}-thinking`} value={thinking} disabled={disabled || !selected}
        onChange={event => setThinking(event.target.value)} className="h-7 rounded border border-hair bg-ivory px-2 text-sm">
        {(selected?.thinking_levels ?? []).map(level => <option key={level} value={level}>{level}</option>)}
      </select>
    </label>
    {selected && <span className="font-mono text-tiny text-ink-4">{selected.model}</span>}
  </>;
}
