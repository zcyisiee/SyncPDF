import { useEffect, useMemo, useState } from 'react';

/** 一级导航屏（顶栏不再承载屏切换，见 DESIGN.md §8.1）。 */
export type ScreenId = 'library' | 'glossary' | 'settings' | 'workbench';

/**
 * 工作台视图。页面合并后只有一个常规工作台（`progress` 是历史名，hash 里保留它做默认）；
 * `archive` 只是把预览区换成版本列表的变体（其余内容完全相同）。
 */
export type WorkbenchView = 'progress' | 'archive';

/** 旧版二级视图 id（进度/识别/翻译/检查）：页面合并成同一个工作台后统一落到 `progress`。 */
const LEGACY_VIEW_IDS: readonly string[] = ['progress', 'layout', 'translate', 'check'];

export type Route =
  | { kind: 'library' }
  | { kind: 'glossary' }
  | { kind: 'settings' }
  | { kind: 'workbench'; did: string; view: WorkbenchView }
  | { kind: 'unknown'; hash: string };

/** `did` 规则与后端 resolver 一致（api.md §1.1）：单段目录名，拒绝空串 / `.` / `..` / 隐藏名。 */
function isValidDid(did: string): boolean {
  return did !== '' && did !== '.' && did !== '..' && !did.startsWith('.') && !did.includes('/');
}

function safeDecode(segment: string): string {
  try {
    return decodeURIComponent(segment);
  } catch {
    return segment;
  }
}

function parseWorkbench(segments: string[], hash: string): Route {
  const did = safeDecode(segments[1] ?? '');
  if (!isValidDid(did)) return { kind: 'unknown', hash };
  if (segments.length === 2) return { kind: 'workbench', did, view: 'progress' };
  // 旧链接不死链：识别/翻译/检查视图合并后仍解析，只是落在同一个工作台。
  const raw = segments[2];
  const view: WorkbenchView | null =
    raw === 'archive' ? 'archive' : LEGACY_VIEW_IDS.includes(raw) ? 'progress' : null;
  if (view === null) return { kind: 'unknown', hash };
  return { kind: 'workbench', did, view };
}

export function parseHash(hash: string): Route {
  const path = hash
    .replace(/^#/, '')
    .replace(/^\/+/, '')
    .replace(/\/+$/, '');
  if (path === '') return { kind: 'library' };
  const segments = path.split('/');
  if (segments.length === 1) {
    if (segments[0] === 'library') return { kind: 'library' };
    if (segments[0] === 'glossary') return { kind: 'glossary' };
    if (segments[0] === 'settings') return { kind: 'settings' };
    return { kind: 'unknown', hash };
  }
  if (segments[0] === 'd' && segments.length <= 3) return parseWorkbench(segments, hash);
  return { kind: 'unknown', hash };
}

export function hashFor(route: Route): string {
  switch (route.kind) {
    case 'library':
      return '#/library';
    case 'glossary':
      return '#/glossary';
    case 'settings':
      return '#/settings';
    case 'workbench':
      return `#/d/${encodeURIComponent(route.did)}/${route.view}`;
    case 'unknown':
      return route.hash === '' ? '#/library' : route.hash;
  }
}

/** 图标栏（一级导航）链接目标；workbench 归属「文件库」分组。 */
export function hashForScreen(screen: ScreenId): string {
  if (screen === 'glossary') return '#/glossary';
  if (screen === 'settings') return '#/settings';
  return '#/library';
}

export function screenIdOf(route: Route): ScreenId {
  switch (route.kind) {
    case 'workbench':
      return 'workbench';
    case 'unknown':
      return 'library';
    default:
      return route.kind;
  }
}

/**
 * 首屏 hash：地址栏有 hash 就用它；否则回落到 `localStorage.ieet.screen` 记的屏
 * （workbench 需要 did，不能脱离地址栏恢复，一律回落文件库）。
 */
export function initialHash(rawHash: string, storedScreen: ScreenId | null): string {
  if (rawHash !== '' && rawHash !== '#') return rawHash;
  if (storedScreen === 'glossary' || storedScreen === 'settings') return `#/${storedScreen}`;
  return '#/library';
}

/** hash 路由（不引 react-router）：`#/library` 默认，`#/d/:did/:view` 工作台。 */
export function useHashRoute(): Route {
  const [hash, setHash] = useState(() => window.location.hash);
  useEffect(() => {
    const sync = () => setHash(window.location.hash);
    window.addEventListener('hashchange', sync);
    sync();
    return () => window.removeEventListener('hashchange', sync);
  }, []);
  return useMemo(() => parseHash(hash), [hash]);
}
