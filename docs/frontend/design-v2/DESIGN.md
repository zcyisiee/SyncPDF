# ieeTranslater · Design System

> 面向 React + Tailwind + shadcn/ui 实现的设计令牌与组件规范。
> 视觉方向：**kami 纸感 × 工程监控** —— 纸是内容的载体（预览页、文档卡、词表），
> 监控是过程的载体（事件流、时间线、状态徽标、bbox）。前者用衬线 + 羊皮纸底色，
> 后者用无衬线 + 等宽 + 高密度。

---

## 0. 一句话系统

**一套暖纸底色（parchment / ivory / sand）+ 唯一墨蓝强调色，正文交给衬线，遥测交给等宽。**
没有任何渐变、毛玻璃、纯白、纯黑。

---

## 1. 颜色令牌

### 1.1 基础层（Neutrals，占屏 85–90%）

| Token | Hex | OKLch | 用途 |
|---|---|---|---|
| `--parchment` | `#f5f4ed` | `oklch(96.60% 0.0093 99.98)` | 页面底色。**永不用纯白** |
| `--ivory` | `#faf9f5` | `oklch(98.18% 0.0054 95.10)` | 卡片 / 纸页 / 输入框 / 弹层 |
| `--sand` | `#e8e6dc` | `oklch(92.37% 0.0135 97.45)` | 次级面：图标栏、分段控件轨道、chip 底 |
| `--sand-2` | `#e5e3d8` | `oklch(91.44% 0.0149 98.30)` | 深一档的次级面（hover、嵌套面） |
| `--fg` | `#141413` | `oklch(19.08% 0.0020 106.59)` | 主文字。**永不用纯黑** |
| `--fg-2` | `#3d3d3a` | `oklch(35.90% 0.0051 106.65)` | 正文、控件文字 |
| `--fg-3` | `#504e49` | `oklch(42.42% 0.0085 88.71)` | 次级正文、表格单元 |
| `--fg-4` | `#6b6a64` | `oklch(52.33% 0.0093 99.01)` | 标签、时间戳、说明。**文字下限** |

派生发丝线（不要新增灰阶）：

```css
--hair:   color-mix(in oklch, var(--fg) 11%, transparent);  /* 1px 分隔线、卡片描边 */
--hair-2: color-mix(in oklch, var(--fg) 22%, transparent);  /* 输入框描边、虚线框 */
```

### 1.2 强调色（唯一，覆盖 ≤5% 像素）

| Token | Hex | OKLch | 用途 |
|---|---|---|---|
| `--accent` | `#1B365D` | `oklch(33.32% 0.0767 257.71)` | 墨蓝。主 CTA 填充、选中描边、预览角标、链接角色 |
| `--accent-soft` | `#E4ECF5` | `oklch(93.97% 0.0149 251.16)` | tag 底、focus ring 外圈 |
| `--on-accent` | `#f7f6f0` | — | 墨蓝上的文字（暖白，不用 `#fff`） |
| `--tint` | `color-mix(in oklch, var(--accent) 8%, transparent)` | — | **已译 bbox 填充** |
| `--tint-2` | `color-mix(in oklch, var(--accent) 14%, transparent)` | — | bbox hover / 选中填充 |

**配额（硬约束）**：每屏 `--accent` 可见使用 ≤ 2 处（典型组合 = 一个 chip/角标 + 一个主 CTA）。
bbox 填充属于**数据编码**，不计入配额，但必须保持 8–14% 的极低不透明度。

### 1.3 状态色

| Token | Hex | OKLch | 语义 |
|---|---|---|---|
| `--run` | `#B7791F` | `oklch(62.64% 0.1248 70.45)` | running / 进行中 / 警告（琥珀） |
| `--pass` | `#2F6B4F` | `oklch(47.94% 0.0780 161.08)` | pass / 已完成（墨绿） |
| `--err` | `#A63D2F` | `oklch(50.46% 0.1415 30.30)` | error / blocker（朱红） |

软底：`color-mix(in oklch, <state> 11–13%, var(--ivory))`。

