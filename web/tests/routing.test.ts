import { act, renderHook, waitFor } from '@testing-library/react';
import { describe, expect, it } from 'vitest';

import {
  WORKBENCH_VIEWS,
  hashFor,
  hashForScreen,
  initialHash,
  parseHash,
  screenIdOf,
  useHashRoute,
} from '../src/lib/routing';

describe('parseHash', () => {
  it('空 hash / # 视为文件库', () => {
    expect(parseHash('')).toEqual({ kind: 'library' });
    expect(parseHash('#')).toEqual({ kind: 'library' });
    expect(parseHash('#/')).toEqual({ kind: 'library' });
    expect(parseHash('#/library')).toEqual({ kind: 'library' });
  });

  it('词表 / 设置占位屏', () => {
    expect(parseHash('#/glossary')).toEqual({ kind: 'glossary' });
    expect(parseHash('#/settings')).toEqual({ kind: 'settings' });
  });

  it('工作台：5 个视图 id 与「缺省视图 = 进度」', () => {
    expect(parseHash('#/d/ccs3764-dyn/progress')).toEqual({
      kind: 'workbench',
      did: 'ccs3764-dyn',
      view: 'progress',
    });
    for (const view of WORKBENCH_VIEWS) {
      expect(parseHash(`#/d/ccs3764-dyn/${view.id}`)).toEqual({
        kind: 'workbench',
        did: 'ccs3764-dyn',
        view: view.id,
      });
    }
    expect(parseHash('#/d/ccs3764-dyn')).toEqual({
      kind: 'workbench',
      did: 'ccs3764-dyn',
      view: 'progress',
    });
  });

  it('did 段做 URI 解码；非法百分号转义不崩（当作 did 交给后端 404）', () => {
    expect(parseHash('#/d/2024-620%20x/progress')).toEqual({
      kind: 'workbench',
      did: '2024-620 x',
      view: 'progress',
    });
    expect(parseHash('#/d/%/progress')).toEqual({
      kind: 'workbench',
      did: '%',
      view: 'progress',
    });
  });

  it('非法 did（空 / . / .. / 隐藏名 / 多段）与未知视图 → unknown', () => {
    expect(parseHash('#/d//progress')).toEqual({ kind: 'unknown', hash: '#/d//progress' });
    expect(parseHash('#/d/./progress')).toEqual({ kind: 'unknown', hash: '#/d/./progress' });
    expect(parseHash('#/d/../progress')).toEqual({ kind: 'unknown', hash: '#/d/../progress' });
    expect(parseHash('#/d/.hidden/progress')).toEqual({ kind: 'unknown', hash: '#/d/.hidden/progress' });
    expect(parseHash('#/d/ccs3764-dyn/recognize')).toEqual({
      kind: 'unknown',
      hash: '#/d/ccs3764-dyn/recognize',
    });
    expect(parseHash('#/d/ccs3764-dyn/progress/extra')).toEqual({
      kind: 'unknown',
      hash: '#/d/ccs3764-dyn/progress/extra',
    });
  });

  it('未知屏 → unknown', () => {
    expect(parseHash('#/nope')).toEqual({ kind: 'unknown', hash: '#/nope' });
  });
});

describe('hashFor / hashForScreen / screenIdOf', () => {
  it('hashFor 与 parseHash 互逆', () => {
    for (const hash of ['#/library', '#/glossary', '#/settings', '#/d/ccs3764-dyn/progress', '#/d/ccs3764-dyn/check']) {
      expect(hashFor(parseHash(hash))).toBe(hash);
    }
    expect(hashFor(parseHash('#/d/x'))).toBe('#/d/x/progress');
    expect(hashFor({ kind: 'unknown', hash: '' })).toBe('#/library');
  });

  it('图标栏分组链接与屏归属', () => {
    expect(hashForScreen('library')).toBe('#/library');
    expect(hashForScreen('workbench')).toBe('#/library');
    expect(hashForScreen('glossary')).toBe('#/glossary');
    expect(hashForScreen('settings')).toBe('#/settings');
    expect(screenIdOf(parseHash('#/d/x/progress'))).toBe('workbench');
    expect(screenIdOf(parseHash(''))).toBe('library');
    expect(screenIdOf(parseHash('#/nope'))).toBe('library');
  });
});

describe('initialHash', () => {
  it('地址栏有 hash 时原样使用', () => {
    expect(initialHash('#/d/x/progress', 'settings')).toBe('#/d/x/progress');
  });

  it('没 hash 时回落到 ieet.screen（workbench 不能脱离地址栏恢复）', () => {
    expect(initialHash('', 'glossary')).toBe('#/glossary');
    expect(initialHash('', 'settings')).toBe('#/settings');
    expect(initialHash('', 'workbench')).toBe('#/library');
    expect(initialHash('', null)).toBe('#/library');
    expect(initialHash('#', 'glossary')).toBe('#/glossary');
  });
});

describe('useHashRoute', () => {
  it('读取初始 hash 并跟随 hashchange', async () => {
    window.location.hash = '#/glossary';
    const { result } = renderHook(() => useHashRoute());
    expect(result.current).toEqual({ kind: 'glossary' });

    act(() => {
      window.location.hash = '#/d/ccs3764-dyn/layout';
    });
    await waitFor(() =>
      expect(result.current).toEqual({
        kind: 'workbench',
        did: 'ccs3764-dyn',
        view: 'layout',
      }),
    );
  });
});
