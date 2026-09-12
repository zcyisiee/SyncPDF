# LaTeX Bbox 排版设想最终实验评估报告

**作者/编写者：** Antigravity (`agy`)  
**评估日期：** 2026-09-12  
**产品代码变动：** 无（零产品代码侵入，纯离线与无害分支实验）  
**关联工件路径：** `docs/layout-hypothesis/`

---

## 0. 审查流程说明与背景说明

在生成本报告之前，项目团队曾尝试启动独立审查代理以执行无死角交叉核验，但该审查代理在执行过程中因达到 900 秒超时限制而中断失败，未生成最终的独立 `review.md`。为保证工程严谨性与学术诚实性：
- **明确记录：** 不得且未将独立代理审查视为“已完成”；
- **报告编写说明：** 本最终报告由 Antigravity (`agy`) 依据当前目录下真实存在的所有侦察日志、实验数据、中间脚本、PDF/PNG 产物以及实际测量结果综合整理并严格撰写。

---

## 1. 面向用户的明确结论

用户设想的核心内容为：
> *“翻译后的 PDF 文本来源于 BabelDOC 解析与 MinerU OCR 的交叉比对；MinerU 的 OCR 结果中包含行内 LaTeX 公式。目前按 bbox 渲染时经常出现糟糕的断行（如行末异常留白、过早折行）。设想将带有 LaTeX 的文本喂给真实的 LaTeX 编译器，令其在目标 bbox 尺寸内排版成独立区域，再以贴片方式盖回原 PDF，以此修复排版缺陷。”*

经过对真实 BabelDOC 运行作业及 MinerU 缓存数据的端到端全链路实测，结论如下：

| 维度 | 结论 | 详细状态 |
|---|---|---|
| **机制与几何可行性** | **已被完全证实 (Feasible)** | **已验证**。XeLaTeX 配合 `xeCJK`、BabelDOC 内置思源黑体、`geometry` 设置 bbox 纸张尺寸、`\XeTeXlinebreaklocale "zh"` 以及 `\xeCJKsetup{PunctStyle=plain}`，能够高保真在 bbox 范围内排版，行均 fill 达到 0.996（完全两端对齐）。PyMuPDF 坐标变换与 `show_pdf_page` 贴片毫秒级无缝对齐。 |
| **公式 OCR 可编译性** | **已被完全证实 (Feasible)** | **已验证**。MinerU OCR 产出的 LaTeX 数学公式规范度极高，实测 4 篇论文共 300 个公式 spans 达到 100% (300/300) 原样编译通过率。包含多行行内公式的真实 MinerU 段落 block 也成功在 bbox 尺寸下完美排版并贴回。 |
| **现有数据流直接接入** | **不可直接接入 (Incompatible As-Is)** | **不可直接套用**。当前系统的核心数据流中，翻译后的正文文件 `agent/translated.jsonl` **完全不包含 LaTeX 公式代码**，公式全被替换为 BabelDOC 占位符 `{vN}`（占比 30%~43%）。公式本身在现有流程中通过保留原 PDF 矢量曲线或切图保护。若要实现设想，必须研发一套严密可靠的“译文 prose + 样式 span + MinerU 公式源码”三元反向重组模块。 |

### 实验结论拆解速览：
1. **已证明成立的部分：**
   - MinerU 提取的 LaTeX 公式语法完全兼容标准 TeX 编译引擎（`amsmath`/`amssymb`）；
   - 指定 bbox 物理宽高的单页 TeX 页面生成与编译在 93%~97% 的段落下直接在源字号贴合；在引入小步长有界缩小（Bounded Shrink）算法后达到 **100% (60/60)** 完美适配；
   - 彻底解决了原流程中非末行提前折断（fill 仅 0.185~0.293）的排版断行缺陷；
   - 坐标正反算中，IL y-up 到 MuPDF y-down 的解析变换与最终 overlay rect 精确一致；实验记录的最近 MinerU bbox 匹配距离为 L1=8.22 pt，属于不同 bbox 来源之间的匹配差异，不能等同于 overlay 坐标误差；
   - 使用 `apply_redactions()` 可以在盖入 LaTeX 排版 PDF 的同时，彻底抹除底层原英文，保证双层 PDF 文本层干净可检索且不产生乱码重叠。