> ⚠️ **状态文字必须用 ink 变体**：`--run` 在 ivory 上只有 3.46:1，小字号不达标。
> ```css
> --run-ink:  color-mix(in oklch, var(--run) 66%, var(--fg));   /* 软底 5.34:1 */
> --pass-ink: color-mix(in oklch, var(--pass) 78%, var(--fg));  /* 软底 6.5:1 */
> --err-ink:  color-mix(in oklch, var(--err) 78%, var(--fg));   /* 软底 7.0:1 */
> ```
> 状态**填充块 / 进度条 / 圆点**仍用饱和原色（图形对象只需 3:1）。

### 1.4 对比度闸门（已实测）

| 配对 | 比值 | 判定 |
|---|---|---|
| `--fg` on parchment | 16.7 : 1 | ✅ |
| `--fg-2` on ivory | 10.3 : 1 | ✅ |
| `--fg-3` on ivory | 7.9 : 1 | ✅ |
| `--fg-4` on ivory | 5.2 : 1 | ✅（也是最小可用灰） |
| `--accent` on ivory | 11.5 : 1 | ✅ |
| `--on-accent` on `--accent` | 10.2 : 1 | ✅ |
| `--run-ink` on `--run-soft` | 5.3 : 1 | ✅ |
| `--pass-ink` on `--pass-soft` | 6.5 : 1 | ✅ |
| `--err-ink` on `--err-soft` | 7.0 : 1 | ✅ |
| 纯 `--run` on ivory | 3.5 : 1 | ❌ 仅可用于图形，不可用于文字 |

---

## 2. 字体

### 2.1 三族分工（不可互换）

```css
--serif: Charter, "Source Han Serif SC", "Noto Serif SC", Georgia, "Songti SC", "SimSun", serif;
--sans:  Inter, system-ui, -apple-system, "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
--mono:  "JetBrains Mono", ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
```

| 用途 | 字族 | 权重 |
|---|---|---|
| 文档名、视图栏文档标题、段落原文/译文、卡片标题、弹层标题 | **serif** | 500 |
| UI 文案、按钮、标签、说明、表格 | **sans** | 400 / 500 |
| 事件流、时间线、状态徽标、段落 id、页码、JSON、数值、表格数字列 | **mono** + `font-variant-numeric: tabular-nums` | 400 |

**事件流、时间线、JSON 一律不用衬线** —— 这是本系统最重要的分工边界。

### 2.2 字号阶梯（桌面 1440×900，密度优先）

| Token | 值 | 行高 | 用途 |
|---|---|---|---|
| `--fs-micro` | 10px | 1.4 | 徽标、时间戳、JSON、表格表头 |
| `--fs-tiny` | 11px | 1.45 | 次级标签、说明、按钮（小） |
| `--fs-sm` | 12px | 1.5 | 控件文字、事件流正文 |
| `--fs-body` | 13px | 1.55 | 默认 UI 正文 |
| `--fs-md` | 14px | 1.4 | 卡片标题、弹层正文 |
| `--fs-lg` | 16px | 1.35 | 弹层标题、verdict 值 |
| `--fs-h2` | 17px | 1.3 | 视图栏文档名、分组标题 |
| `--fs-h1` | 21–28px | 1.3 | 页面标题（文件库） |

纸页正文独立于 UI 阶梯，由预览容器注入：`--pfs`（单页 11.5px / 对照 7.6px / × 缩放系数）。

### 2.3 字距与行高（强制）

| 场景 | letter-spacing | line-height |
|---|---|---|
| 正文 13–14px | `0` | 1.5–1.55 |
| 小字 10–12px | `0.02em` | 1.4–1.5 |
| ALL CAPS / 表格表头 | **`0.06em`** | 1.4 |
| 拉丁标题 ≥17px | `-0.01em` | 1.3 |
| **中文衬线正文（段落译文）** | `0`（中文不加负字距） | **1.78** |
| **中文衬线标题** | `0` | **1.35–1.4** |

> 中文标题撑满 em 框，行高不得低于 1.3；本系统所有中文标题行高 ≥ 1.35。
> 等宽数字必须 `tabular-nums`，否则时间线 / 页码会跳动。

---

## 3. 间距、圆角、阴影

