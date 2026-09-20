/**
 * 工作台外壳的 UI 状态：屏/视图路由镜像、栏宽、分隔条拖拽态、预览模式与 bbox 图层。
 * 宽度、折叠态、屏幕与 bbox 图层持久化到 localStorage，键名按 DESIGN.md §8.2 冻结
 * （`ieet.inspw`/`ieet.tlh`/`ieet.inspCollapsed`/`ieet.screen`/`ieet.bboxMode`），
 * 范围也按 §8.2 表 clamp；预览页码与选中段落只活在会话里（不持久化）。
 *
 * 注：旧版的 220px 视图栏（`ieet.vrw`）已删除 —— 二级视图合并成同一个工作台，
 * 预览占满除右栏/时间线外的全部宽度；预览模式与 bbox 图层也不再按视图记默认值。
 */
import { useStore } from 'zustand';
import { createStore, type StoreApi } from 'zustand/vanilla';

import type { BboxMode } from '../lib/preview';
import type { ScreenId } from '../lib/routing';

export type GutterId = 'inspector' | 'timeline';
export type PreviewMode = 'source' | 'target' | 'compare';

const BBOX_MODES: readonly BboxMode[] = ['parse', 'layout', 'off'];

export const STORAGE_KEYS = {
  inspector: 'ieet.inspw',
  timeline: 'ieet.tlh',
  timelineCollapsed: 'ieet.timelineCollapsed',
  inspectorCollapsed: 'ieet.inspCollapsed',
  screen: 'ieet.screen',
  bboxMode: 'ieet.bboxMode',
} as const;