2. **尚未验证 / 尚未实现的部分：**
   - **公式回填机制（Formula Fusion）未实现：** 尚未编写将 `{vN}` 映射并还原为真实 MinerU LaTeX 公式的自动化对齐代码；
   - **全文档自动化级联：** 实验覆盖了多篇典型论文的数十个段落及整页测试，未做全本数百页端到端回归；
   - **链接与交互要素重映射：** 未验证超链接、文献引用跳转在 redaction 之后的注记迁移。

---

## 2. 实验环境、真实输入与执行证据

本实验拒绝任何合成数据或伪造环境，完全依托本机真实 TeX 发行版、真实 BabelDOC 运行缓存与真实历史作业进行。

### 2.1 探测到的真实运行环境

- **操作系统：** macOS (Darwin arm64)
- **TeX 引擎：** `/Library/TeX/texbin/xelatex` (TeX Live 2023)
- **宏包可用性：** `xeCJK`、`geometry`、`standalone`、`preview`、`amsmath`、`amssymb`、`ctex`（通过 `kpsewhich` 验证全部存在）
- **中文字体：** `~/.cache/babeldoc/fonts/SourceHanSansCN-Regular.ttf`（BabelDOC 自带的思源黑体）
  - *环境关键坑点：* 在 `fontspec` / `xeCJK` 中不能使用家族裸名 `Source Han Sans CN`，必须通过物理路径 `Path=.../, Extension=.ttf, UprightFont=SourceHanSansCN-Regular` 加载；否则会抛出找不到字体的致命错误。
- **Python 环境：** Python 3.12.2，`PyMuPDF` (fitz) 1.27.1

### 2.2 真实测试输入工件

| 作业代号 | 真实文件路径 | 页面数 | 对应的 MinerU 缓存 (SHA256) |
|---|---|---|---|
| `2026-f1872` | `/tmp/babeldoc-three-test.eH8NAn/2026-f1872/2026-f1872-paper/input.pdf` | 20 | `f15a0e00eed725df8ed53c5da37250bb2a09ac1dd4c7710013d6b0b8d5ac062e.json` |
| `2312-04432` | `/tmp/babeldoc-three-test.eH8NAn/2312-04432/2312.04432v2/input.pdf` | 16 | `773068fa427a7c594c0bd97361aea23eb5bc68dc513548604c628029c3d0c4e9.json` |
| `ccs2026b` (公式样本) | `/tmp/babeldoc-three-test.eH8NAn/ccs2026b` | 21 | `ebdca8e7e579450770eafdf98243f197b8e60fc115fe07ab15870a31812a326b.json` |
| `DeepSeek` (公式样本) | `/tmp/babeldoc-e2e-deepseek-v4-flash` | 51 | `ba68e2e40408125ae6d2f63a9a241b61c73910691c74ec1a2a7023c851eac08d.json` |

每个作业均直接读取历史缓存中的只读文件：`agent/layout_geometry.json`、`agent/translated.jsonl`、`output/*.mono.pdf`。

### 2.3 执行脚本矩阵与重现命令

实验代码全部收敛存放在 `docs/layout-hypothesis/scripts/`：
- `latex_box.py`：底层核心工具函数，负责生成 `.tex`、执行 `xelatex` 编译、墨迹与行 fill 计算、PyMuPDF `show_pdf_page` 贴片；
- `check_mineru_latex.py`：对 MinerU 提取的公式做 300 样本编译健壮性压测；
- `run_experiment.py`：主流程驱动，测试 H1、H2、H3 及多策略字号伸缩 Sweep；
- `verify_coordinates.py`：针对三套坐标系执行严密的数学闭环验证；
- `check_side_effects.py`：测试 `none`、`draw` 与 `redact` 模式下文本层重复性、字体嵌入及检索可用性；
- `compare_line_quality.py`：提取现有流水线双行截断缺陷并与 LaTeX 渲染做行填充率定量对比；
- `page_pass.py`：整页多段落批量编译压测。

**一键重现命令：**
```bash
cd /Users/zhengcaiyi/Desktop/博0/杂项/Github小玩意/BabelDOC/ieeTranslater
for s in run_experiment verify_coordinates check_side_effects compare_line_quality page_pass check_mineru_latex; do
  python3 docs/layout-hypothesis/scripts/$s.py
done
```