```css
--s1:4px  --s2:6px  --s3:8px  --s4:12px  --s5:16px  --s6:20px  --s7:28px
--r: 4px     /* 按钮、chip、输入框、折叠面板 */
--r2: 6px    /* 卡片、弹层、分段控件 */
```

- **阴影只有一种**：`0 1px 0 var(--hair), 0 4px 24px rgba(0,0,0,.05)`。
  卡片默认无阴影，hover 时才加 `0 4px 24px rgba(0,0,0,.05)`。
- **1px ring 优先于 shadow**：`box-shadow: 0 0 0 1px var(--hair)` 用于选中 / 抬升。
- 纸页（`.paper`）圆角 **2px**，模拟纸张，不用 6px。
- 圆角上限 6px。**没有任何渐变、毛玻璃、彩色光晕。**

### 布局栅格（工作台）

```
┌─ topbar 40px ──────────────────────────────────────────────┐
│ 图标栏 56 │ 视图栏 220 │ 预览区 1fr │ 右侧面板 360         │  ← 1fr 行
├────────────────────────────────────────────────────────────┤
│ 时间线 88px（跨 4 列）                                     │
└────────────────────────────────────────────────────────────┘
预览区内部：工具条 44 + 上下文条 34 + 纸页画布（滚动）
```

---

## 4. 组件规范

### 4.1 Button

| 变体 | 默认 | Hover | Active | Disabled |
|---|---|---|---|---|
| `primary` | bg `--accent` / fg `--on-accent` / border `--accent` | bg `color-mix(accent 88%, fg)`，fg 不变 | `translateY(1px)` | — |
| `default` | bg `--ivory` / border `--hair` / fg `--fg-2` | bg `--sand`，border `--hair-2`，fg `--fg` | `translateY(1px)` | opacity .45，bg ivory，fg `--fg-4` |
| `danger` | fg `--err` / border `color-mix(err 34%)` | bg `--err-soft`，border `--err` | 同上 | — |
| `ghost` / `icon` | 透明 / 28×28 | bg `--sand` | — | — |

尺寸：默认高 28px（padding 0 10px），`sm` 高 24px。触控目标 ≥ 44px 时外包一层透明的 `::before` 命中区。
**hover 永不降低对比度**：只动背景亮度（±0.06–0.12 L）或边框，不动前景色。

**动作经济**：同一视口内同一动作只允许一个 primary。文件库 = `上传 PDF`；弹层内 = `开始翻译`；
工作台不做 primary，改用 `danger` 的 `取消任务` 与描边按钮。

### 4.2 Segmented control（原文 / 译文 / 对照）

轨道 `--sand` + padding 2px + 圆角 6px；激活项 `--ivory` + 1px ring + fg `--fg`；
未激活 fg `--fg-4`，hover 时 fg `--fg-2` + bg `color-mix(fg 6%)`。
**不使用 accent 表示选中**（配额留给 CTA 与角标）。`role="group"` + `aria-pressed`。

### 4.3 Chip / Badge / 状态徽标

| 组件 | 尺寸 | 字体 | 底色 |
|---|---|---|---|
| `.chip`（阶段、类型、id） | h 19px / r 3px / padding 0 6px | mono 10px `0.03em` | `--sand` + fg `--fg-3` |
| `.chip--acc`（预览、已编辑） | 同上 | mono 10px | `--accent-soft` + `--accent` |
| `.chip--acc-line`（墨蓝虚线角标「预览」） | 同上 | mono 10px | 透明 + `1px dashed --accent` |
| `.st`（running / pass / error，带 6px 圆点） | h 20px / r 3px | sans 11px | `*-soft` + `*-ink` |
| `.sevr`（P0 / P1 / P2） | h 18px / min-w 24px | mono 10px | P0=`err-soft`，P1=`run-soft`，P2=`sand` |

running 圆点 `animation: pulse 1.6s ease-in-out infinite`（opacity 1→.35，scale 1→.82）。
**这是全站唯一的动效**，也只出现在真正运行中的元素上。

### 4.4 Card

`bg --ivory` + `1px solid --hair` + `r 6px`，无阴影。
hover：`border-color: --hair-2` + `0 4px 24px rgba(0,0,0,.05)`。
**禁止「左侧彩色竖条 + 圆角卡片」**。唯一例外是错误卡（见 4.9），
它沿用朱红左边线但**圆角为 0**，以此避开该反模式。

