# SyncPDF 重写：调研、技术路径与整体计划

日期：2026-09-22。本目录保留把 Python 版重写为 **SyncPDF**（Rust 引擎 + Electron 前端，内容流改写输出）的原始决策，以及后续修复与验收。恢复工作先读唯一 [task state](task-state.md)；下方决策摘要和指标是原始方案，不代表已实现能力。当前优先修复后端，最新模型/委派约束以task state为准。

## 文件导航

| 文件 | 读者 | 内容 |
|---|---|---|
| `01-调研报告.md` | 决策者、主控 | 为什么重写（实测数字）、保留 / 丢弃清单、hjfy 借鉴、技术栈与否决理由、风险、夹具 |
| `02-技术路径与架构.md` | 主控、实现者 | crate 布局、IR、翻译协议、回写算法、字体、排版、JSONL、存储、Electron、IPC、安全 |
| `03-整体计划与任务拆分.md` | 主控 | M0–M4 里程碑、57 个任务、并行组、brief 模板、验收、禁用清单、性能门禁 |
| [task-state.md](task-state.md) | 用户、主控、叶子只读 | 唯一当前状态、偏好、验收与下一步 |
| [04-合并后引擎快速验收](04-合并后引擎快速验收.md) | 主控 | 初始真实输出失败基线 |
| [05-后端修复执行计划](05-后端修复执行计划.md) | 主控 | R1–R5顺序与阶段历史 |
| [06-R1绑定集成验收](06-R1绑定集成验收.md) | 主控、接手者 | 候选保全、累积review、集成、真实论文及字体复验 |
| [handoff-codex.md](handoff-codex.md) | 接手者 | Codex接手前历史快照与环境命令 |
| `research/01-current-project-audit.md` | 实现者按需 | 现版全量审计，18 条必留规约与证据行号 |
| `research/02-hjfy-engine-deep-dive.md` | 实现者按需 | hjfy 引擎逆向细节：地址、数据结构、错误码 |
| `research/03-tech-stack-research.md` | 实现者按需 | 每个 crate / npm 包的版本核实与对比 |
| `research/04-performance-baseline.md` | 实现者按需 | 现版分阶段耗时、17 条缺陷、目标推导 |

## 一页决策摘要

| 决策 | 结论 |
|---|---|
| 产品名 | SyncPDF；crate 前缀 `syncpdf-`；应用包 `syncpdf` |
| 输出模型 | 操作级内容流补丁删原文 + 追加隔离流写译文 + 子集 CIDFontType2；不再有 bbox 贴片与 XeLaTeX |
| 引擎 | Rust workspace `engine/`，10 个 crate；pdfium-render 解析渲染，自写 Op 过滤器 + pdf-writer 回写，lopdf 备选 |
| 字体 / 排版 | skrifa + harfrust + subsetter + fontdb；icu_segmenter Strict + hypher |
| 布局模型 | ort `=2.0.0-rc.13`；PP-DocLayoutV2 起步 → V3；不内嵌 MinerU |
| LLM | genai 0.6.5；默认 OpenAI 兼容端点，Anthropic 第二 |
| 前端 | 自建 Electron 44 + electron-vite + React；复用 VSCode 视觉 token / 分栏 / codicons；不 fork Code-OSS |
| 进程 | 主进程 spawn sidecar，stdin/stdout JSONL，stdin EOF = 取消；不用 napi-rs |
| 翻译协议 | 受限 HTML `<p id>` / `<span data-style>` / `{{KEEP_n}}`；16 条 hjfy 校验码 + 现版 fallback 语义 |
| 身份 | 显式 `GlyphId = (page, (stream_obj, gen), op_index, ordinal)`，取代 pickle `id()` |
| 存储 | 只有 SQLite；`~/Library/Application Support/SyncPDF/` |
| 安全 | 密钥只经 stdin 传入，不进 argv / 日志；凭据 0600；renderer sandbox |
| 首发不做 | 竖排、首字下沉、inpainting、表格内翻译、增量 xref 写入 |
| 执行方式 | 主控拆任务 → Sonnet 子代理独立 worktree 并行 → 主控亲审 diff + 跑测试 → 合并 |

## 关键数字

| 指标 | 现版 | 目标 |
|---|---|---|
| 12 页全量 build | 52s | <2s |
| 流式单块 | p50 4.6s / 尾 14.2s | <100ms |
| 30 页（不含 LLM）| 分钟级 | <15s |
| 编辑 → 预览 | 秒级 | <200ms |
| 导出膨胀 | 14.5× | <2× |
| 安装体积 | 11GB+ | ~100MB |

## 进入实施前的三件事

1. 插桩现版 check 阶段（160s 无细分证据）。
2. 把 `~/.sp/` 三份 source.pdf 与 `examples/ci/test.pdf` 复制到 `engine/fixtures/`。
3. 建 `engine/` 与 `app/` 骨架，然后从 M0 组 A 开始派发。

## 未定论（M0 实测决定）

- fit 阶梯精确步序（hjfy 推断为 12.5% → 25% → ε 三段搜索）。
- pdfium 字形能否稳定映射到内容流 Op；不能则改自写解释器定位。
- 全量重写的膨胀比是否已 <2×；否则提前做增量写入。