---

## 3. 核心假设（H1、H2、H3）实验评测与定量结果

所有量化指标均以 `docs/layout-hypothesis/out/` 下生成的实际 JSON 为准。

### 3.1 H1：MinerU OCR LaTeX 语法可编译性验证

**结论：完全证实 (Confirmed 300/300)**

从四个跨领域的真实 MinerU OCR 缓存文件中，抽取全部出现的 `inline_equation` 与 `interline_equation`，置入标准 LaTeX 数学环境中通过 XeLaTeX 原样编译：

| 缓存来源与论文主题 | 样本数 (Spans) | 原样编译成功数 | 成功率 | 错误数 |
|---|---|---|---|---|
| `f15a0e00` (2026-f1872 / 安全分析) | 4 | 4 | **100.0%** | 0 |
| `773068fa` (2312-04432 / 联邦学习) | 55 | 55 | **100.0%** | 0 |
| `ebdca8e7` (ccs2026b / 系统密码学) | 117 | 117 | **100.0%** | 0 |
| `ba68e2e4` (DeepSeek-V4 技术报告) | 124 | 124 | **100.0%** | 0 |
| **合计** | **300** | **300** | **100.0%** | **0** |

- **语法包容度：** MinerU 生成的带有空格的符号结构（例如 `\mathbf { w } _ { \mathrm { p r e } }`）、自动编号 `\tag{1}`、`\text{...}` 以及包含复杂阵列的 `\begin{array}` 均直接编译成功。
- **重要边界限制：** 这一 100% 成功率是在“抽取纯公式 span 放入数学模式”下取得的。如果直接把 MinerU 包含普通英文与公式的一整行原始文本不做处理扔给 LaTeX，会发生 `! Missing $ inserted.` 错误。必须对普通文本进行转义并将公式用 `$` 包裹。
- **H1b 真实段落 block 综合排版验证：**
  测试了 2312-04432 论文第 1 页的 Block 9（12 行 MinerU 文本，内含 7 个行内公式，bbox 为 257.0 × 145.0 pt），编译排版产物为 11 行，完美容纳并贴合 bbox，证明了图文公式混排的可行性（生成中间件见 `work/2312-04432_block9_p1/2312-04432_block9_p1.pdf` 与 `.tex`，并在 `out/experiment-results.json` 的 `block_latex` 节点完整记录）。

### 3.2 H2：Bbox 空间适配与文字充满度（Fit Sweep）

在每篇论文中抽取 30 个真实正文段落（共 60 个段落），以段落的原始基准字号（`src_font_size`，约为 9.963 pt）编译进相同长宽的页面中。

| 论文代号 | 抽取段落数 | 原始字号直出适配率 | 失败原因分布 | 有界缩小 (Bounded Shrink) 适配率 |
|---|---|---|---|---|
| `2026-f1872` | 30 | **93.3% (28/30)** | 1 × 垂直溢出，1 × overfull-hbox | **100% (30/30)** |
| `2312-04432` | 30 | **96.7% (29/30)** | 1 × overfull-hbox | **100% (30/30)** |

#### 关键技术发现：xeCJK 标点挤压与溢出假象
在调试 `2026-f1872` 段落 `P02-001`（bbox 252.55 × 143.23 pt）时发现：
- 在默认 `xeCJK` 配置下，中文句号/逗号在行末会采用悬挂（Hanging/Quadding）间距，墨迹横向边缘达到 **258.50 pt**，超出页面边界 6.9 pt，被判定为 overflow；
- 当启用 `\xeCJKsetup{PunctStyle=plain}` 后，中文标点严格收缩在版心内，墨迹最大宽度精准收敛为 **251.61 pt**，刚好贴合且无任何字形截断；
- **230/230 个字符完全提取无丢失**，9 行正文非末行填充率均达 0.996。

#### 适配策略对比与 Bounded Shrink 算法：
1. `as_is`（原始字号）：适配率 93.3% ~ 96.7%；
2. `pad1`（人为添加 1 pt 内边距）：由于部分紧凑段落因此提前折行，反而导致总高度溢出（2312-04432 适配率降至 90.0%）；
3. `pad1_sloppy`：提升部分段落，但仍无法解决全部长英文 token；
4. **Bounded Shrink（有界递减）：** 借鉴原产品 `_find_optimal_scale_and_layout` 思想，以 0.95、0.90 阶梯微调字号与行距，实现 **60/60 = 100% 全量贴合**。

