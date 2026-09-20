import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useEffect } from 'react';
import type { CSSProperties } from 'react';

import { initialHash, screenIdOf, useHashRoute } from '../lib/routing';
import { readStoredScreen, useUiStore } from '../stores/ui';
import { GlossaryScreen } from '../screens/GlossaryScreen';
import { SettingsScreen } from '../screens/SettingsScreen';
import { UnknownScreen } from '../screens/PlaceholderScreen';
import { WorkbenchScreen } from '../screens/WorkbenchScreen';
import { Gutter } from '../components/shell/Gutter';
import { PaperNav } from '../components/shell/PaperNav';
import { Button } from '../components/ui/Button';
import { Icon } from '../components/icons';

/** 连接失败不自动重试：错误卡里有显式「重试」（DESIGN.md §4.9）。 */
export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: { retry: false, refetchOnWindowFocus: false },
    },
  });
}

const queryClient = createQueryClient();

export function App() {
  return (
    <QueryClientProvider client={queryClient}>
      <AppRoutes />
    </QueryClientProvider>
  );
}

/**
 * 三栏骨架（设计稿 §2）：左栏论文导航常驻所有路由，中栏按路由渲染，右栏（检查器）
 * 只属于工作台（由 `WorkbenchScreen` 的内层 `.wb-grid` 提供）。
 */
function AppRoutes() {
  const route = useHashRoute();
  const setScreen = useUiStore((state) => state.setScreen);
  const navWidth = useUiStore((state) => state.navWidth);

  // 首屏：地址栏没带 hash 时回落到 localStorage 记的屏（§8.1 的 `ieet.screen`）。
  useEffect(() => {
    const next = initialHash(window.location.hash, readStoredScreen());
    if (next !== window.location.hash) window.location.hash = next;
  }, []);

  // 屏状态是路由的镜像（为 `ieet.screen` 提供唯一写入点）。
  useEffect(() => {
    setScreen(screenIdOf(route));
  }, [route, setScreen]);

  const activeDid = route.kind === 'workbench' ? route.did : null;

  return (
    <div className="app-grid" style={{ '--navw': `${navWidth}px` } as CSSProperties}>
      <PaperNav activeDid={activeDid} route={route} />
      <Gutter id="nav" />
      <main data-od-id="app-main" className="flex min-h-0 min-w-0 flex-col">
        {route.kind === 'library' ? (
          <LibraryEmptyState />
        ) : route.kind === 'glossary' ? (
          <GlossaryScreen />
        ) : route.kind === 'settings' ? (
          <SettingsScreen />
        ) : route.kind === 'workbench' ? (
          <WorkbenchScreen did={route.did} view={route.view} />
        ) : (
          <UnknownScreen hash={route.hash} />
        )}
      </main>
    </div>
  );
}

/**
 * `#/library` 的中栏空态：文档列表在左栏常驻，中栏只引导去选一篇或上传。
 * 「上传 PDF」与左栏的上传按钮是**同一个**文件选择器（`requestUpload` 让 PaperNav 去点
 * 它长在左栏里的 input），不在这里再建一份队列状态。
 */
function LibraryEmptyState() {
  const requestUpload = useUiStore((state) => state.requestUpload);
  return (
    <div
      data-od-id="library-empty"
      className="flex min-h-0 flex-1 items-center justify-center p-s7"
    >
      <div className="max-w-[430px] rounded border border-hair-2 bg-ivory px-s7 py-s7 text-center">
        <span className="mx-auto mb-s4 grid h-10 w-10 place-items-center rounded-[3px] border border-hair-2 bg-sand text-ink-3">
          <Icon name="library" className="h-5 w-5" />
        </span>
        <h1 className="font-serif text-h2 font-medium text-ink">选择或上传一篇论文</h1>
        <p className="mt-s2 text-body text-ink-3">
          左栏是全部文档：点一张卡片进去翻译。还没有文档就用下面的按钮选一个 PDF，
          或直接把文件拖到左栏任意位置。
        </p>
        <div className="mt-s5 flex items-center justify-center">
          <Button variant="primary" data-od-id="cta-upload" onClick={requestUpload}>
            <Icon name="upload" className="h-[13px] w-[13px]" />
            上传 PDF
          </Button>
        </div>
      </div>
    </div>
  );
}
