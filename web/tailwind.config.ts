import type { Config } from 'tailwindcss';

// 设计令牌映射（参见 src/app/globals.css）。
// 颜色走 CSS 变量（唯一拼写来源在 globals.css），Tailwind 只做别名映射。
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        parchment: 'var(--parchment)',
        ivory: 'var(--ivory)',
        canvas: 'var(--canvas)',
        paper: 'var(--paper)',
        sand: { DEFAULT: 'var(--sand)', 2: 'var(--sand-2)' },
        ink: {
          DEFAULT: 'var(--fg)',
          2: 'var(--fg-2)',
          3: 'var(--fg-3)',
          4: 'var(--fg-4)',
        },
        hair: { DEFAULT: 'var(--hair)', 2: 'var(--hair-2)' },
        accent: {
          DEFAULT: 'var(--accent)',
          strong: 'var(--accent-strong)',
          soft: 'var(--accent-soft)',
          on: 'var(--on-accent)',
          line: 'var(--accent-line)',
        },
        tint: { DEFAULT: 'var(--tint)', 2: 'var(--tint-2)' },
        run: {
          DEFAULT: 'var(--run)',
          soft: 'var(--run-soft)',
          ink: 'var(--run-ink)',
        },
        pass: {
          DEFAULT: 'var(--pass)',
          soft: 'var(--pass-soft)',
          ink: 'var(--pass-ink)',
        },
        err: {
          DEFAULT: 'var(--err)',
          soft: 'var(--err-soft)',
          ink: 'var(--err-ink)',
        },
        warn: {
          DEFAULT: 'var(--warn)',
          soft: 'var(--warn-soft)',
          ink: 'var(--warn-ink)',
        },
      },
      fontFamily: {
        serif: [
          "'Iowan Old Style'",
          'Charter',
          '"Source Han Serif SC"',
          '"Noto Serif SC"',
          'Georgia',
          '"Songti SC"',
          '"SimSun"',
          'serif',
        ],
        sans: [
          '-apple-system',
          'BlinkMacSystemFont',
          '"PingFang SC"',
          '"Hiragino Sans GB"',
          '"Segoe UI"',
          'system-ui',
          'sans-serif',
        ],
        mono: [
          '"SF Mono"',
          'ui-monospace',
          '"JetBrains Mono"',
          'Menlo',
          'Consolas',
          'monospace',
        ],
      },
      fontSize: {
        // §2.2 字号阶梯（桌面 1440×900，密度优先）
        micro: ['10px', { lineHeight: '1.4' }],
        tiny: ['11px', { lineHeight: '1.45' }],
        sm: ['12px', { lineHeight: '1.5' }],
        body: ['13px', { lineHeight: '1.55' }],
        md: ['14px', { lineHeight: '1.4' }],
        lg: ['16px', { lineHeight: '1.35' }],
        h2: ['17px', { lineHeight: '1.3' }],
        h1: ['28px', { lineHeight: '1.3' }],
      },
      borderRadius: { DEFAULT: '4px', card: '6px' },
      boxShadow: {
        ring: '0 0 0 1px var(--hair)',
        lift: '0 4px 24px rgba(0,0,0,.05)',
        modal: '0 4px 24px rgba(0,0,0,.14)',
      },
      spacing: {
        // §3 间距阶梯
        s1: '4px',
        s2: '6px',
        s3: '8px',
        s4: '12px',
        s5: '16px',
        s6: '20px',
        s7: '28px',
      },
    },
  },
  plugins: [],
} satisfies Config;