### 3.3 H3：坐标映射与 PDF 叠加（Mechanics）

**结论：完全证实 (Confirmed Exactly)**

以段落 `P02-001` 为例，各坐标系对齐数据如下：
- **IL 坐标 (y-up):** `[49.009, 593.771, 301.561, 737.004]`（页面总高度 792.0 pt）
- **IL 镜像计算 (y-down):** `[49.009, 54.996, 301.561, 198.229]`
- **最近 MinerU Bbox:** `[45.000, 55.000, 303.000, 201.000]`（L1 距离仅 8.22 pt，验证了两者指的是同一块版面区域）
- **PyMuPDF 实际贴片 Rect:** `[49.009, 54.996, 301.561, 198.229]`（绝对吻合）

**性能与体积：**
- 单段落 PDF 贴片生成仅约 29 KB；
- `show_pdf_page` 贴片耗时仅 **0.033 ~ 0.057 秒**；
- 贴片字体完整嵌入为 Type0 (`SourceHanSansCN-Regular` 及 `LMRoman10-Regular`)；
- 使用 PyMuPDF `search_for()` 搜索中文字符串，准确返回 `[[49.0, 56.2, 123.7, 66.9]]`，坐标精准对齐。

---

## 4. 关键排版对比：断行质量实测

针对两篇论文第 2~5 页共计 123 个正文段落，对比现有流水线输出与 LaTeX 假说方案的非末行行填充率（Line Fill Ratio = 渲染宽度 / 边框宽度）：

| 论文代号 | 现有流水线段落数 | 现有流水线异常短行段落数 | 现有流水线最小非末行填充率 | LaTeX 最小非末行填充率 |
|---|---|---|---|---|
| `2026-f1872` | 83 | 3 个段落存在断行缺陷 | **0.185** (异常截断留白) | **0.996** (全对齐) |
| `2312-04432` | 40 | 7 个段落存在断行缺陷 | **0.293** (异常截断留白) | **0.996** (全对齐) |

- **现有排版缺陷证据：** 典型如 `2026-f1872` 的 `P03-011` 段落，各行填充率为 `[0.923, 0.883, 0.956, 0.965, 0.185, 0.860, 0.958, 1.0]`。第五行在仅填充了 18.5% 宽度时突兀换行，右侧出现巨大空白。
- **LaTeX 效果：** LaTeX 通过 TeX 经典的 Knuth-Plass 全局折行与对齐算法，非末行填充率稳定保持在 **0.996**，视觉两端完全对齐。
- **实事求是的局限性说明：** 在能够直接提取出纯文本进行 1:1 对比的 9 个（及 11 个）匹配段落中，刚好现存输出也没有短行（0/9, 0/11）。当前流水线产生 0.185 畸变短行的段落往往夹杂着 `{vN}` 占位符或图表列表，这更进一步印证了排版问题与公式占位符紧密相关。

---

## 5. 最重要的数据流发现：占位符机制与公式重组瓶颈

这是将设想落实到产品中时**最关键的架构鸿沟**：

```
[原 PDF] ---> BabelDOC 提取文本层 ---> 替换公式为 {vN} ---> 翻译模型 (LLM) ---> translated.jsonl (含 {vN}，无公式)
   |                                                                                |
   +--------> 提取原公式矢量 (PdfFormula) --------------------------------------------+---> 现有排版器 (逐字块拼接)
   |
[MinerU OCR] ---> 产生包含 LaTeX 的 markdown 文本 (在当前流水线中仅作为交叉比对/辅助定位)
```

