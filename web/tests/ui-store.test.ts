import { describe, expect, it } from 'vitest';

import {
  LAYOUT_SPECS,
  STORAGE_KEYS,
  createUiStore,
  readStoredBboxMode,
  readStoredScreen,
  widthFromDelta,
} from '../src/stores/ui';

describe('分隔条位移 → 新宽度（widthFromDelta：方向 + clamp）', () => {
  it('右侧面板：轴正向位移变窄（invert），两端 clamp', () => {
    expect(widthFromDelta('inspector', 340, -40)).toBe(380);
    expect(widthFromDelta('inspector', 340, 40)).toBe(300);
    expect(widthFromDelta('inspector', 340, -400)).toBe(560);
    expect(widthFromDelta('inspector', 340, 400)).toBe(280);
  });

  it('左侧导航栏：轴正向位移变宽（左栏不反转），两端 clamp', () => {
    expect(widthFromDelta('nav', 280, 40)).toBe(320);
    expect(widthFromDelta('nav', 280, -40)).toBe(240);
    expect(widthFromDelta('nav', 280, 400)).toBe(420);
    expect(widthFromDelta('nav', 280, -400)).toBe(220);
  });
});

describe('ui store 三栏宽度', () => {
  it('默认值与设计稿一致（旧时间线高度/折叠态字段已删除）', () => {
    const state = createUiStore().getState();
    expect(state.navWidth).toBe(280);
    expect(state.inspectorWidth).toBe(340);
    expect('timelineHeight' in state).toBe(false);
    expect('timelineCollapsed' in state).toBe(false);
    expect('inspectorCollapsed' in state).toBe(false);
    expect('viewrailWidth' in state).toBe(false);
  });

  it('spec 默认/范围与设计稿一致', () => {
    expect(LAYOUT_SPECS.nav).toMatchObject({
      default: 280,
      min: 220,
      max: 420,
      step: 16,
      invert: false,
    });
    expect(LAYOUT_SPECS.inspector).toMatchObject({
      default: 340,
      min: 280,
      max: 560,
      step: 16,
      invert: true,
    });
  });

  it('setLayoutWidth 按范围 clamp（超上限/超下限/小数）', () => {
    const store = createUiStore();
    store.getState().setLayoutWidth('inspector', 9999);
    expect(store.getState().inspectorWidth).toBe(560);
    store.getState().setLayoutWidth('inspector', 0);
    expect(store.getState().inspectorWidth).toBe(280);
    store.getState().setLayoutWidth('nav', 1000);
    expect(store.getState().navWidth).toBe(420);
    store.getState().setLayoutWidth('nav', 1);
    expect(store.getState().navWidth).toBe(220);
    store.getState().setLayoutWidth('nav', 300.4);
    expect(store.getState().navWidth).toBe(300);
  });

  it('写入 localStorage 的键名（ieet.inspw / ieet.navw）', () => {
    const store = createUiStore();
    store.getState().setLayoutWidth('inspector', 420);
    store.getState().setLayoutWidth('nav', 320);
    expect(window.localStorage.getItem(STORAGE_KEYS.inspector)).toBe('420');
    expect(window.localStorage.getItem(STORAGE_KEYS.nav)).toBe('320');
  });

  it('冷启动读回持久化值，并把越界/坏值退回默认', () => {
    window.localStorage.setItem(STORAGE_KEYS.inspector, '9999');
    window.localStorage.setItem(STORAGE_KEYS.nav, 'abc');
    const state = createUiStore().getState();
    expect(state.inspectorWidth).toBe(560);
    expect(state.navWidth).toBe(280);
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
    store.getState().setDragging('nav');
    expect(store.getState().dragging).toBe('nav');
    store.getState().setDragging(null);
    expect(store.getState().dragging).toBeNull();
  });
});

