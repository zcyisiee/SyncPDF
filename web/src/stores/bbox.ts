import { create } from 'zustand';
import { categoryKey, DEFAULT_VISIBILITY, type BboxVisibility } from '../lib/bbox';

const STYLE_KEY = 'ieet.bboxStyle';
const documentKey = (did: string) => `ieet.bboxVisibility.${encodeURIComponent(did)}`;
function read(key: string): unknown {
  try { return JSON.parse(window.localStorage.getItem(key) ?? 'null'); } catch { return null; }
}
function write(key: string, value: unknown) {
  try { window.localStorage.setItem(key, JSON.stringify(value)); } catch { /* Session controls still work. */ }
}
function record(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}
export function readVisibility(did: string): BboxVisibility {
  const value = read(documentKey(did));
  if (!record(value) || typeof value.defaultVisible !== 'boolean' || !record(value.overrides)) return DEFAULT_VISIBILITY;
  return { defaultVisible: value.defaultVisible, overrides: Object.fromEntries(
    Object.entries(value.overrides).filter((entry): entry is [string, boolean] => typeof entry[1] === 'boolean'),
  ) };
}
function bounded(value: unknown, fallback: number, min: number, max: number) {
  return typeof value === 'number' && Number.isFinite(value) ? Math.min(max, Math.max(min, value)) : fallback;
}
function readStyle() {
  const value = read(STYLE_KEY);
  return { strokeWidth: bounded(record(value) ? value.strokeWidth : null, 1.5, 0.5, 4),
    fillOpacity: bounded(record(value) ? value.fillOpacity : null, 0.08, 0, 0.4) };
}
interface BboxState {
  documents: Record<string, BboxVisibility>;
  strokeWidth: number;
  fillOpacity: number;
  setCategory: (did: string, label: string | null, visible: boolean) => void;
  setAll: (did: string, visible: boolean) => void;
  setStyle: (style: { strokeWidth?: number; fillOpacity?: number }) => void;
}
export const useBboxStore = create<BboxState>((set, get) => ({
  documents: {}, ...readStyle(),
  setCategory: (did, label, visible) => {
    const previous = get().documents[did] ?? readVisibility(did);
    const value = { ...previous, overrides: { ...previous.overrides, [categoryKey(label)]: visible } };
    write(documentKey(did), value);
    set({ documents: { ...get().documents, [did]: value } });
  },
  setAll: (did, visible) => {
    const value = { defaultVisible: visible, overrides: {} };
    write(documentKey(did), value);
    set({ documents: { ...get().documents, [did]: value } });
  },
  setStyle: (style) => {
    const value = { strokeWidth: bounded(style.strokeWidth, get().strokeWidth, 0.5, 4),
      fillOpacity: bounded(style.fillOpacity, get().fillOpacity, 0, 0.4) };
    write(STYLE_KEY, value);
    set(value);
  },
}));
