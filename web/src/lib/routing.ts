import { useEffect, useMemo, useState } from 'react';

import type { IconName } from '../components/icons';

/** 一级导航屏（顶栏不再承载屏切换，见 DESIGN.md §8.1）。 */
export type ScreenId = 'library' | 'glossary' | 'settings' | 'workbench';

/**
 * 工作台二级视图。hash 段用 `layout`（识别视图的产物是版面几何），标签用「识别」。
 * 本任务只有 `progress` 有内容，其余 4 个标签可点击但显示 W05–W12 占位。
 */
export const WORKBENCH_VIEWS = [
  { id: 'progress', label: '进度', icon: 'progress' },
  { id: 'layout', label: '识别', icon: 'recognize' },
  { id: 'translate', label: '翻译', icon: 'translate' },
  { id: 'check', label: '检查', icon: 'check' },
  { id: 'archive', label: '归档', icon: 'archive' },
] as const satisfies readonly { id: string; label: string; icon: IconName }[];

export type WorkbenchView = (typeof WORKBENCH_VIEWS)[number]['id'];

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
  const view = WORKBENCH_VIEWS.find((candidate) => candidate.id === segments[2]);
  if (!view) return { kind: 'unknown', hash };
  return { kind: 'workbench', did, view: view.id };
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
