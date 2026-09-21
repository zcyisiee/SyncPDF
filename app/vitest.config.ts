import { defineConfig } from 'vitest/config';
import react from '@vitejs/plugin-react';
import { resolve } from 'node:path';

/**
 * 测试配置：node 环境（sidecar / credentials / protocol 纯逻辑）+ jsdom 环境
 * （documentStore reducer / React 组件挂载）。测试文件内按需切环境。
 */
export default defineConfig({
  plugins: [react()],
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src/renderer/src'),
      '@shared': resolve(__dirname, 'src/shared'),
    },
  },
  test: {
    include: ['tests/**/*.test.{ts,tsx}'],
    environment: 'node',
    // jsdom 用 testEnvironmentMatch 配注切（见各测试文件 docblock）
    environmentMatchGlobs: [
      ['tests/renderer/**', 'jsdom'],
    ],
    testTimeout: 30_000,
    hookTimeout: 30_000,
  },
});