export interface LayoutSpec {
  /** 该栏宽度在本 store 里的字段名。 */
  key: 'inspectorWidth' | 'timelineHeight';
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

export interface ParagraphSelectOptions {
  /** shift 语义：已在集合中 → 移除，否则追加；移除后主选中 = 剩余最后一个。 */
  extend?: boolean;
}

/** 文件库卡片的右键菜单位置（`did` + 视口坐标，`position: fixed` 用）。 */
export interface LibraryMenu {
  did: string;
  x: number;
  y: number;
}

export interface UiState {
  screen: ScreenId;
  inspectorWidth: number;
  timelineHeight: number;
  timelineCollapsed: boolean;
  inspectorCollapsed: boolean;
  previewMode: PreviewMode;
  previewZoom: number | null;
  compareLinked: boolean;
  setPreviewZoom: (zoom: number | null) => void;
  setCompareLinked: (linked: boolean) => void;
  /** 预览页码（1 基；**不**持久化，`resetPreviewForDocument` 在 did 变化时重置为 1）。 */
  previewPage: number;
  /** 预览状态当前绑定的 did（`resetPreviewForDocument` 靠它判断是否换文档）。 */
  previewDid: string | null;
  /** bbox 图层三态（持久化 `ieet.bboxMode`；默认段落框，用户可切）。 */
  bboxMode: BboxMode;
  /**
   * 段落多选集合（shift 逐次点击 toggle，按点击顺序、无重复）。只活在会话里（不持久化）。
   * `selectedParagraphId` = 集合的最后一个元素（右栏编辑器跟随的「主选中段」），
   * 由 store 内部同步维护——读方（InspectorPanel/CompileBar）语义不变。
   */
  selectedParagraphIds: string[];
  /** 当前选中的段落 id（= `selectedParagraphIds` 最后一个；无选择 → null）。 */
  selectedParagraphId: string | null;
  /** W11 重译候选：上次用过的 profile id（会话内记忆，**不**持久化；换文档不丢）。 */
  retranslateProfile: string | null;
  /** 正在拖拽的分隔条（用于 `is-drag` 视觉态）。 */
  dragging: GutterId | null;
  /**
   * 文件库卡片的右键菜单（`did` + 视口坐标）；**同一时刻最多一个**。
   * 放在全局 store 而不是每张卡片自己的 state：卡片各自持菜单会同时开出好几个
   * （右键第二张卡片时第一张的还开着），既没有交互意义也难收拾。
   */
  libraryMenu: LibraryMenu | null;
  openLibraryMenu: (did: string, at: { x: number; y: number }) => void;
  closeLibraryMenu: () => void;
  setScreen: (screen: ScreenId) => void;
  /** 拖拽/键盘统一入口：clamp 到 §8.2 范围并持久化。 */
  setLayoutWidth: (id: GutterId, next: number) => void;
  setDragging: (id: GutterId | null) => void;
  setTimelineCollapsed: (collapsed: boolean) => void;
  /** 折叠右侧面板（`--inspw:0`）与 `ieet.inspCollapsed`。 */
  setInspectorCollapsed: (collapsed: boolean) => void;
  setPreviewMode: (mode: PreviewMode) => void;
  setPreviewPage: (page: number) => void;
  /** did 变化时重置会话内预览状态（页码回 1 + 清空选中）；同一 did 重复调用无副作用。 */
  resetPreviewForDocument: (did: string) => void;
  setBboxMode: (mode: BboxMode) => void;
  /** 统一选中入口：`extend` = shift 多选语义，否则重置为单选 `[id]`。 */
  selectParagraph: (id: string, opts?: ParagraphSelectOptions) => void;
  /**
   * 把多选集合里的某段挪到末尾（= 设为「主选中段」，编辑器随之跟随）；不在集合中则忽略，
   * 已在末尾不 set（避免无谓重渲染）。用于段落 tab 的块切换 chips。
   */
  focusParagraph: (id: string) => void;
  /** 清空多选（`selectedParagraphId` 一并 → null）。 */
  clearParagraphSelection: () => void;
  /** 兼容入口（单选时代的调用方继续可用）：null → 清空，否则 `[id]`。 */
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

/**
 * 多选写回：一次 set 同步两个字段——`selectedParagraphId` 永远等于集合的最后一个元素，
 * 保证只读 `selectedParagraphId` 的旧读方（InspectorPanel/CompileBar）无需感知多选。
 */
function selectionPatch(ids: string[]): Pick<UiState, 'selectedParagraphId' | 'selectedParagraphIds'> {
  return { selectedParagraphIds: ids, selectedParagraphId: ids.length > 0 ? ids[ids.length - 1] : null };
}

export function createUiStore(): StoreApi<UiState> {
  return createStore<UiState>()((set, get) => ({
    screen: readStoredScreen() ?? 'library',
    inspectorWidth: readStoredNumber(LAYOUT_SPECS.inspector),
    timelineHeight: readStoredNumber(LAYOUT_SPECS.timeline),
    timelineCollapsed: readStoredFlag(STORAGE_KEYS.timelineCollapsed, false),
    inspectorCollapsed: readStoredFlag(STORAGE_KEYS.inspectorCollapsed, false),
    previewMode: 'target',
    previewZoom: null,
    compareLinked: true,
    setPreviewZoom: (zoom) => set({ previewZoom: zoom === null ? null : Number.isFinite(zoom) ? clamp(zoom, 0.1, 4) : get().previewZoom }),
    setCompareLinked: (compareLinked) => set({ compareLinked }),
    previewPage: 1,
    previewDid: null,
    bboxMode: readStoredBboxMode() ?? 'parse',
    selectedParagraphIds: [],
    selectedParagraphId: null,
    retranslateProfile: null,
    dragging: null,
    libraryMenu: null,
    openLibraryMenu: (did, at) => set({ libraryMenu: { did, x: at.x, y: at.y } }),
    closeLibraryMenu: () => {
      // 已经是关的就别 set（避免订阅方无谓重渲染）
      if (get().libraryMenu === null) return;
      set({ libraryMenu: null });
    },
    setScreen: (screen) => {
      writeStored(STORAGE_KEYS.screen, screen);
      set({ screen });
    },
    setLayoutWidth: (id, next) => {
      const spec = LAYOUT_SPECS[id];
      const value = clamp(Math.round(next), spec.min, spec.max);
      writeStored(spec.storageKey, String(value));
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
    setTimelineCollapsed: (collapsed: boolean) => {
      writeStored(STORAGE_KEYS.timelineCollapsed, collapsed ? '1' : '0');
      set({ timelineCollapsed: collapsed });
    },
    setInspectorCollapsed: (collapsed) => {
      writeStored(STORAGE_KEYS.inspectorCollapsed, collapsed ? '1' : '0');
      set({ inspectorCollapsed: collapsed });
    },
    setPreviewMode: (previewMode) => set({ previewMode }),
    setPreviewPage: (page) => set({ previewPage: Number.isFinite(page) ? Math.max(1, Math.round(page)) : 1 }),
    resetPreviewForDocument: (did) => {
      if (get().previewDid === did) return;
      // 段落 id 属于某个文档：换文档时页码与两个选中字段一起清空
      set({ previewDid: did, previewPage: 1, ...selectionPatch([]) });
    },
    setBboxMode: (bboxMode) => {
      writeStored(STORAGE_KEYS.bboxMode, bboxMode);
      set({ bboxMode });
    },
    selectParagraph: (id, opts) => {
      const current = get().selectedParagraphIds;
      let next: string[];
      if (opts?.extend) {
        next = current.includes(id) ? current.filter((item) => item !== id) : [...current, id];
      } else if (current.length === 1 && current[0] === id) {
        // 已是单选该段：保持原数组引用，避免无谓的订阅方重渲染
        return;
      } else {
        next = [id];
      }
      set(selectionPatch(next));
    },
    clearParagraphSelection: () => {
      if (get().selectedParagraphIds.length === 0) return;
      set(selectionPatch([]));
    },
    focusParagraph: (id) => {
      const current = get().selectedParagraphIds;
      if (!current.includes(id)) return;
      if (current[current.length - 1] === id) return;
      set(selectionPatch([...current.filter((item) => item !== id), id]));
    },
    setSelectedParagraph: (id) => {
      if (id === null) get().clearParagraphSelection();
      else get().selectParagraph(id);
    },
    setRetranslateProfile: (retranslateProfile) => set({ retranslateProfile }),
  }));
}

export const uiStore = createUiStore();

export function useUiStore<T>(selector: (state: UiState) => T): T {
  return useStore(uiStore, selector);
}
