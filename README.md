# BabelDOC 中文增强版

本项目基于上游 [BabelDOC](https://github.com/funstory-ai/BabelDOC)，用于把英文论文翻译成中文 PDF，并生成原文与译文对照版。

## 相对上游的改动

### 超链接保留与对应

上游流程在译文长度变化、跨行或文本被改写后，可能把链接放到错误的文字上。本项目增加了以下处理：

- 解析阶段保存每个链接的源页、矩形、动作、目标页和覆盖字符。
- 重建阶段按优先级定位译文中的对应区域：LaTeX `bdoclink` 标记、存活字符、段落内文字锚点、段落比例映射。
- 链接矩形直接更新到现有 PDF 注释，保留 `/A`、`/Dest`、URI、内部跳转、远程文件动作和边框属性。
- 链接跨行时拆成多个注释，并保留同一个逻辑链接 ID。
- 更新矩形时清理过期 `QuadPoints`，避免点击区域仍停留在旧位置。
- mono 和 dual PDF 都会复制内部目标坐标；跨页目标按目标页尺寸映射。

### 链接审计

`babeldoc/tools/agent/link_audit.py` 会逐条比较源 PDF 和译文 PDF，检查：

- 链接数量和动作是否保留。
- 内部目标页和坐标是否一致。
- 引文、图、表、公式、脚注编号是否覆盖正确文字。
- 外部 URI 是否保留。外部网站只检查地址，不访问网站。
- 区分 `missing`、`wrong_label`、`wrong_role`、`source_invalid` 和 `external_unchecked`。

### Markdown 翻译管线

`skills/document-translate/` 提供可恢复的工具管线：

```text
parse_document → translate_document → apply_translation
→ review_document → reconstruct_pdf → audit_links → layout_lint → report
```

翻译输入使用带段落 ID、样式锚点和公式锚点的 Markdown。写回阶段会检查 ID、锚点顺序、公式占位符和段落完整性。默认翻译模型是 `agy`，可指定模型和 thinking 档位。

### 解析和排版检查

- 解析后检查版面字符覆盖率、目录条目、保护内容和行内公式匹配率。
- 参考文献、作者信息、页脚、图内文字和表格内部文字默认保留原文。
- 参考文献后的 `Appendix`，以及有明确字母编号序列的附录标题可以恢复翻译；孤立标题不会自动恢复。
- XeLaTeX bbox 排版默认开启，失败的段落自动回退到普通排版，并记录回退原因。
- `layout_lint` 检查字号收缩、文本层兼容字形、溢出和重叠。

## 安装

需要 Python 3.12、[uv](https://docs.astral.sh/uv/) 和 MinerU API token。

```bash
git clone https://github.com/zcyisiee/ieeTranslater.git
cd ieeTranslater
uv sync
export MINERU_API_TOKEN="你的 token"
```

翻译模型 CLI 需要单独安装并登录。使用 `agy` 时确认命令可用：

```bash
agy models
```

## 使用方法

### 直接使用上游命令行

适合普通翻译：

```bash
uv run babeldoc \
  --files paper.pdf \
  --openai \
  --openai-model gpt-4o-mini \
  --openai-base-url https://api.openai.com/v1 \
  --openai-api-key "$OPENAI_API_KEY"
```

### 使用增强管线

在仓库根目录执行。每个阶段的结果写入指定工作目录；建议把工作目录放在 `tmp/`。

```bash
WD=tmp/my-paper
PDF="/absolute/path/paper.pdf"

# 1. 解析
bdt parse "$PDF" --workdir "$WD" --layout mineru

# 2. 使用 agy 翻译
bdt translate --workdir "$WD" --model gemini-3.8-flash-low --effort low --timeout 3600

# 3. 写回、审查、重建
bdt apply --workdir "$WD"
bdt check --workdir "$WD" --skip-pdf-checks
bdt build --workdir "$WD" --dual

# 4. 导出报告
bdt report --workdir "$WD"
```

也可以一条命令串起来（`bdt run`）：

```bash
# 完整链路：parse → translate → apply → build → check → report
bdt run "$PDF" --workdir "$WD" --layout mineru --dual

# 续跑：从 build 开始（上游阶段跳过；若上游产物被改动会报 stale_upstream 并要求重跑）
bdt run --workdir "$WD" --from build --dual

# 不调模型走通链路：--markdown self 用 agent/document.md 当译文
bdt run "$PDF" --workdir "$WD" --markdown self --dual
```

`bdt run` 把每阶段的完成标记与关键输入 sha256 记进 `agent/run_state.json`
（run 私有状态，其他工具不读）。任一步失败即以该步错误 JSON 退出（exit 1），
已完成阶段产物保留在 workdir 供续跑。`--from` 见下方"常用参数"。

链接审计：

```bash
python - <<'PY'
from babeldoc.tools.agent.link_audit import audit_links

audit_links(
    "原文.pdf",
    "tmp/my-paper/output/译文.no_watermark.zh.mono.pdf",
    report_path="tmp/my-paper/agent/link_audit.json",
)
PY
```

也可以使用仓库内的稳定工具入口（stdout 恒为单行 JSON）：

```bash
bdt --help                              # 子命令清单
bdt parse --help                        # 单子命令参数
```

### 常用参数

- `layout=mineru`：使用 MinerU 解析。没有 token 时，可传 `mineru_json` 回放本地布局缓存。
- `model`：翻译模型，例如 `gemini-3.8-flash-low`。
- `effort`：`low`、`medium` 或 `high`。
- `dual=true`：同时生成 mono 和 dual PDF。
- `latex_bbox=true`：启用 bbox 内 LaTeX 两端对齐排版。
- `pages`：只处理指定页，例如 `1,2,5-7`。
- `--from {parse,translate,apply,build,check,review,report}`（仅 `bdt run`）：
  从指定阶段续跑。若被跳过阶段的输入哈希与 `agent/run_state.json` 记录不符
  （上游被改动），会以 `stale_upstream` 报错并在 `suggested_from` 里给出该重跑的
  最早阶段，不会静默沿用旧产物。
- `--markdown`：导入已有译文；`bdt run ... --markdown self` 表示用
  `agent/document.md` 自译（不调用模型，适合离线验证链路）。
- `--translator`：翻译 provider 选择，语义由后续阶段定义；当前版本仅接收并写入
  `run_state.json` 的 `config`。

### 特性开关与默认值

**MinerU 识别出的 `inline_equation`（行内公式）是默认行为，没有任何开关**。只要布局阶段产出了 provider IR（`layout=mineru` 或 `paddle` 都会），它就在两段链路上自动生效：

1. 翻译前：`InlineMathProtector` 在三个入口（extract / md-translate / high_level）无条件运行，把 inline_equation 的 span 盒转成 formula 布局区 → 聚成 `PdfFormula` → 翻译模型看到 `{vN}` 占位符；
2. 渲染时：LaTeX bbox 融合的 `mineru` 级自动采用 span 自带的公式 LaTeX（精确盒匹配 + 三道一致性闸门，不过就降级 simple_math/fragment，同样无开关）。

完整特性清单（CLI = `python -m babeldoc.tools.agent` 子命令旗标；`bdt` = 工具层子命令旗标）：

| 特性 | CLI 旗标 | JSON 键 | 默认 | 说明 |
|---|---|---|---|---|
| 布局后端 | `--layout mineru/paddle` | `layout` | `mineru` | paddle = 本地 PP-DocLayoutV3/PaddleOCR-VL（MLX/CoreML，全 GPU，无需 token）；JSON 入口暂只支持 mineru |
| MinerU OCR 文本回填 | `--mineru-ocr-text` | —（暂未暴露） | **关** | 等长 text span 保守字符回填（见 `samples/pipeline/03b-provider-ocr`）；**会改变模型输入**，实验性 |
| LaTeX bbox 排版 | `--latex-bbox` / `--no-latex-bbox` | `latex_bbox` | **开** | 缺 XeLaTeX/字体自动回退旧渲染并在报告 `fallbacks` 留痕；关闭时输出与旧渲染逐字节一致 |
| LaTeX bbox 模式 | `--latex-bbox-mode full/repair` | `latex_bbox_mode` | `full` | `repair` = 复现旧行为（只修溢出，不整段重排） |
| dual 双语 PDF | `--dual`（reconstruct） | `dual` | CLI **关** / JSON **开** | 拼宽左原文右译文；两入口默认值相反，注意区分 |
| 水印 | —（仅 API `reconstruct(watermark=)`） | `watermark` | 关 | CLI 子命令未暴露 |
| 布局覆盖率门禁 | `--layout-coverage-threshold` | —（暂未暴露） | `0.005` | 原生字符未被任何区域覆盖的占比上限，超限解析失败（防静默漏译） |
| 追加跳过标签 | `--skip-labels` | —（暂未暴露） | 无 | 默认已跳过 reference/author/图表内部/页眉页脚；`*_caption` 保留翻译 |
| 页码子集 | `--pages 1,2 或 1-3` | `pages` | 全文 | 解析前裁剪 |
| MinerU 结果回放 | `--mineru-json` / `--mineru-cache-key`（md-translate） | `mineru_json` | 无 | 离线回放已缓存布局，不联网 |
| 页渲染 DPI | `--dpi`（render） | — | `110` | 视觉审查 PNG |

两条容易混淆的边界：OCR 文本回填（opt-in）与公式 LaTeX（默认）是独立通道——前者动模型输入（改了要重翻译），后者只在渲染侧（升级不需要重翻译）；`latex_bbox` 默认开但尽力而为，能力探测失败自动回退，不会让任务失败。

## 输出文件

工作目录结构通常如下：

```text
agent/
  document.md          # 带段落 ID 的原文 Markdown
  translated.md        # 模型返回的译文
  translated.jsonl     # 写回后的段落
  apply_report.json    # 锚点和 ID 检查结果
  review_verdict.json  # 结构审查结果
  link_audit.json      # 链接逐条审计结果
  layout_geometry.json # 排版几何信息
  layout_lint.json     # 排版检查结果
  usage.json           # 模型用量
  FINAL_REPORT.md      # 汇总报告
output/
  *.zh.mono.pdf
  *.zh.dual.pdf
```

## 测试

运行链接相关测试：

```bash
python -m pytest tests/test_link_remap.py tests/test_link_audit.py tests/test_link_correspondence.py -q
```

运行全部测试：

```bash
python -m pytest -q
```

测试过程中的 PDF、PNG、JSON 和模型日志统一放在 `tmp/`，该目录已加入 `.gitignore`。

## 已知限制

- 外部 URI 只验证注释动作和地址是否保留，不验证网站当前是否可访问。
- `wrong_label` 或 `wrong_role` 表示对应关系需要人工复核；链接数量一致不能证明语义对应正确。
- 译文明显变长时，自动排版可能缩小字号；请查看 `layout_lint.json` 和渲染图片。
- 表格内部文字、图内文字和参考文献条目默认不翻译。
