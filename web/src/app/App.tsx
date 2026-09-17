import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { useEffect } from 'react';

import { initialHash, screenIdOf, useHashRoute } from '../lib/routing';
import { readStoredScreen, useUiStore } from '../stores/ui';
import { GlossaryScreen } from '../screens/GlossaryScreen';
import { LibraryScreen } from '../screens/LibraryScreen';
import { PlaceholderScreen, UnknownScreen } from '../screens/PlaceholderScreen';
import { WorkbenchScreen } from '../screens/WorkbenchScreen';

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

function AppRoutes() {
  const route = useHashRoute();
  const setScreen = useUiStore((state) => state.setScreen);

  // 首屏：地址栏没带 hash 时回落到 localStorage 记的屏（§8.1 的 `ieet.screen`）。
  useEffect(() => {
    const next = initialHash(window.location.hash, readStoredScreen());
    if (next !== window.location.hash) window.location.hash = next;
  }, []);

  // 屏状态是路由的镜像（为 `ieet.screen` 提供唯一写入点）。
  useEffect(() => {
    setScreen(screenIdOf(route));
  }, [route, setScreen]);

  switch (route.kind) {
    case 'library':
      return <LibraryScreen />;
    case 'glossary':
      return <GlossaryScreen />;
    case 'settings':
      return <PlaceholderScreen screen="settings" />;
    case 'workbench':
      return <WorkbenchScreen did={route.did} view={route.view} />;
    case 'unknown':
      return <UnknownScreen hash={route.hash} />;
  }
}
