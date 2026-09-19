import { useState } from 'react';
import type { ProfileListItem } from '../api/types';

export const HARNESS_DEFAULT_KEY = 'ieet.translation-default';

function savedDefault(): { profile?: string; thinking?: string; dual?: boolean; useGlossary?: boolean; reviewer?: boolean; previewWorkers?: number } {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(HARNESS_DEFAULT_KEY) ?? '{}');
    return value && typeof value === 'object' ? value : {};
  } catch { return {}; }
}

/** job 提交用的默认偏好（模型/思考强度之外：dual、词表、AI 审校、预览并行数），存同一个 localStorage 键。 */
export interface TranslationDefaults {
  profile?: string;
  thinking?: string;
  dual: boolean;
  useGlossary: boolean;
  reviewer: boolean;
  previewWorkers: number;
}

/** 读 job 默认偏好：布尔字段带缺省（dual 关、词表开、审校关），坏了当没存过。 */
export function readTranslationDefaults(): TranslationDefaults {
  const saved = savedDefault();
  const workers = Number(saved.previewWorkers);
  return {
    profile: saved.profile,
    thinking: saved.thinking,
    dual: saved.dual === true,
    useGlossary: saved.useGlossary !== false,
    reviewer: saved.reviewer === true,
    previewWorkers: Number.isInteger(workers) && workers >= 1 && workers <= 8 ? workers : 8,
  };
}

/** 写 job 默认偏好（设置屏的「设为默认」）。localStorage 不可用时静默（只影响本次会话）。 */
export function saveTranslationDefaults(defaults: TranslationDefaults): boolean {
  try {
    localStorage.setItem(HARNESS_DEFAULT_KEY, JSON.stringify({
      profile: defaults.profile,
      thinking: defaults.thinking,
      dual: defaults.dual,
      useGlossary: defaults.useGlossary,
      reviewer: defaults.reviewer,
      previewWorkers: defaults.previewWorkers,
    }));
    return true;
  } catch { return false; }
}

/** Defaults are preferences only; the server validates every model/thinking pair. */
export function useHarnessSelection(items: ProfileListItem[]) {
  const [saved] = useState(savedDefault);
  const [profileOverride, setProfileOverride] = useState(saved.profile ?? '');
  const [thinkingOverride, setThinkingOverride] = useState(saved.thinking ?? '');
  const profiles = items.filter(item => item.builtin && item.has_translator);
  const selected = profiles.find(item => item.id === profileOverride) ?? profiles[0];
  const profile = selected?.id ?? '';
  const thinking = selected?.thinking_levels?.includes(thinkingOverride)
    ? thinkingOverride : selected?.default_thinking ?? '';
  const setProfile = (id: string) => {
    setProfileOverride(id);
    const next = profiles.find(item => item.id === id);
    if (!next?.thinking_levels?.includes(thinking)) setThinkingOverride(next?.default_thinking ?? '');
  };
  return { profiles, selected, profile, thinking, setProfile, setThinking: setThinkingOverride };
}