describe('ui store 预览页 / bbox 图层 / 选中段落（W05）', () => {
  it('previewPage 默认 1、setPreviewPage clamp 到整数且不持久化', () => {
    const store = createUiStore();
    expect(store.getState().previewPage).toBe(1);
    store.getState().setPreviewPage(5);
    expect(store.getState().previewPage).toBe(5);
    store.getState().setPreviewPage(0);
    expect(store.getState().previewPage).toBe(1);
    store.getState().setPreviewPage(3.6);
    expect(store.getState().previewPage).toBe(4);
    store.getState().setPreviewPage(Number.NaN);
    expect(store.getState().previewPage).toBe(1);
    expect(window.localStorage.length).toBe(0);
  });

  it('resetPreviewForDocument：换文档回第 1 页并清空选中，同一 did 不重复重置', () => {
    const store = createUiStore();
    store.getState().setPreviewPage(7);
    store.getState().setSelectedParagraph('P07-002');
    store.getState().resetPreviewForDocument('doc-a');
    expect(store.getState().previewPage).toBe(1);
    expect(store.getState().selectedParagraphId).toBeNull();
    expect(store.getState().previewDid).toBe('doc-a');

    store.getState().setPreviewPage(4);
    store.getState().resetPreviewForDocument('doc-a');
    expect(store.getState().previewPage).toBe(4);
    store.getState().resetPreviewForDocument('doc-b');
    expect(store.getState().previewPage).toBe(1);
  });

  it('bboxMode 默认 parse；setBboxMode 持久化 ieet.bboxMode；坏值退回默认', () => {
    const store = createUiStore();
    expect(store.getState().bboxMode).toBe('parse');
    store.getState().setBboxMode('off');
    expect(store.getState().bboxMode).toBe('off');
    expect(window.localStorage.getItem(STORAGE_KEYS.bboxMode)).toBe('off');
    expect(readStoredBboxMode()).toBe('off');

    window.localStorage.setItem(STORAGE_KEYS.bboxMode, 'bogus');
    expect(readStoredBboxMode()).toBeNull();
  });

  it('bboxMode 四档都能持久化（`target` 不能刷新后被丢回默认）', () => {
    for (const mode of ['parse', 'target', 'layout', 'off'] as const) {
      window.localStorage.setItem(STORAGE_KEYS.bboxMode, mode);
      expect(readStoredBboxMode()).toBe(mode);
      const store = createUiStore();
      store.getState().setBboxMode(mode);
      expect(store.getState().bboxMode).toBe(mode);
    }
  });

  it('冷启动读回 ieet.bboxMode（用户显式选择跨会话保留）', () => {
    window.localStorage.setItem(STORAGE_KEYS.bboxMode, 'layout');
    expect(createUiStore().getState().bboxMode).toBe('layout');
  });

  it('selectedParagraphId 可设可清', () => {
    const store = createUiStore();
    expect(store.getState().selectedParagraphId).toBeNull();
    store.getState().setSelectedParagraph('P01-001');
    expect(store.getState().selectedParagraphId).toBe('P01-001');
    store.getState().setSelectedParagraph(null);
    expect(store.getState().selectedParagraphId).toBeNull();
  });
});

describe('ui store 段落多选（shift 语义）', () => {
  it('默认空集合，selectedParagraphId 与集合末元素同步为 null', () => {
    const store = createUiStore();
    expect(store.getState().selectedParagraphIds).toEqual([]);
    expect(store.getState().selectedParagraphId).toBeNull();
  });

  it('extend 追加：按点击顺序追加，主选中 = 最后一个', () => {
    const store = createUiStore();
    store.getState().selectParagraph('P01-001', { extend: true });
    store.getState().selectParagraph('P01-002', { extend: true });
    store.getState().selectParagraph('P01-003', { extend: true });
    expect(store.getState().selectedParagraphIds).toEqual(['P01-001', 'P01-002', 'P01-003']);
    expect(store.getState().selectedParagraphId).toBe('P01-003');
  });

  it('extend 移除已选：主选中回退到剩余最后一个；移除到空 → null', () => {
    const store = createUiStore();
    store.getState().selectParagraph('P01-001', { extend: true });
    store.getState().selectParagraph('P01-002', { extend: true });
    // 移除主选中（最后点击的那段）：集合剩前面的，主选中回退
    store.getState().selectParagraph('P01-002', { extend: true });
    expect(store.getState().selectedParagraphIds).toEqual(['P01-001']);
    expect(store.getState().selectedParagraphId).toBe('P01-001');
    store.getState().selectParagraph('P01-001', { extend: true });
    expect(store.getState().selectedParagraphIds).toEqual([]);
    expect(store.getState().selectedParagraphId).toBeNull();
  });

  it('非 extend 重置为单选 [id]（多选集合被丢弃）', () => {
    const store = createUiStore();
    store.getState().selectParagraph('P01-001', { extend: true });
    store.getState().selectParagraph('P01-002', { extend: true });
    store.getState().selectParagraph('P02-005');
    expect(store.getState().selectedParagraphIds).toEqual(['P02-005']);
    expect(store.getState().selectedParagraphId).toBe('P02-005');
    // 单选同一 id 不换集合引用（无语义变化）
    const before = store.getState().selectedParagraphIds;
    store.getState().selectParagraph('P02-005');
    expect(store.getState().selectedParagraphIds).toBe(before);
  });

  it('clearParagraphSelection 清空两个选中字段', () => {
    const store = createUiStore();
    store.getState().selectParagraph('P01-001', { extend: true });
    store.getState().selectParagraph('P01-002', { extend: true });
    store.getState().clearParagraphSelection();
    expect(store.getState().selectedParagraphIds).toEqual([]);
    expect(store.getState().selectedParagraphId).toBeNull();
  });

  it('resetPreviewForDocument 换文档清空多选，同一 did 不重复重置', () => {
    const store = createUiStore();
    store.getState().selectParagraph('P01-001', { extend: true });
    store.getState().selectParagraph('P01-002', { extend: true });
    store.getState().resetPreviewForDocument('doc-a');
    expect(store.getState().previewDid).toBe('doc-a');
    expect(store.getState().selectedParagraphIds).toEqual([]);
    expect(store.getState().selectedParagraphId).toBeNull();

    store.getState().selectParagraph('P02-001', { extend: true });
    store.getState().resetPreviewForDocument('doc-a'); // 同一 did：保留选中
    expect(store.getState().selectedParagraphIds).toEqual(['P02-001']);
    store.getState().resetPreviewForDocument('doc-b');
    expect(store.getState().selectedParagraphIds).toEqual([]);
  });

  it('setSelectedParagraph 兼容入口：非 null → [id]，null → 清空', () => {
    const store = createUiStore();
    store.getState().selectParagraph('P01-001', { extend: true });
    store.getState().selectParagraph('P01-002', { extend: true });
    store.getState().setSelectedParagraph('P03-001');
    expect(store.getState().selectedParagraphIds).toEqual(['P03-001']);
    expect(store.getState().selectedParagraphId).toBe('P03-001');
    store.getState().setSelectedParagraph(null);
    expect(store.getState().selectedParagraphIds).toEqual([]);
    expect(store.getState().selectedParagraphId).toBeNull();
  });
});