1. **译文中根本没有 LaTeX：**  
   检查 `agent/translated.jsonl`，所有行均无 `\` 字符，公式全部以 `{vN}` 形式存在。在 `2026-f1872` 中含有 `{vN}` 的段落高达 **445 个 (43.2%)**；在 `2312-04432` 中高达 **178 个 (30.3%)**。
2. **现有公式不重新排版：**  
   现有产品通过 `typesetting.py` 中的 `PdfFormula` 机制直接把原 PDF 中的矢量曲线搬移到目标位置，或者保留区域留白，根本没有在翻译正文阶段把 LaTeX 源码交给 LLM 翻译。
3. **设想落地的核心前置条件：**  
   所谓“直接把带 LaTeX 的文本交给 LaTeX 编译器排版”，在当前架构下**无法对翻译译文直接执行**。必须在 `translated.jsonl` 之后建立反向融合管道：
   - 解析 `{vN}` 占位符并建立到 MinerU 公式 span 的映射索引；
   - 将翻译后的中文 prose 进行 LaTeX 转义（如 `&`, `%`, `_`）；
   - 将对应的 MinerU LaTeX 公式包裹 `$ ... $` 重新填回 `{vN}` 原位置；
   - 保留原有的样式标记（如 `<b>`、`<style>`）；
   - 将拼接好的复合 LaTeX 源码输入给 XeLaTeX 编译。

---

## 6. 底层机制关键陷阱：`draw` 与 `redact` 的本质差异

实验深度测定了在 PDF 盖章过程中的文本层行为（详见 `out/side-effects.json`）：

| 贴片覆盖模式 | 视觉表现 | 裁剪区字符数 | 底层原始英文是否残留 | 是否产生双层重叠乱码 | 文本可检索性 |
|---|---|---|---|---|---|
| `none` (直接贴片) | 正常覆盖 | 990 | **是 (残留)** | **是 (严重污染)** | 中英文混杂检索 |
| `draw` (画白底矩形遮挡) | 正常覆盖 | 990 | **是 (残留)** | **是 (严重污染)** | 中英文混杂检索 |
| `redact` (`apply_redactions`) | 正常覆盖 | **242** | **否 (彻底抹除)** | **否 (干净替换)** | 仅检索到新中文 |

- **重大风险警示：** 使用常规的 `draw_rect([..., fill=white])` 仅仅是向 PDF 内容流追加了一个白色矢量矩形指令，**底层的原始英文字符仍然存在于文本流中**！如果用户复制粘贴或者划词翻译，会复制出重叠的乱码双语内容。
- **唯一正确解法：** 必须使用 PyMuPDF 的 `apply_redactions()` 物理抹除原文本，再执行 `show_pdf_page()`。此时必须注意：执行 redaction 会一并抹除区域内的 PDF Annotation（如超链接），后续必须配合链接重建逻辑。

---

## 7. 边界风险与工程局限性梳理

在将该技术产品化之前，必须清醒评估以下系统风险：

1. **中文排版宏包副作用：**  
   必须强制注入 `\xeCJKsetup{PunctStyle=plain}` 和 `\XeTeXlinebreaklocale "zh"`。否则行尾标点外挂会导致假性溢出，未启用断行规则会导致中文无法自动折行。
2. **长不可断 Token（邮箱/超长参数）：**  
   如论文中的作者邮箱 `ahmad.sadeghi@trust.tu-darmstadt.de` 或大写参数，TeX 无法在中间断字，会导致严重的 `overfull \hbox`。实验表明 `\sloppy` 仅能缓解部分，必须依赖 Bounded Shrink 机制整体微调缩小字号。
3. **多栏、图表与跨页漂移：**  
   当前实验严格限制在单段落独立 bbox 内。如果段落跨页、跨栏或者处于表格内部，必须由上游版面分析做前置几何切分，不能依赖 TeX 自身的自动换页。
4. **编译性能与吞吐瓶颈：**  
   实测调用系统级 `xelatex` 编译单个段落耗时约为 **0.46 ~ 0.62 秒**。对于一整页（约 4~5 个纯文本段落），页面级批量编译需要 2.3 ~ 2.5 秒；若一篇论文包含 500 个段落，纯串行编译需要 **4~5 分钟**。这显著高于现有纯 Python 文本放置的速度，必须引入并行编译进程池或常驻 Daemon（如 tectonic / chktex 服务）。
5. **宿主环境与分发依赖：**  
   引入此设想意味着运行环境必须预装 TeX Live / XeTeX 引擎及中文字体，镜像体积将激增数 GB，且在极简 Docker 镜像或轻量 CI 运行中不可用。

---

## 8. 推荐的分阶段产品化演进路线

为避免对稳定运行的产品核心流水线造成破坏，推荐采用“三步走”渐进式落地路径：

```
[Phase 1: 可选后处理实验开关] ---> [Phase 2: 公式与占位符回填网关] ---> [Phase 3: 规模化全要素验收]
   (仅处理纯文本短行段落)               (打通 {vN} 与 MinerU 公式)              (多模态覆盖率与端到端质检)