### 4.5 纸页与 bbox（核心组件）

```
.paper            width: var(--pw); min-height: calc(var(--pw) * 1.294) /* Letter */
                  bg --ivory, border 1px --hair, radius 2px, padding 3.1em 3.4em
                  font-family: --serif, font-size: var(--pfs)
.paper__flow      flex column, gap .72em（自然流，绝不绝对定位 → 永不重叠）
```

| bbox 状态 | 视觉 |
|---|---|
| 已译 | `background: var(--tint)`（墨蓝 8%） |
| 已译 hover | `background: var(--tint-2)`（14%） |
| 待译 | `1px dashed var(--hair-2)`，文字 `--fg-3` |
| 预览态（已编辑未编译） | `outline: 1px dashed var(--accent); outline-offset: 2px` |
| 选中 | `outline: 2px solid var(--accent)` + **8 个 6×6 拖拽手柄**（ivory 底 + 1.5px 墨蓝描边） |
| 检查问题 | P0 `0 0 0 1px --err` + 7% 朱红底；P1/P2 `0 0 0 1px --run` |
| 图表未嵌入 | `1px dashed --hair-2` 盒 + `--parchment` 底 + mono 说明文字（**有标签的占位，不是灰框**） |

角标：右上角 10px mono；进度态 = `128/206`（`--run-ink` + hair 描边）；预览态 = 墨蓝虚线 `预览`。
对照模式两页并排（gap 16，单页宽 300，字号 7.6px），同一段落 id **两侧同时高亮**。

层开关（段落框 / 版面块 / 公式）只切换 bbox 描边与区块元信息，不改变文字排版。

### 4.6 Event row（事件流）

```
[52px 时间戳 mono 10px][阶段 chip] [级别圆点 5px]
                        人话叙述 12px / 1.5
                        （展开）原始 JSON
```

- 时间戳 `--fg-4`，叙述 `--fg-2`，JSON：`--parchment` 底 + 1px hair + 10px mono + `overflow-x:auto`。
- 点击整行展开/收起 JSON；`aria-expanded` 同步。
- 级别圆点：info = `--hair-2`，warn = `--run`，err = `--err`，run = `--run` + pulse。
- 过滤条：阶段（全部 + 7）× 级别（信息/警告/错误/全级别）；激活 chip = `--accent` 填充。
- 排序：**最新在上**；头部标注「实时」脉冲点或错误态。

### 4.7 Timeline（时间线）

- 7 段：`parse / translate / apply / build / check / review / report`。
- 宽度 = `flex-grow: <该段耗时秒数>`（按耗时比例）。
- 状态：完成 `--pass-soft` + `--pass-ink`；进行中 `--run-soft` + `--run-ink` + 底部 2px 进度；未开始 `--parchment` + hair 虚线感；失败 `--err-soft` + `--err-ink`。
- 进行中段内显示批内秒表（mono，如 `02:41`）；主行显示 `已译 128/206 段`。
- 失败段右上 6px 朱红圆点。点选任一段 = 事件流按该阶段过滤，被选项加 `0 0 0 2px --accent`。
- 两端：左「当前段 02:41」，右「总用时 07:12 · 预计 ~11:00（`--fg-4`）」。

### 4.8 输入与滑块

- `input` / `textarea` / `select`：`--ivory` 底，`1px --hair-2`，r 4px，h 28–30px。
- focus：`border-color: --accent` + `box-shadow: 0 0 0 3px var(--accent-soft)`（替代 outline）。
- 滑块：轨道 2px `--hair-2`，滑钮 11px 圆 + 1.5px 墨蓝描边，hover 填充 `--accent-soft`。
- 排版参数行：`grid-template-columns: 96px 1fr 44px`（mono 名称 / 滑块 / mono 数值）。
- 开关 `.sw`：30×17 轨道，17px 高时缩略 12px 球；开启 = `--accent` 填充（开关属于控件，不计入 accent 文字配额）。

### 4.9 错误卡