describe('ui store focusParagraph（多选块切换 chips）', () => {
  it('把集合里的某段挑到末尾（主选中段随之切换）', () => {
    const store = createUiStore();
    store.getState().selectParagraph('P01-001', { extend: true });
    store.getState().selectParagraph('P01-002', { extend: true });
    store.getState().selectParagraph('P01-003', { extend: true });
    store.getState().focusParagraph('P01-001');
    expect(store.getState().selectedParagraphIds).toEqual(['P01-002', 'P01-003', 'P01-001']);
    expect(store.getState().selectedParagraphId).toBe('P01-001');
  });

  it('不在集合中的 id 被忽略（不抛错、不改选中）', () => {
    const store = createUiStore();
    store.getState().selectParagraph('P01-001', { extend: true });
    store.getState().selectParagraph('P01-002', { extend: true });
    const before = store.getState().selectedParagraphIds;
    store.getState().focusParagraph('P09-009');
    expect(store.getState().selectedParagraphIds).toBe(before);
    expect(store.getState().selectedParagraphId).toBe('P01-002');
  });

  it('已在末尾（已是主选中段）：不 set（集合引用不变）', () => {
    const store = createUiStore();
    store.getState().selectParagraph('P01-001', { extend: true });
    store.getState().selectParagraph('P01-002', { extend: true });
    const before = store.getState().selectedParagraphIds;
    store.getState().focusParagraph('P01-002');
    expect(store.getState().selectedParagraphIds).toBe(before);
  });

  it('单选集合里 focus 自身也保持原集合引用（chips 只在多选时渲染，但语义不依赖它）', () => {
    const store = createUiStore();
    store.getState().selectParagraph('P01-001');
    const before = store.getState().selectedParagraphIds;
    store.getState().focusParagraph('P01-001');
    expect(store.getState().selectedParagraphIds).toBe(before);
    store.getState().focusParagraph('P02-002'); // 不在集合里 → 忽略
    expect(store.getState().selectedParagraphIds).toBe(before);
  });
});

describe('reader session choices', () => {
  it('zoom clamps invalid/extreme values, fit resets, compare links by default', () => {
    const store = createUiStore();
    expect(store.getState().compareLinked).toBe(true);
    store.getState().setPreviewZoom(100);
    expect(store.getState().previewZoom).toBe(4);
    store.getState().setPreviewZoom(-1);
    expect(store.getState().previewZoom).toBe(0.1);
    store.getState().setPreviewZoom(Number.NaN);
    expect(store.getState().previewZoom).toBe(0.1);
    store.getState().setPreviewZoom(null);
    expect(store.getState().previewZoom).toBeNull();
    store.getState().setCompareLinked(false);
    expect(store.getState().compareLinked).toBe(false);
  });
});

describe('事件流 → 预览定位桥（locateInPreview）', () => {
  it('nonce 自增（同页重复点击也触发）、page clamp 到 ≥1 的整数、paragraphId 缺了就 null', () => {
    const store = createUiStore();
    expect(store.getState().locate).toBeNull();

    store.getState().locateInPreview(5, 'P02-003');
    expect(store.getState().locate).toEqual({ nonce: 1, page: 5, paragraphId: 'P02-003' });

    // 同一页再点一次：值是新的，nonce 变了 → PreviewArea 的 effect 会再滚一次
    store.getState().locateInPreview(5, 'P02-003');
    expect(store.getState().locate).toEqual({ nonce: 2, page: 5, paragraphId: 'P02-003' });

    store.getState().locateInPreview(3.6);
    expect(store.getState().locate).toEqual({ nonce: 3, page: 4, paragraphId: null });
    store.getState().locateInPreview(0);
    expect(store.getState().locate).toEqual({ nonce: 4, page: 1, paragraphId: null });
    store.getState().locateInPreview(5, '');
    expect(store.getState().locate).toEqual({ nonce: 5, page: 5, paragraphId: null });
  });

  it('坏页数（NaN/Infinity）不写状态；locate 不持久化', () => {
    const store = createUiStore();
    store.getState().locateInPreview(Number.NaN);
    store.getState().locateInPreview(Number.POSITIVE_INFINITY);
    expect(store.getState().locate).toBeNull();

    store.getState().locateInPreview(7);
    expect(window.localStorage.length).toBe(0);
  });
});
