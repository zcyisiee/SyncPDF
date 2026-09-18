import { useState } from 'react';
import type { ProfileListItem } from '../api/types';

export const HARNESS_DEFAULT_KEY = 'ieet.translation-default';

function savedDefault(): { profile?: string; thinking?: string } {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(HARNESS_DEFAULT_KEY) ?? '{}');
    return value && typeof value === 'object' ? value : {};
  } catch { return {}; }
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