```
border: 1px solid --hair; border-left: 3px solid --err; border-radius: 0;  /* 圆角为 0 */
bg: --err-soft; 标题 --err-ink（serif 500 14px）; 详情 mono 12px
按钮组：从翻译重试（描边）· 换模型重试（描边）· 查看日志（描边）
```

### 4.10 弹层与 Toast

- 遮罩 `color-mix(in oklch, var(--fg) 34%, transparent)`（暖色遮罩，不用纯黑）。
- 弹层 440px，`--ivory`，r 6px，`0 4px 24px rgba(0,0,0,.14)`；三区 = 头 / 体 / 脚。
- Toast：底部居中，`--fg` 底 + `--parchment` 字，r 6px，1.9s 后淡出。

### 4.11 表格（词表）

- 表头 sticky，10px mono `0.06em` + `--fg-4`，`border-bottom: 1px --hair`。
- 单元行内编辑：默认透明，hover `--sand` + 1px 虚线外框；focus `2px solid --accent`（inset）。
- 术语列 mono 11px，译法列 serif 13.5px（术语是文本，不是遥测数据）。
- 数值列右对齐 + `tabular-nums`。

---

## 5. 交互状态矩阵（实现时必须逐项对齐）

| 元素 | hover | focus-visible | active / selected | disabled |
|---|---|---|---|---|
| 主按钮 | bg 暗 12% | `0 0 0 3px --accent-soft` | 下移 1px | — |
| 次按钮 | bg `--sand` | 同上 | 下移 1px | opacity .45 |
| 导航项（图标栏 / 视图栏 / 词表） | bg `--sand`，fg `--fg` | ring | bg `--ivory` + 1px ring，图标 `--accent` | — |
| 纸页 bbox | 填充 8%→14% | 2px 墨蓝 outline | 2px 墨蓝 outline + 8 手柄 | — |
| 事件行 | bg `--sand` | ring | 展开露出 JSON | — |
| 时间线段 | `filter: brightness(.97)` | ring | `0 0 0 2px --accent` | — |

所有可聚焦元素必须 `:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px }`。
禁用态是全站**唯一**允许降低对比度的状态。

---

## 6. Tailwind / shadcn 映射

```js
// tailwind.config.js
export default {
  theme: {
    extend: {
      colors: {
        parchment: '#f5f4ed', ivory: '#faf9f5', sand: { DEFAULT: '#e8e6dc', 2: '#e5e3d8' },
        ink: { DEFAULT: '#141413', 2: '#3d3d3a', 3: '#504e49', 4: '#6b6a64' },
        accent: { DEFAULT: '#1B365D', soft: '#E4ECF5', on: '#f7f6f0' },
        run:  { DEFAULT: '#B7791F', soft: 'color-mix(in oklch, #B7791F 13%, #faf9f5)', ink: 'color-mix(in oklch,#B7791F 66%,#141413)' },
        pass: { DEFAULT: '#2F6B4F', soft: 'color-mix(in oklch, #2F6B4F 12%, #faf9f5)', ink: 'color-mix(in oklch,#2F6B4F 78%,#141413)' },
        err:  { DEFAULT: '#A63D2F', soft: 'color-mix(in oklch, #A63D2F 11%, #faf9f5)', ink: 'color-mix(in oklch,#A63D2F 78%,#141413)' },
      },
      fontFamily: {
        display: ['Charter', '"Source Han Serif SC"', '"Noto Serif SC"', 'Georgia', '"Songti SC"', 'serif'],
        sans: ['Inter', 'system-ui', '"PingFang SC"', '"Microsoft YaHei"', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'ui-monospace', 'SFMono-Regular', 'Menlo', 'monospace'],
      },
      borderRadius: { DEFAULT: '4px', card: '6px' },
      boxShadow: { ring: '0 0 0 1px color-mix(in oklch,#141413 11%,transparent)',
                   lift: '0 4px 24px rgba(0,0,0,.05)', modal: '0 4px 24px rgba(0,0,0,.14)' },
      spacing: { s1:'4px', s2:'6px', s3:'8px', s4:'12px', s5:'16px', s6:'20px', s7:'28px' },
    },
  },
};
```

shadcn/ui 主题变量（`app/globals.css`）：

