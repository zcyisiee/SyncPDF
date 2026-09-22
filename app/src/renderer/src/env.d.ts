/// <reference types="vite/client" />

/** CSS import 的模块声明（tokens.css 等）。 */
declare module '*.css';

/**
 * preload 白名单 API（src/preload/index.ts）的渲染进程视图。
 * 渲染进程只能通过它到达主进程；这里是唯一的类型来源。
 */
declare global {
  interface Window {
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
      /** 读白名单内文件的字节（M2-06；白名单外抛错）。 */
      readFileBytes: (path: string) => Promise<ArrayBuffer>;
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

export {};
