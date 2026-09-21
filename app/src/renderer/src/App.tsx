/**
 * 应用根：订阅引擎事件流 → documentStore；主题模式应用到 <html data-theme>；渲染工作台。
 */
import { useEffect } from 'react';
import type { EngineEvent } from '@shared/protocol';
import { Workbench } from './layout/Workbench';
import { documentStore } from './store/documentStore';
import { useUiStore } from './store/uiStore';

declare global {
  interface Window {
    /** preload 白名单 API（src/preload/index.ts）。 */
    syncpdf: {
      configure: (request: {
        provider: string;
        base_url: string;
        model: string;
        api_key: string;
        concurrency: number;
        cache_dir: string;
      }) => Promise<{ ok: boolean }>;
      startRun: (request: Record<string, unknown>) => Promise<{ ok: boolean }>;
      retranslate: (request: Record<string, unknown>) => Promise<{ ok: boolean }>;
      applyEdit: (request: Record<string, unknown>) => Promise<{ ok: boolean }>;
      exportDocument: (request: Record<string, unknown>) => Promise<{ ok: boolean }>;
      cancel: () => Promise<{ ok: boolean }>;
      onEvent: (listener: (event: unknown) => void) => () => void;
      openFile: () => Promise<string | null>;
      readCredentials: () => Promise<{
        provider: string;
        base_url: string;
        model: string;
        api_key: string;
      } | null>;
      writeCredentials: (credentials: {
        provider: string;
        base_url: string;
        model: string;
        api_key: string;
      }) => Promise<void>;
    };
  }
}

export function App(): JSX.Element {
  const theme = useUiStore((state) => state.theme);

  // 引擎事件流 → documentStore（seq 去重在 reducer 内）
  useEffect(() => {
    const unsubscribe = window.syncpdf.onEvent((event) => {
      documentStore.getState().applyEvent(event as EngineEvent);
    });
    return unsubscribe;
  }, []);

  // 主题模式：data-theme 覆盖 prefers-color-scheme（auto = 不设属性）
  useEffect(() => {
    if (theme === 'auto') {
      delete document.documentElement.dataset.theme;
    } else {
      document.documentElement.dataset.theme = theme;
    }
  }, [theme]);

  return <Workbench />;
}