```css
:root{
  --background: 60 15% 95%;      /* parchment */
  --foreground: 60 5% 8%;        /* fg */
  --card: 45 33% 97%;            /* ivory */
  --card-foreground: 60 5% 8%;
  --muted: 50 20% 89%;           /* sand */
  --muted-foreground: 55 4% 41%; /* fg-3 */
  --border: 52 16% 88%;
  --input: 52 16% 88%;
  --primary: 214 55% 24%;        /* accent 墨蓝 */
  --primary-foreground: 50 26% 95%;
  --destructive: 8 52% 42%;      /* err */
  --warning: 38 71% 42%;         /* run */
  --success: 156 39% 30%;        /* pass */
  --radius: 4px;
}
```

`Badge` → `.chip`；`Tabs` → 视图栏 / 面板 tab；`Dialog` → 配置弹层；
`Table` → 词表（cell 用 `contentEditable` 或 `react-hook-form` + 行级 PATCH）；
`Slider` → 排版参数；`Tooltip` → 禁用态「任务完成后可编辑」；
`ScrollArea` → 事件流 / 纸页画布 / 词表主体。

---

## 7. 无障碍与实现约束

1. 正常文字 ≥ 4.5:1，大字与图形 ≥ 3:1（见 §1.4 实测表）。
2. 焦点环全站可见，颜色 `--accent`，2px + 2px offset。
3. 状态**永不只靠颜色**：徽标带圆点 + 文案；bbox 问题块同时有描边色与 `title`/`aria-label`。
4. 触控目标 ≥ 44px（图标栏按钮 36px 视觉 + 8px 命中扩展）。
5. 中文文本容器禁用负字距；中文标题行高 ≥ 1.35；正文行高 1.78。
6. 容器不得出现孤字行（末行 1–2 字符）：优先调容器宽度与换行，其次才动字号。
7. 动效只有一处：running 脉冲。尊重 `prefers-reduced-motion`。
8. 纸页文字采用**自然文档流**（flex column），不用绝对定位 —— 这是本系统「永不重叠」的结构保证。
9. 所有区块带 `data-od-id`（kebab-case）：`screen-library`、`preview-toolbar`、`timeline`、
   `error-card`、`bbox-P05-002`、`check-verdict`、`doc-attention` …… 供标注模式定位。

---

## 变更记录 v2

原型 v1 → v2 的落地规范。本节的数值即实现值，新增组件一律复用 §1 令牌，未引入新灰阶或新强调色。

### 8.1 顶栏与两级导航

- 顶栏只保留品牌（`iee Translater · 学术 PDF 翻译工作台`）与右侧「当前文档名 + 任务状态徽标」
  （如 `● tr-2418 翻译中`），**不再承载屏幕切换**。高度 44px。
- 一级 = 图标栏（文件库 / 词表 / 设置，56px）；二级 = 视图栏（进度 / 识别 / 翻译 / 检查 / 归档，
  默认 220px）。一级高亮表示所在分组，二级高亮表示当前视图；hash 路由 `#/<screen>` 与
  `localStorage.ieet.screen` 保持不变。
- 开发用屏幕切换器（不属于产品 UI）：右下角浮动 mono 10px 链接组，默认 `opacity .5`，
  hover / focus-within 展开，`bottom: calc(var(--tlh) + 10px)`。

### 8.2 栏宽与拖拽分隔条

| 分隔条 | 位置 | 默认 | 范围 | 轴 | localStorage |
|---|---|---|---|---|---|
| `gutter-viewrail` | 视图栏 ↔ 预览区 | 220px | 160–320 | `col-resize` | `ieet.vrw` |
| `gutter-inspector` | 预览区 ↔ 右侧面板 | 360px | 280–560 | `col-resize` | `ieet.inspw` |
| `gutter-timeline` | 时间线 ↔ 上方 | 96px | 72–160 | `row-resize` | `ieet.tlh` |

- 分隔条 **固定 6px**（`--gw`），底色 `--parchment`，hover / 拖拽 / focus 时居中显 **2px `--accent`** 线，
  不加阴影、不加渐变。宽度由 CSS 变量 `--vrw` / `--inspw` / `--tlh` 驱动，写在 `:root` 上。
