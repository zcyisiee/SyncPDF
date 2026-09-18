import type { Config } from 'tailwindcss';

// 令牌唯一来源：docs/frontend/design-v2/DESIGN.md §6（Tailwind/shadcn 映射）+ §1/§2/§3。
// 颜色/字体/圆角/阴影/间距照抄设计稿；`hair` 与 `fontSize` 阶梯是 §1.1 派生线与 §2.2 表格的逐一映射。
export default {
  content: ['./index.html', './src/**/*.{ts,tsx}'],
  theme: {
    extend: {
      colors: {
        parchment: '#f5f4ed',
        ivory: '#faf9f5',
        sand: { DEFAULT: '#e8e6dc', 2: '#e5e3d8' },
        ink: { DEFAULT: '#141413', 2: '#3d3d3a', 3: '#504e49', 4: '#6b6a64' },
        // §1.1 派生发丝线（不新增灰阶）
        hair: {
          DEFAULT: 'color-mix(in oklch, #141413 11%, transparent)',
          2: 'color-mix(in oklch, #141413 22%, transparent)',
        },
        accent: { DEFAULT: '#1B365D', soft: '#E4ECF5', on: '#f7f6f0' },
        // §1.1 bbox 数据编码色（墨蓝 8% / 14%，来自 globals.css 的 --tint / --tint-2）
        tint: { DEFAULT: 'var(--tint)', 2: 'var(--tint-2)' },
        run: {
          DEFAULT: '#B7791F',
          soft: 'color-mix(in oklch, #B7791F 13%, #faf9f5)',
          ink: 'color-mix(in oklch, #B7791F 66%, #141413)',
        },
        pass: {
          DEFAULT: '#2F6B4F',
          soft: 'color-mix(in oklch, #2F6B4F 12%, #faf9f5)',
          ink: 'color-mix(in oklch, #2F6B4F 78%, #141413)',
        },
        err: {
          DEFAULT: '#A63D2F',
          soft: 'color-mix(in oklch, #A63D2F 11%, #faf9f5)',
          ink: 'color-mix(in oklch, #A63D2F 78%, #141413)',
        },
      },
      fontFamily: {
        serif: [
          'Charter',
          '"Source Han Serif SC"',
          '"Noto Serif SC"',
          'Georgia',
          '"Songti SC"',
          '"SimSun"',
          'serif',
        ],
        sans: [
          'Inter',
          'system-ui',
          '-apple-system',
          '"Segoe UI"',
          '"PingFang SC"',
          '"Microsoft YaHei"',
          'sans-serif',
        ],
        mono: [
          '"JetBrains Mono"',
          'ui-monospace',
          'SFMono-Regular',
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
        ring: '0 0 0 1px color-mix(in oklch, #141413 11%, transparent)',
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
