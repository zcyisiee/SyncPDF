/**
 * uiStore（M2-07）：布局 / 选中 / 面板。与 documentStore 分离（沿用现版双 store 划分）。
 * allotment 布局尺寸持久化到 localStorage（`syncpdf.layout`）。
 */
import { useStore } from 'zustand';
import { createStore, type StoreApi } from 'zustand/vanilla';

/** 活动栏视图 id（侧栏内容切换）。 */
export type ViewId = 'documents' | 'paragraphs' | 'terminology' | 'settings';

/** 面板 tab。 */
export type PanelTab = 'issues' | 'output';

/** 主题模式。 */
export type ThemeMode = 'auto' | 'dark' | 'light';

/** allotment 布局尺寸（px；-1 = 面板隐藏时使用占位）。 */
export interface LayoutSizes {
  /** 侧栏宽度。 */
  sidebarWidth: number;
  /** 编辑器三栏：源 PDF。 */
  editorSourceWidth: number;
  /** 编辑器三栏：译文 PDF。 */
  editorTargetWidth: number;
  /** 面板高度（底部）。 */
  panelHeight: number;
  /** 各栏可见性（false = allotment 布局折叠该部分）。 */
  sidebarVisible: boolean;
  panelVisible: boolean;
}

const LAYOUT_KEY = 'syncpdf.layout';

const DEFAULT_LAYOUT: LayoutSizes = {
  sidebarWidth: 300,
  editorSourceWidth: 50,
  editorTargetWidth: 50,
  panelHeight: 220,
  sidebarVisible: true,
  panelVisible: true,
};

function clamp(value: number, min: number, max: number): number {
  return Math.min(Math.max(value, min), max);
}

/** localStorage → LayoutSizes（缺字段 / 非法值 → 默认；测试环境无 localStorage 安全降级）。 */
export function readLayout(): LayoutSizes {
  try {
    const raw = window.localStorage.getItem(LAYOUT_KEY);
    if (raw === null) return { ...DEFAULT_LAYOUT };
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== 'object' || parsed === null) return { ...DEFAULT_LAYOUT };
    const record = parsed as Record<string, unknown>;
    const num = (key: keyof LayoutSizes): number | undefined =>
      typeof record[key] === 'number' && Number.isFinite(record[key] as number)
        ? (record[key] as number)
        : undefined;
    const bool = (key: keyof LayoutSizes): boolean | undefined =>
      typeof record[key] === 'boolean' ? (record[key] as boolean) : undefined;
    return {
      sidebarWidth: clamp(num('sidebarWidth') ?? DEFAULT_LAYOUT.sidebarWidth, 180, 600),
      editorSourceWidth: clamp(num('editorSourceWidth') ?? DEFAULT_LAYOUT.editorSourceWidth, 10, 90),
      editorTargetWidth: clamp(num('editorTargetWidth') ?? DEFAULT_LAYOUT.editorTargetWidth, 10, 90),
      panelHeight: clamp(num('panelHeight') ?? DEFAULT_LAYOUT.panelHeight, 120, 600),
      sidebarVisible: bool('sidebarVisible') ?? DEFAULT_LAYOUT.sidebarVisible,
      panelVisible: bool('panelVisible') ?? DEFAULT_LAYOUT.panelVisible,
    };
  } catch {
    return { ...DEFAULT_LAYOUT };
  }
}

function writeLayout(layout: LayoutSizes): void {
  try {
    window.localStorage.setItem(LAYOUT_KEY, JSON.stringify(layout));
  } catch {
    // 隐私模式 / 存储被禁：只影响本次会话
  }
}

export interface UiState {
  /** 活动栏当前视图。 */
  view: ViewId;
  /** 主题模式（auto 跟随 prefers-color-scheme）。 */
  theme: ThemeMode;
  layout: LayoutSizes;
  /** 面板激活的 tab。 */
  panelTab: PanelTab;
  /**
   * 当前选中段落（主选中；多选后续任务扩展）。
   * @deprecated M2-06 起选中的唯一来源是 documentStore.selectedParagraphId；
   * 本字段仅保留给尚未迁移的调用方，新代码不要用。
   */
  selectedParagraphId: string | null;
  /** 源栏 / 译文栏滚动同步开关。 */
  scrollSyncEnabled: boolean;
  /** 编辑器三栏当前激活的栏（段落编辑器滚动定位用）。 */
  activeEditor: 'source' | 'target' | 'paragraph';
  setView: (view: ViewId) => void;
  setTheme: (theme: ThemeMode) => void;
  setPanelTab: (tab: PanelTab) => void;
  setLayout: (patch: Partial<LayoutSizes>) => void;
  selectParagraph: (id: string | null) => void;
  setActiveEditor: (editor: UiState['activeEditor']) => void;
  setScrollSync: (enabled: boolean) => void;
}

export function createUiStore(): StoreApi<UiState> {
  return createStore<UiState>()((set, get) => ({
    view: 'documents',
    theme: 'auto',
    layout: readLayout(),
    panelTab: 'issues',
    selectedParagraphId: null,
    scrollSyncEnabled: true,
    activeEditor: 'source',
    setView: (view) => set({ view }),
    setTheme: (theme) => set({ theme }),
    setPanelTab: (panelTab) => set({ panelTab }),
    setLayout: (patch) => {
      const next = { ...get().layout, ...patch };
      writeLayout(next);
      set({ layout: next });
    },
    selectParagraph: (selectedParagraphId) => set({ selectedParagraphId }),
    setActiveEditor: (activeEditor) => set({ activeEditor }),
    setScrollSync: (scrollSyncEnabled) => set({ scrollSyncEnabled }),
  }));
}

export const uiStore = createUiStore();

export function useUiStore<T>(selector: (state: UiState) => T): T {
  return useStore(uiStore, selector);
}