- 指针拖拽使用 Pointer Events + `setPointerCapture`；键盘可达（`role="separator"` + `tabindex="0"`，
  方向键每次 16px / 8px）。右侧面板可折叠（`--inspw:0`）状态存 `ieet.inspCollapsed`。

### 8.3 时间线（v1 太挤 → v2 两行结构）

```
┌ timeline  height = var(--tlh)，默认 96px ──────────────────────────────┐
│ 元信息行 14px  mono 10px  左「任务 tr-2418 · 当前段 02:41」+ 状态徽标   │
│                            右「总用时 07:12 · 预计 ~11:00」            │
│ 名称行  1fr    7 列等宽网格 minmax(136px,1fr)，名称 11px sans nowrap   │
│                进行中阶段名下加 mono 11px「02:41 · 已译 128/206 段」   │
│                名称与时长条之间 1px `--hair-2` 细线连接（高 6px）      │
│ 时长条行 14px  条高 10px，宽度 = 该段耗时 / 最长段（共享线性比例尺）   │
└───────────────────────────────────────────────────────────────────────┘
```

- **比例只体现在时长条上**：名称列等宽，条宽 `flex = duration`（同一 px/秒 线性标尺，按列裁切）。
- **未开始阶段**：条固定 `64px` + `1px dashed --hair-2`，不参与比例。
- **进行中**：秒表与 `已译 x/y 段` 放在名称行下方（mono 11px），**不塞进时长条**；条内 55% 不透明度填充表示批内进度。
- 压缩态：`--tlh < 94px` 时隐藏名称下第二行，进度改为名称右侧 mono 10px 内联（`.tl-compact`）。
- 阶段整列可点击 = 事件流按阶段过滤，选中列加 `0 0 0 2px --accent`；失败段名旁 6px `--err` 圆点。
- 时间线总宽不足时横向滚动，名称永不换行、永不竖排。

### 8.4 识别类别色板（7 类淡暖色，虚线描边）

低彩度暖色系，`1px dashed` 描边 + 26% 填充的图例色块；均为图形编码，**不计入 accent 配额**，
但每色在 ivory 上 ≥ 3:1，且标签文字用 `color-mix(cat 58%, fg)` 保证 ≥ 4.5:1。

| 类别 | Token | oklch | 用途 |
|---|---|---|---|
| 正文 | `--c-text` | `oklch(52% .030 78)` | 暖褐 · 段落正文 |
| 标题 | `--c-head` | `oklch(46% .050 42)` | 赭石 · 各级标题 |
| 公式 | `--c-eq` | `oklch(54% .035 100)` | 橄榄 · 行间与行内公式 |
| 图 | `--c-fig` | `oklch(50% .055 26)` | 陶土 · figure 与图注 |
| 表 | `--c-tbl` | `oklch(52% .040 118)` | 苔绿 · table |
| 页眉页脚 | `--c-margin` | `oklch(58% .020 62)` | 灰褐 · running head / 页码 |
| 参考文献 | `--c-ref` | `oklch(44% .045 20)` | 深赭 · 引文条目 |

识别视图为**只读**：层开关（版面块 / 段落框 / 字符框 / 行内公式）在右侧面板，选中区块显示
`id / 类别 / 页 / bbox(x y w h) / 是否翻译 / 识别置信度`；面板底部固定一行 `--fg-4`：
「修正识别结果将在后续版本提供」。

### 8.5 文案与状态（v2 统一口径）

- 「未编译」从词典中移除。已保存但未编译 → **「预览中」**（`chip--acc` + 纸页墨蓝虚线角标）。
- 改动数量统一表述为 **「N 处改动」**：视图栏翻译条目徽标与预览上方上下文条
  「3 处改动 · 正在重排预览（第 5 页）」必须同数同词。
- 归档视图每版提供四种产物下载（PDF / dual PDF / 译文 Markdown / 报告，`.lnk` 文字链，
  hover 才出现墨蓝下划线），并有「回滚到此版本」描边按钮；顶部「标记完成并归档」在
  `blockers > 0` 时禁用，禁用原因写在包裹层 `.tip--w` 的 tooltip 里（禁用是全站唯一允许降对比度的状态）。
