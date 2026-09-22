/**
 * 应用根：订阅引擎事件流 → documentStore；主题模式应用到 <html data-theme>；渲染工作台。
 * `window.syncpdf` 的类型在 env.d.ts。
 */
import { useEffect } from 'react';
import type { EngineEvent } from '@shared/protocol';
import { Workbench } from './layout/Workbench';
import { sharedPdfLoader } from './pdf/usePdfDocument';
import { documentStore } from './store/documentStore';
import { useUiStore } from './store/uiStore';

export function App(): JSX.Element {
  const theme = useUiStore((state) => state.theme);

  // 引擎事件流 → documentStore（seq 去重在 reducer 内）
  useEffect(() => {
    const unsubscribe = window.syncpdf.onEvent((event) => {
      documentStore.getState().applyEvent(event as EngineEvent);
    });
    return unsubscribe;
  }, []);

  // 换源文档时丢掉 pdf.js 文档缓存（译文栏的失效由 revision 负责）
  useEffect(() => {
    let previous = documentStore.getState().sourcePath;
    return documentStore.subscribe((state) => {
      if (state.sourcePath === previous) return;
      previous = state.sourcePath;
      sharedPdfLoader.clear();
    });
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
