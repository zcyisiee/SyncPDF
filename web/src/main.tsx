import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';

import { App } from './app/App';
import './app/globals.css';

const container = document.getElementById('root');
if (container === null) throw new Error('#root 不存在（index.html 被改动？）');

createRoot(container).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
