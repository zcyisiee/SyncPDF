import { describe, expect, it } from 'vitest';

import {
  LAYOUT_SPECS,
  STORAGE_KEYS,
  createUiStore,
  readStoredScreen,
  widthFromDelta,
} from '../src/stores/ui';

describe('分隔条位移 → 新宽度（widthFromDelta：方向 + clamp）', () => {
  it('视图栏：指针右移变宽（无 invert），两端 clamp', () => {
    expect(widthFromDelta('viewrail', 220, 40)).toBe(260);
    expect(widthFromDelta('viewrail', 220, -40)).toBe(180);
    expect(widthFromDelta('viewrail', 300, 40)).toBe(320);
    expect(widthFromDelta('viewrail', 200, -80)).toBe(160);
  });

  it('右侧面板 / 时间线：轴正向位移变窄（invert），两端 clamp', () => {
    expect(widthFromDelta('inspector', 360, -40)).toBe(400);
    expect(widthFromDelta('inspector', 360, 40)).toBe(320);
    expect(widthFromDelta('inspector', 360, -400)).toBe(560);
    expect(widthFromDelta('inspector', 360, 400)).toBe(280);
    expect(widthFromDelta('timeline', 96, 20)).toBe(76);
    expect(widthFromDelta('timeline', 96, 100)).toBe(72);
    expect(widthFromDelta('timeline', 96, -100)).toBe(160);
  });
});

describe('ui store 三栏宽度（DESIGN.md §8.2）', () => {
  it('默认值与设计表一致', () => {
    const state = createUiStore().getState();
    expect(state.viewrailWidth).toBe(220);
    expect(state.inspectorWidth).toBe(360);
    expect(state.timelineHeight).toBe(96);
    expect(state.inspectorCollapsed).toBe(false);
  });

  it('spec 默认/范围与 §8.2 表逐项一致', () => {
    expect(LAYOUT_SPECS.viewrail).toMatchObject({ default: 220, min: 160, max: 320, step: 16 });
    expect(LAYOUT_SPECS.inspector).toMatchObject({ default: 360, min: 280, max: 560, step: 16 });
    expect(LAYOUT_SPECS.timeline).toMatchObject({ default: 96, min: 72, max: 160, step: 8 });
  });

  it('setLayoutWidth 按范围 clamp（超上限/超下限/小数）', () => {
    const store = createUiStore();
    store.getState().setLayoutWidth('viewrail', 999);
    expect(store.getState().viewrailWidth).toBe(320);
    store.getState().setLayoutWidth('viewrail', 10);
    expect(store.getState().viewrailWidth).toBe(160);
    store.getState().setLayoutWidth('inspector', 9999);
    expect(store.getState().inspectorWidth).toBe(560);
    store.getState().setLayoutWidth('inspector', 0);
    expect(store.getState().inspectorWidth).toBe(280);
    store.getState().setLayoutWidth('timeline', 1000);
    expect(store.getState().timelineHeight).toBe(160);
    store.getState().setLayoutWidth('timeline', 1);
    expect(store.getState().timelineHeight).toBe(72);
    store.getState().setLayoutWidth('timeline', 99.6);
    expect(store.getState().timelineHeight).toBe(100);
  });

  it('写入 localStorage 的键名按 §8.2（ieet.vrw / ieet.inspw / ieet.tlh）', () => {
    const store = createUiStore();
    store.getState().setLayoutWidth('viewrail', 300);
    store.getState().setLayoutWidth('inspector', 420);
    store.getState().setLayoutWidth('timeline', 120);
    expect(window.localStorage.getItem(STORAGE_KEYS.viewrail)).toBe('300');
    expect(window.localStorage.getItem(STORAGE_KEYS.inspector)).toBe('420');
    expect(window.localStorage.getItem(STORAGE_KEYS.timeline)).toBe('120');
  });

  it('冷启动读回持久化值，并把越界/坏值退回默认', () => {
    window.localStorage.setItem(STORAGE_KEYS.viewrail, '280');
    window.localStorage.setItem(STORAGE_KEYS.inspector, '9999');
    window.localStorage.setItem(STORAGE_KEYS.timeline, 'abc');
    const state = createUiStore().getState();
    expect(state.viewrailWidth).toBe(280);
    expect(state.inspectorWidth).toBe(560);
    expect(state.timelineHeight).toBe(96);
  });

  it('拖右侧分隔条只在宽度真变化时展开折叠的面板，并同步持久化', () => {
    const store = createUiStore();
    store.getState().setInspectorCollapsed(true);
    expect(store.getState().inspectorCollapsed).toBe(true);
    expect(window.localStorage.getItem(STORAGE_KEYS.inspectorCollapsed)).toBe('1');

    // 拖到与折叠前相同的宽度：状态不变，不意外展开
    store.getState().setLayoutWidth('inspector', 360);
    expect(store.getState().inspectorCollapsed).toBe(true);
    expect(window.localStorage.getItem(STORAGE_KEYS.inspectorCollapsed)).toBe('1');

    store.getState().setLayoutWidth('inspector', 420);
    expect(store.getState().inspectorWidth).toBe(420);
    expect(store.getState().inspectorCollapsed).toBe(false);
    expect(window.localStorage.getItem(STORAGE_KEYS.inspectorCollapsed)).toBe('0');
  });

  it('setInspectorCollapsed 持久化 ieet.inspCollapsed（折叠 = --inspw:0）', () => {
    const store = createUiStore();
    store.getState().setInspectorCollapsed(true);
    expect(store.getState().inspectorCollapsed).toBe(true);
    store.getState().setInspectorCollapsed(false);
    expect(store.getState().inspectorCollapsed).toBe(false);
    expect(window.localStorage.getItem(STORAGE_KEYS.inspectorCollapsed)).toBe('0');
  });
});

describe('ui store 屏 / 预览模式 / 拖拽态', () => {
  it('setScreen 持久化 ieet.screen，readStoredScreen 只认 4 个合法屏', () => {
    const store = createUiStore();
    store.getState().setScreen('workbench');
    expect(store.getState().screen).toBe('workbench');
    expect(window.localStorage.getItem(STORAGE_KEYS.screen)).toBe('workbench');
    expect(readStoredScreen()).toBe('workbench');
    window.localStorage.setItem(STORAGE_KEYS.screen, 'bogus');
    expect(readStoredScreen()).toBeNull();
  });

  it('previewMode 与 dragging', () => {
    const store = createUiStore();
    expect(store.getState().previewMode).toBe('target');
    store.getState().setPreviewMode('compare');
    expect(store.getState().previewMode).toBe('compare');
    store.getState().setDragging('timeline');
    expect(store.getState().dragging).toBe('timeline');
    store.getState().setDragging(null);
    expect(store.getState().dragging).toBeNull();
  });
});
