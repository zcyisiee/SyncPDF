/**
 * 工作台外壳的 UI 状态：屏/视图路由镜像、三栏宽度、分隔条拖拽态、预览模式与 bbox 图层。
 * 宽度、折叠态、屏幕与 bbox 图层持久化到 localStorage，键名按 DESIGN.md §8.2 冻结
 * （`ieet.vrw`/`ieet.inspw`/`ieet.tlh`/`ieet.inspCollapsed`/`ieet.screen`/`ieet.bboxMode`），
 * 范围也按 §8.2 表 clamp；预览页码与选中段落只活在会话里（不持久化）。
 */
import { useStore } from 'zustand';
import { createStore, type StoreApi } from 'zustand/vanilla';

import type { BboxMode } from '../lib/preview';
import type { WorkbenchView, ScreenId } from '../lib/routing';

export type GutterId = 'viewrail' | 'inspector' | 'timeline';
export type PreviewMode = 'source' | 'target' | 'compare';

const BBOX_MODES: readonly BboxMode[] = ['parse', 'layout', 'off'];

export const STORAGE_KEYS = {
  viewrail: 'ieet.vrw',
  inspector: 'ieet.inspw',
  timeline: 'ieet.tlh',
  inspectorCollapsed: 'ieet.inspCollapsed',
  screen: 'ieet.screen',
  bboxMode: 'ieet.bboxMode',
} as const;

export interface LayoutSpec {
  /** 该栏宽度在本 store 里的字段名。 */
  key: 'viewrailWidth' | 'inspectorWidth' | 'timelineHeight';
  storageKey: string;
  /** 分隔条拖拽轴：x = 竖条（调列宽），y = 横条（调行高）。 */
  axis: 'x' | 'y';
  /** true = 指针朝轴正向移动时该栏变窄（右侧面板 / 时间线分隔条都在被调栏的右/下方）。 */
  invert: boolean;
  default: number;
  min: number;
  max: number;
  /** 键盘方向键步长（§8.2：列 16px / 时间线 8px）。 */
  step: number;
  label: string;
}

/** §8.2 表：默认 / 范围 / 轴 / localStorage 键，逐项照抄。 */
export const LAYOUT_SPECS = {
  viewrail: {
    key: 'viewrailWidth',
    storageKey: STORAGE_KEYS.viewrail,
    axis: 'x',
    invert: false,
    default: 220,
    min: 160,
    max: 320,
    step: 16,
    label: '调整视图栏宽度（160–320）',
  },
  inspector: {
    key: 'inspectorWidth',
    storageKey: STORAGE_KEYS.inspector,
    axis: 'x',
    invert: true,
    default: 360,
    min: 280,
    max: 560,
    step: 16,
    label: '调整右侧面板宽度（280–560）',
  },
  timeline: {
    key: 'timelineHeight',
    storageKey: STORAGE_KEYS.timeline,
    axis: 'y',
    invert: true,
    default: 96,
    min: 72,
    max: 160,
    step: 8,
    label: '调整时间线高度（72–160）',
  },
} as const satisfies Record<GutterId, LayoutSpec>;

export interface UiState {
  screen: ScreenId;
  viewrailWidth: number;
  inspectorWidth: number;
  timelineHeight: number;
  inspectorCollapsed: boolean;
  previewMode: PreviewMode;
  previewView: WorkbenchView | null;
  previewChoices: Partial<Record<WorkbenchView, { mode: PreviewMode; bbox: BboxMode }>>;
  previewZoom: number | null;
  compareLinked: boolean;
  enterPreviewView: (view: WorkbenchView) => void;
  setPreviewZoom: (zoom: number | null) => void;
  setCompareLinked: (linked: boolean) => void;
  /** 预览页码（1 基；**不**持久化，`resetPreviewForDocument` 在 did 变化时重置为 1）。 */
  previewPage: number;
  /** 预览状态当前绑定的 did（`resetPreviewForDocument` 靠它判断是否换文档）。 */
  previewDid: string | null;
  /** bbox 图层三态（持久化 `ieet.bboxMode`；默认值由视图决定，见 `bboxModeForView`）。 */
  bboxMode: BboxMode;
  /** 当前选中的段落 id（W05 只联动右侧面板占位；真内容 W10）。 */
  selectedParagraphId: string | null;
  /** W11 重译候选：上次用过的 profile id（会话内记忆，**不**持久化；换文档不丢）。 */
  retranslateProfile: string | null;
  /** 正在拖拽的分隔条（用于 `is-drag` 视觉态）。 */
  dragging: GutterId | null;
  setScreen: (screen: ScreenId) => void;
  /** 拖拽/键盘统一入口：clamp 到 §8.2 范围并持久化。 */
  setLayoutWidth: (id: GutterId, next: number) => void;
  setDragging: (id: GutterId | null) => void;
  /** 折叠右侧面板（`--inspw:0`）与 `ieet.inspCollapsed`。 */
  setInspectorCollapsed: (collapsed: boolean) => void;
  setPreviewMode: (mode: PreviewMode) => void;
  setPreviewPage: (page: number) => void;
  /** did 变化时重置会话内预览状态（页码回 1 + 清空选中）；同一 did 重复调用无副作用。 */
  resetPreviewForDocument: (did: string) => void;
  setBboxMode: (mode: BboxMode) => void;
  setSelectedParagraph: (id: string | null) => void;
  setRetranslateProfile: (id: string | null) => void;
}

const SCREENS: readonly ScreenId[] = ['library', 'glossary', 'settings', 'workbench'];

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}