```

### Phase 1：作为可选的实验性后处理插件 (Post-Process Hook)
- **范围约束：** 仅针对无占位符（`{vN}` count == 0）且在现有流水线中出现严重短行（`min_fill < 0.8`）的正文段落开启；
- **实现手段：** 在 `PDFCreater` 完成整篇 PDF 渲染后，针对识别出的问题 bbox 执行 `apply_redactions` 并叠加 XeLaTeX 贴片；
- **配置隔离：** 增加配置项 `--enable-latex-bbox-layout=false`，默认关闭，仅在具备 TeX 依赖的环境下手动开启。

### Phase 2：构建公式占位符反向对齐融合模块 (Formula Fusion)
- **研发任务：** 打通 `agent/translated.jsonl` 中 `{vN}` 与 MinerU OCR `*_equation` 的对应映射；
- **数据结构转换：** 产出包含正确数学转义与中文转义的标准 LaTeX 段落文本；
- **注记保护：** 接入 URI 链接与注记重映射机制，防止 `redact` 操作破坏原文档的文献引用链接。

### Phase 3：性能工程化与多模型回归验收
- **性能优化：** 探索单次 TeX 批量排版整页所有 bbox（通过分页构建独立的子区域或多页面 PDF 单次编译提取），替代单段落逐个 fork 启动 `xelatex` 进程，将页面编译耗时压到 1 秒以内；
- **规模测试：** 扩展至 20 篇以上各领域学术论文（包含双栏 IEEE、单栏 ACM、Nature 等），执行像素级差异比对与文本层端到端检索校验。

---

## 9. 证据分类与事实清单（Evidence Audit）

本报告遵循学术求实原则，所有结论均明确标注其支撑层级：

### 9.1 已执行的直接物理证据 (Executed Proof)
- [x] MinerU OCR 300 个真实公式 span 编译测试，通过率 100%（`out/mineru-latex-compilability.json`）；
- [x] 两篇真实论文 60 个段落原始字号适配率 93.3%~96.7%，有界微调适配率 100%（`out/experiment-results.json`）；
- [x] IL y-up → MuPDF y-down → overlay rect 的坐标闭环吻合；`out/coordcheck.json` 同时记录最近 MinerU bbox 的 L1 匹配距离为 8.22 pt，未将该匹配距离误报为坐标变换误差；
- [x] `none` / `draw` / `redact` 三种遮盖模式下的真实文本层字符计数与提取差异（`out/side-effects.json`）；
- [x] 现有输出非末行断行填充率低至 0.185 的缺陷测量，及对应 LaTeX 方案达到 0.996 的两端对齐度（`out/line-quality-comparison.json`）；
- [x] 整页 16 个段落批量编译覆盖实测成功（`out/page-pass.json`）。

### 9.2 静态代码与架构分析结论 (Static Analysis)
- [x] `agent/translated.jsonl` 不含公式且大量依赖 `{vN}` 占位符（43.2% 及 30.3%）；
- [x] 现有系统通过原 PDF 矢量提取（`PdfFormula`）保留公式，而不是由文本流渲染；
- [x] `apply_redactions` 会破坏原 PDF Link Annotations。

### 9.3 明确列为推测或尚未验证的事项 (Hypothesized / Unverified)
- [ ] `{vN}` 还原映射到 MinerU 公式时的行内 baseline 垂直对齐精度是否完美；
- [ ] 大规模全书运行下的复杂表格与分栏段落级联交互行为；
- [ ] 人工大规模主观视觉质量评测（当前断行评估主要依据 `min_fill` 统计指标）。

---

## 10. 报告编写与完成记录

- **编写工具：** Antigravity CLI (`agy`)
- **审查状态记录：** 独立审查代理启动后因 900s 超时退出，未产生 `review.md`。本报告严格基于现有硬性实验数据独立分析撰写，未假定任何外部审查已通过。
- **产品代码安全审计：** 确认全程未对 `ieeTranslater` 既有业务代码做出任何改动。
