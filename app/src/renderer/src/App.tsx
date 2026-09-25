/**
 * 根组件：订阅主进程推送，挂载工作台。
 */
import { useEffect } from 'react';
import { useLibrary } from '@/store/library';
import { Workbench } from '@/layout/Workbench';

export function App(): JSX.Element {
  useEffect(() => useLibrary.getState().init(), []);
  return <Workbench />;
}
