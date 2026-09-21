/**
 * 渲染进程入口：挂载 React 工作台 + 接通引擎事件流（window.syncpdf.onEvent → documentStore）。
 */
import React from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import './theme/index.css';

const container = document.getElementById('root');
if (container === null) {
  throw new Error('#root 不存在');
}

createRoot(container).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
