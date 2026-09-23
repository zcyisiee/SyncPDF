# Rust 真实翻译 MVP 验收

2026-09-23。目标是尽快给用户可查看、可复跑的后端样张；不做界面。完整功能目标仍未完成。

## 已接入

- `bdt rust-translate` 调用原有Rust sidecar和本机pi，默认deepseek/deepseek-flash、low；保留唯一bdt产品入口。
- PP-DocLayout-V3 Apple GPU、Markdown one-shot、首个闭合块立即排版、固定原字号、源内容保护与部分结果状态贯通。
- `--cached-from` 只读复制先前真实译文缓存；缓存仍按源文本/语言/协议校验，缺失与坏块回退，主请求/补救请求均为0。这样可独立重复验证排版，无需等待模型。
- 普通数字原子仅在单一样式/真实源范围明确且段落不与注释相交时按原文回填；公式、引用、URL原子继续保留。

## 真实运行历史

输入：`/Users/zhengcaiyi/Downloads/2106.04690v2.pdf`，23页。证据均在本仓库`tmp/backend-repair/`。

| 运行 | 结果 |
|---|---|
| `mvp-real-p1-3` | 63.10秒；主请求1/补救0；16成功、14原子回退、0溢出；首次typeset53.57秒，首页落盘56.17秒 |
| `mvp-real-full-v1` | 352.39秒；主请求1/补救10；90成功、72回退；首次typeset133.50秒，首页落盘136.10秒，翻译阶段351.94秒结束，已证明编译早于模型完成 |
| `mvp-real-full-v2` | 接数字后107段排版就绪，但278.38秒时最后补救响应出现unknown escape，4块未落定；仅最后已保存快照可用，不能把107段准备好当成107段均已发布 |

最终交付使用下一节的真实缓存重编译结果；v1/v2证据保留，没有改成通过。

## 工程检查

- 主控完整review并合入bdt候选`4fe625d7`，补PageReady的1基页号、产物IO错误、矛盾成功状态及中断子进程组处理。
- Rust相关3crate：269通过/0失败/5 ignored；pipeline普通数字专项包含141 unit通过，仍保护公式/引用/URL/注释相交/跨样式；cache-only验证有效缓存命中、坏身份拒绝、未命中保留原文、真实通道0调用。
- bdt入口、cache只读复制与单入口守卫：26 pytest通过；Ruff、fmt、strict Clippy通过。release及最终真实重编译结果另附。
- 日志：`tmp/backend-repair/mvp-runner/`。

## 当前限制

A3 dual、中文目录、链接点击框重建和逐块字体/字号编辑尚未交付。未能安置的公式/引用、源区域冲突及固定字号溢出仍保留原文。普通正文有中文翻译但全文仍为部分结果；不能将MVP写成完整论文翻译质量通过。动态库仍依赖本机开发环境，CLI指南有运行命令；打包分发未完成。

## 最终 MVP 交付

产物目录：`tmp/backend-repair/mvp-20260923/`，包含`translated.pdf`、`preview-3-pages.pdf`、`README.md`未成功块清单、事件/结果JSON与保留内容量测。

- 全23页完成保存、验证和发布；18.993秒，真实缓存158段，主请求0/补救0，未生成模拟译文。
- 107块成功排版并写入，55块回退（40原子、12固定字号溢出、3无有效译文；4个缓存未命中块中的1块已按原子回退计入）；另40源区域重叠和1旋转侧注保留，均显式提示。作者机构/脚注/图表/reference仍按保护策略保留。
- 62943个保留字符位置、字号、颜色核对，变化0；qpdf通过。主控目视首页中文标题/摘要与p7正文/表格旁文字；仍可见中英混排和留白，交由用户反馈，不宣称完整排版质量。
- 23个PageReady、0 error事件；RunFinished false / bdt exit1正确表示部分译文，不能改为完整成功。
- Rust相关269测试通过、strict Clippy/fmt/release通过；bdt26pytest与Ruff通过。

### 本机复跑

```bash
source tmp/backend-repair/codex-r1-integration/env.sh
~/miniconda3/envs/bdt/bin/python -m babeldoc_tools rust-translate \
  /Users/zhengcaiyi/Downloads/2106.04690v2.pdf \
  --workdir tmp/rust-mvp-next --cached-from tmp/backend-repair/mvp-real-full-v1 \
  --layout-device coreml
```

去掉`--cached-from`即使用真实pi模型翻译。每次用新workdir；默认字号不缩小。后续优先按样张反馈修复阻碍阅读的公式/引用与源区域归属，再接dual/目录/链接和局部编辑；本报告不代表完整后端目标完成。