/**
 * 分隔条位移 → 新宽度：先按该栏的 axis 方向（invert）定符号，再按 §8.2 范围 clamp。
 * 指针拖拽与方向键共用本函数（Gutter 负责把 clientX/Y 或按键转换成位移）。
 */
export function widthFromDelta(id: GutterId, base: number, delta: number): number {
  const spec = LAYOUT_SPECS[id];
  const moved = base + (spec.invert ? -delta : delta);
  return clamp(Math.round(moved), spec.min, spec.max);
}

function readStoredNumber(spec: LayoutSpec): number {
  try {
    const raw = window.localStorage.getItem(spec.storageKey);
    if (raw === null) return spec.default;
    const value = Number(raw);
    if (!Number.isFinite(value)) return spec.default;
    return clamp(Math.round(value), spec.min, spec.max);
  } catch {
    return spec.default;
  }
}

function readStoredFlag(key: string, fallback: boolean): boolean {
  try {
    const raw = window.localStorage.getItem(key);
    return raw === null ? fallback : raw === '1';
  } catch {
    return fallback;
  }
}

export function readStoredScreen(): ScreenId | null {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEYS.screen);
    return SCREENS.find((screen) => screen === raw) ?? null;
  } catch {
    return null;
  }
}

/** 用户显式选过的 bbox 图层模式；没选过 → null（PreviewArea 用视图默认值）。 */
export function readStoredBboxMode(): BboxMode | null {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEYS.bboxMode);
    return BBOX_MODES.find((mode) => mode === raw) ?? null;
  } catch {
    return null;
  }
}

function writeStored(key: string, value: string): void {
  try {
    window.localStorage.setItem(key, value);
  } catch {
    // 隐私模式/存储被禁：宽度只影响本次会话，不阻塞渲染
  }
}

export function createUiStore(): StoreApi<UiState> {
  return createStore<UiState>()((set, get) => ({
    screen: readStoredScreen() ?? 'library',
    viewrailWidth: readStoredNumber(LAYOUT_SPECS.viewrail),
    inspectorWidth: readStoredNumber(LAYOUT_SPECS.inspector),
    timelineHeight: readStoredNumber(LAYOUT_SPECS.timeline),
    inspectorCollapsed: readStoredFlag(STORAGE_KEYS.inspectorCollapsed, false),
    previewMode: 'target',
    previewView: null,
    previewChoices: {},
    previewZoom: null,
    compareLinked: true,
    enterPreviewView: (view) => {
      const state = get();
      if (state.previewView === view) return;
      const choices = { ...state.previewChoices };
      if (state.previewView !== null) choices[state.previewView] = { mode: state.previewMode, bbox: state.bboxMode };
      const choice = choices[view] ?? {
        mode: view === 'layout' ? 'source' : view === 'translate' ? 'target' : state.previewMode,
        bbox: view === 'translate' ? 'layout' : view === 'layout' ? 'parse' : state.bboxMode,
      };
      set({ previewView: view, previewChoices: choices, previewMode: choice.mode, bboxMode: choice.bbox });
    },
    setPreviewZoom: (zoom) => set({ previewZoom: zoom === null ? null : Number.isFinite(zoom) ? clamp(zoom, 0.1, 4) : get().previewZoom }),
    setCompareLinked: (compareLinked) => set({ compareLinked }),
    previewPage: 1,
    previewDid: null,
    bboxMode: readStoredBboxMode() ?? 'parse',
    selectedParagraphId: null,
    retranslateProfile: null,
    dragging: null,
    setScreen: (screen) => {
      writeStored(STORAGE_KEYS.screen, screen);
      set({ screen });
    },
    setLayoutWidth: (id, next) => {
      const spec = LAYOUT_SPECS[id];
      const value = clamp(Math.round(next), spec.min, spec.max);
      writeStored(spec.storageKey, String(value));
      if (id === 'viewrail') {
        set({ viewrailWidth: value });
        return;
      }
      if (id === 'inspector') {
        // 折叠态（--inspw:0）下拖分隔条只在宽度真变化时展开：避免「拖回原宽度才意外展开」。
        const collapsed = get().inspectorCollapsed && value === get().inspectorWidth;
        if (!collapsed) writeStored(STORAGE_KEYS.inspectorCollapsed, '0');
        set({ inspectorWidth: value, inspectorCollapsed: collapsed });
        return;
      }
      set({ timelineHeight: value });
    },
    setDragging: (dragging) => set({ dragging }),
    setInspectorCollapsed: (collapsed) => {
      writeStored(STORAGE_KEYS.inspectorCollapsed, collapsed ? '1' : '0');
      set({ inspectorCollapsed: collapsed });
    },
    setPreviewMode: (previewMode) => set({ previewMode }),
    setPreviewPage: (page) => set({ previewPage: Number.isFinite(page) ? Math.max(1, Math.round(page)) : 1 }),
    resetPreviewForDocument: (did) => {
      if (get().previewDid === did) return;
      set({ previewDid: did, previewPage: 1, selectedParagraphId: null });
    },
    setBboxMode: (bboxMode) => {
      writeStored(STORAGE_KEYS.bboxMode, bboxMode);
      set({ bboxMode });
    },
    setSelectedParagraph: (selectedParagraphId) => set({ selectedParagraphId }),
    setRetranslateProfile: (retranslateProfile) => set({ retranslateProfile }),
  }));
}

export const uiStore = createUiStore();

export function useUiStore<T>(selector: (state: UiState) => T): T {
  return useStore(uiStore, selector);
}
