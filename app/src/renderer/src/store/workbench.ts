/**
 * 工作台 UI 状态（持久化到 localStorage）：三块可开关区域、分栏尺寸、视图模式、
 * 叠加框开关与图例勾选、右栏 / 底部面板当前 tab。
 */
import { create } from 'zustand';
import { persist } from 'zustand/middleware';
import type { RegionKind } from '@shared/protocol';

export type ViewMode = 'source' | 'target' | 'dual';
export type Zoom = number | 'fit-width';
export type InspectorTab = 'block' | 'info';
export type PanelTab = 'events' | 'issues' | 'logs';

export const ZOOM_STEPS = [0.5, 0.75, 1, 1.25, 1.5, 2, 3] as const;

export interface WorkbenchState {
  leftVisible: boolean;
  rightVisible: boolean;
  panelVisible: boolean;
  /** allotment 尺寸（px）：横向 [左, 中, 右]、纵向 [编辑区, 面板]。 */
  columnSizes: number[] | null;
  rowSizes: number[] | null;
  viewMode: ViewMode;
  /** 双栏同步滚动与缩放。 */
  sync: boolean;
  zoom: { source: Zoom; target: Zoom };
  showBoxes: boolean;
  hiddenKinds: RegionKind[];
  inspectorTab: InspectorTab;
  panelTab: PanelTab;

  toggleLeft: () => void;
  toggleRight: () => void;
  togglePanel: () => void;
  setColumnSizes: (sizes: number[]) => void;
  setRowSizes: (sizes: number[]) => void;
  setViewMode: (mode: ViewMode) => void;
  setSync: (sync: boolean) => void;
  setZoom: (pane: 'source' | 'target', zoom: Zoom) => void;
  setShowBoxes: (show: boolean) => void;
  toggleKind: (kind: RegionKind) => void;
  setInspectorTab: (tab: InspectorTab) => void;
  setPanelTab: (tab: PanelTab) => void;
}

export const useWorkbench = create<WorkbenchState>()(
  persist(
    (set) => ({
      leftVisible: true,
      rightVisible: true,
      panelVisible: false,
      columnSizes: null,
      rowSizes: null,
      viewMode: 'dual',
      sync: true,
      zoom: { source: 'fit-width', target: 'fit-width' },
      showBoxes: true,
      hiddenKinds: ['header', 'footer'],
      inspectorTab: 'block',
      panelTab: 'events',

      toggleLeft: () => set((s) => ({ leftVisible: !s.leftVisible })),
      toggleRight: () => set((s) => ({ rightVisible: !s.rightVisible })),
      togglePanel: () => set((s) => ({ panelVisible: !s.panelVisible })),
      setColumnSizes: (columnSizes) => set({ columnSizes }),
      setRowSizes: (rowSizes) => set({ rowSizes }),
      setViewMode: (viewMode) => set({ viewMode }),
      setSync: (sync) => set((s) => ({ sync, zoom: sync ? { ...s.zoom, target: s.zoom.source } : s.zoom })),
      // 同步开着时两栏缩放一起变
      setZoom: (pane, zoom) =>
        set((s) => ({ zoom: s.sync ? { source: zoom, target: zoom } : { ...s.zoom, [pane]: zoom } })),
      setShowBoxes: (showBoxes) => set({ showBoxes }),
      toggleKind: (kind) =>
        set((s) => ({
          hiddenKinds: s.hiddenKinds.includes(kind)
            ? s.hiddenKinds.filter((k) => k !== kind)
            : [...s.hiddenKinds, kind],
        })),
      setInspectorTab: (inspectorTab) => set({ inspectorTab }),
      setPanelTab: (panelTab) => set({ panelTab }),
    }),
    { name: 'syncpdf.workbench', version: 1 },
  ),
);

/** 缩放一档（'fit-width' 按 1 计）。 */
export function stepZoom(zoom: Zoom, direction: 1 | -1): number {
  const current = zoom === 'fit-width' ? 1 : zoom;
  if (direction > 0) return ZOOM_STEPS.find((z) => z > current + 1e-6) ?? ZOOM_STEPS.at(-1)!;
  return [...ZOOM_STEPS].reverse().find((z) => z < current - 1e-6) ?? ZOOM_STEPS[0];
}
