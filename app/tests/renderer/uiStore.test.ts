/**
 * uiStore 测试：布局持久化（localStorage）。
 * @vitest-environment jsdom
 */
import { beforeEach, describe, expect, it } from 'vitest';
import { createUiStore, readLayout, type LayoutSizes } from '../../src/renderer/src/store/uiStore';

function setStoredLayout(value: unknown): void {
  window.localStorage.setItem('syncpdf.layout', JSON.stringify(value));
}

describe('readLayout', () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it('无存储 → 默认布局', () => {
    expect(readLayout()).toEqual({
      sidebarWidth: 300,
      editorSourceWidth: 50,
      editorTargetWidth: 50,
      panelHeight: 220,
      sidebarVisible: true,
      panelVisible: true,
    } satisfies LayoutSizes);
  });

  it('存储损坏 / 非法值 → 默认布局（不抛错）', () => {
    window.localStorage.setItem('syncpdf.layout', 'not json');
    expect(readLayout().sidebarWidth).toBe(300);
    setStoredLayout('string');
    expect(readLayout().sidebarWidth).toBe(300);
    setStoredLayout(null);
    expect(readLayout().sidebarWidth).toBe(300);
    setStoredLayout({ sidebarWidth: 'wide' });
    expect(readLayout().sidebarWidth).toBe(300);
  });

  it('越界值 clamp 到范围', () => {
    setStoredLayout({ sidebarWidth: 50, panelHeight: 10000, editorSourceWidth: 500 });
    const layout = readLayout();
    expect(layout.sidebarWidth).toBe(180); // min
    expect(layout.panelHeight).toBe(600); // max
    expect(layout.editorSourceWidth).toBe(90); // max
  });
});

describe('createUiStore', () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it('setLayout 持久化并更新状态', () => {
    const store = createUiStore();
    store.getState().setLayout({ sidebarWidth: 420, panelVisible: false });
    expect(store.getState().layout.sidebarWidth).toBe(420);
    expect(store.getState().layout.panelVisible).toBe(false);
    // localStorage 已写入
    const persisted = JSON.parse(window.localStorage.getItem('syncpdf.layout') ?? '{}');
    expect(persisted.sidebarWidth).toBe(420);
    // 新 store 读取持久化值
    const second = createUiStore();
    expect(second.getState().layout.sidebarWidth).toBe(420);
    expect(second.getState().layout.panelVisible).toBe(false);
  });

  it('视图 / 面板 / 选中 / 编辑器激活栏切换', () => {
    const store = createUiStore();
    store.getState().setView('paragraphs');
    expect(store.getState().view).toBe('paragraphs');
    store.getState().setPanelTab('output');
    expect(store.getState().panelTab).toBe('output');
    store.getState().selectParagraph('P01-001');
    expect(store.getState().selectedParagraphId).toBe('P01-001');
    store.getState().selectParagraph(null);
    expect(store.getState().selectedParagraphId).toBeNull();
    store.getState().setActiveEditor('paragraph');
    expect(store.getState().activeEditor).toBe('paragraph');
    store.getState().setTheme('dark');
    expect(store.getState().theme).toBe('dark');
  });
});
